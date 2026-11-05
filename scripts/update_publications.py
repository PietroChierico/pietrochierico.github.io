#!/usr/bin/env python3
"""Update the publication list of the website from Google Scholar.

What it does
1. Reads the Google Scholar profile (list page + one detail page per new paper).
2. Looks up DOI and clean author names on Crossref.
3. Applies manual corrections from publications_overrides.json.
4. Writes publications.json (data cache) and two Quarto include files:
     files/includes/_publications.qmd         full list (Publications page)
     files/includes/_publications_recent.qmd  latest papers (Research page)

If Google Scholar cannot be reached (it sometimes blocks automated requests),
the previous publications.json is kept and the include files are regenerated
from it, so the site never loses its list.

Only the Python standard library is used.

Usage:  python scripts/update_publications.py            # fetch + generate
        python scripts/update_publications.py --offline  # only regenerate includes
"""

from __future__ import annotations

import datetime as dt
import difflib
import html
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
SCHOLAR_USER = "ZHbnJvcAAAAJ"
OWNER_FAMILY = "Chierico"          # highlighted in bold in author lists
RECENT_COUNT = 4                   # papers shown on the Research page

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "publications.json"
OVERRIDES_FILE = ROOT / "publications_overrides.json"
INCLUDE_DIR = ROOT / "files" / "includes"
FULL_INCLUDE = INCLUDE_DIR / "_publications.qmd"
RECENT_INCLUDE = INCLUDE_DIR / "_publications_recent.qmd"

SCHOLAR_BASE = "https://scholar.google.com"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
CROSSREF_UA = ("pietrochierico.github.io publication updater "
               "(https://github.com/PietroChierico/pietrochierico.github.io)")

# Venues that Scholar labels "Journal" but are really conference proceedings.
CONFERENCE_PATTERNS = [
    r"computing in cardiology",
    r"conference",
    r"proceedings",
    r"symposium",
    r"congress",
    r"workshop",
    r"advances in digital health and medical bioengineering",  # EHB proceedings
    r"\bEHB\b",
    r"\bEMBC\b",
]

SECTIONS = [
    ("journal", "Journal papers"),
    ("conference", "Conference papers"),
    ("other", "Other publications"),
    ("thesis", "Theses"),
]


class ScholarBlocked(Exception):
    pass


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def http_get(url: str, ua: str = BROWSER_UA, retries: int = 3) -> str:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": ua,
                "Accept-Language": "en-US,en;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as err:  # noqa: BLE001
            last_err = err
            time.sleep(4 * (attempt + 1))
    raise RuntimeError(f"GET failed: {url} ({last_err})")


def strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def norm_title(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def is_conference(venue: str) -> bool:
    return any(re.search(p, venue, re.I) for p in CONFERENCE_PATTERNS)


# --------------------------------------------------------------------------- #
# Google Scholar
# --------------------------------------------------------------------------- #
def fetch_profile() -> list[dict]:
    url = (f"{SCHOLAR_BASE}/citations?user={SCHOLAR_USER}&hl=en"
           f"&cstart=0&pagesize=100&sortby=pubdate")
    page = http_get(url)
    rows = re.findall(r'<tr class="gsc_a_tr">(.*?)</tr>', page, re.S)
    if not rows:
        raise ScholarBlocked("no publication rows found (captcha or layout change)")

    items = []
    for row in rows:
        m = re.search(r'<a href="([^"]+)" class="gsc_a_at">(.*?)</a>', row, re.S)
        if not m:
            continue
        href = html.unescape(m.group(1))
        cid = urllib.parse.parse_qs(urllib.parse.urlparse(href).query) \
            .get("citation_for_view", [""])[0]
        grays = re.findall(r'<div class="gs_gray">(.*?)</div>', row, re.S)
        cites = re.search(r'class="gsc_a_ac gs_ibl">(\d*)<', row)
        year = re.search(r'class="gsc_a_h gsc_a_hc gs_ibl">(\d{4})<', row)
        venue = strip_tags(grays[1]) if len(grays) > 1 else ""
        venue = re.sub(r",\s*\d{4}$", "", venue).strip()
        items.append({
            "scholar_id": cid,
            "title": strip_tags(m.group(2)),
            "authors_raw": strip_tags(grays[0]) if grays else "",
            "venue": venue,
            "year": int(year.group(1)) if year else None,
            "citations": int(cites.group(1)) if cites and cites.group(1) else 0,
            "scholar_url": (f"{SCHOLAR_BASE}/citations?view_op=view_citation&hl=en"
                            f"&user={SCHOLAR_USER}&citation_for_view={cid}"),
        })
    return items


def fetch_detail(scholar_url: str) -> dict:
    page = http_get(scholar_url)
    out: dict = {"fields": {}}
    link = re.search(r'class="gsc_oci_title_link" href="([^"]+)"', page)
    if link:
        out["url"] = html.unescape(link.group(1))
    for k, v in re.findall(
            r'<div class="gsc_oci_field">(.*?)</div><div class="gsc_oci_value">(.*?)</div>',
            page, re.S):
        out["fields"][strip_tags(k)] = strip_tags(v)
    if not out["fields"]:
        raise ScholarBlocked("empty detail page")
    return out


# --------------------------------------------------------------------------- #
# Crossref
# --------------------------------------------------------------------------- #
def crossref_lookup(title: str) -> dict | None:
    q = urllib.parse.urlencode({
        "query.bibliographic": title,
        "query.author": OWNER_FAMILY,
        "rows": 5,
        "select": "DOI,title,author,container-title,type,issued,volume,issue,page,article-number",
    })
    try:
        data = json.loads(http_get(f"https://api.crossref.org/works?{q}", ua=CROSSREF_UA))
    except Exception as err:  # noqa: BLE001
        print(f"  ! Crossref lookup failed: {err}")
        return None
    target = norm_title(title)
    for it in data.get("message", {}).get("items", []):
        cand = norm_title((it.get("title") or [""])[0])
        if difflib.SequenceMatcher(None, target, cand).ratio() < 0.93:
            continue
        fams = [norm_title(a.get("family", "")) for a in it.get("author", [])]
        if norm_title(OWNER_FAMILY) not in fams:
            continue
        return it
    return None


# --------------------------------------------------------------------------- #
# Authors
# --------------------------------------------------------------------------- #
def initials(given: str) -> str:
    parts = re.split(r"[\s\-]+", given.replace(".", " ").strip())
    return " ".join(f"{p[0]}." for p in parts if p)


def authors_from_crossref(cr: dict) -> list[dict]:
    out = []
    for a in cr.get("author", []):
        fam, giv = a.get("family", "").strip(), a.get("given", "").strip()
        if not fam:
            continue
        m = re.match(r"^([A-Z])\.?\s+(.+)$", fam)   # "J Rieta" -> given += "J"
        if m:
            fam, giv = m.group(2), f"{giv} {m.group(1)}".strip()
        out.append({"family": fam, "given": giv})
    return out


def authors_from_scholar(raw: str) -> list[dict]:
    """Scholar mixes 'First Last' and 'Last, First' in one comma list."""
    toks = [t.strip() for t in raw.split(",") if t.strip() and t.strip() != "..."]
    out, i = [], 0
    while i < len(toks):
        t = toks[i]
        if " " not in t and i + 1 < len(toks) and " " not in toks[i + 1]:
            out.append({"family": t, "given": toks[i + 1]})       # "Last, First"
            i += 2
            continue
        words = t.split()
        out.append({"family": words[-1], "given": " ".join(words[:-1])})
        i += 1
    return out


def format_authors(authors: list[dict], truncated: bool = False) -> str:
    """APA-like: Chierico, P., Suárez, D., & Rieta, J. J."""
    names = []
    for a in authors:
        name = a["family"] + (f", {initials(a['given'])}" if a.get("given") else "")
        name = html.escape(name)
        if norm_title(a["family"]) == norm_title(OWNER_FAMILY):
            name = f"<strong>{name}</strong>"
        names.append(name)
    if truncated:
        return ", ".join(names) + ", et al."
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + ", &amp; " + names[-1]


# --------------------------------------------------------------------------- #
# Build records
# --------------------------------------------------------------------------- #
def classify(detail_fields: dict, venue: str) -> str:
    if "Institution" in detail_fields or re.search(r"thesis|universit|politecnico", venue, re.I) \
            and not detail_fields.get("Journal"):
        return "thesis"
    if "Conference" in detail_fields or is_conference(venue):
        return "conference"
    if "Journal" in detail_fields:
        return "journal"
    return "other"


def build_record(item: dict, previous: dict | None) -> dict:
    rec = dict(previous or {})
    # Always refresh what the list page gives us.
    rec.update({k: item[k] for k in ("scholar_id", "title", "year", "citations", "scholar_url")})
    rec.setdefault("venue", item["venue"])

    if not rec.get("_detail_done"):
        print(f"  - details: {item['title'][:70]}")
        try:
            det = fetch_detail(item["scholar_url"])
            time.sleep(3)
        except Exception as err:  # noqa: BLE001
            print(f"    ! detail page unavailable ({err}); using list data")
            det = {"fields": {}}
        f = det["fields"]
        full_venue = (f.get("Journal") or f.get("Conference") or f.get("Book")
                      or f.get("Source") or f.get("Institution") or item["venue"])
        rec["venue"] = full_venue
        rec["type"] = classify(f, full_venue)
        rec["url"] = det.get("url", "")
        rec["volume"] = f.get("Volume", "")
        rec["issue"] = f.get("Issue", "")
        rec["pages"] = f.get("Pages", "")
        raw = f.get("Authors") or item["authors_raw"]
        rec["authors"] = authors_from_scholar(raw)
        rec["authors_truncated"] = raw.strip().endswith("...")
        rec["_detail_done"] = bool(f)

    if not rec.get("doi") and rec.get("type") != "thesis":
        cr = crossref_lookup(rec["title"])
        time.sleep(1)
        if cr:
            rec["doi"] = cr["DOI"]
            rec["authors"] = authors_from_crossref(cr) or rec["authors"]
            rec["authors_truncated"] = False
            rec["volume"] = cr.get("volume", rec.get("volume", ""))
            rec["issue"] = cr.get("issue", rec.get("issue", ""))
            rec["pages"] = cr.get("article-number") or cr.get("page") or rec.get("pages", "")
    return rec


def apply_overrides(records: list[dict]) -> list[dict]:
    if not OVERRIDES_FILE.exists():
        return records
    ov = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
    out = []
    for r in records:
        o = ov.get(r["scholar_id"]) or ov.get(norm_title(r["title"])) or {}
        if o.get("hide"):
            continue
        merged = {**r, **{k: v for k, v in o.items() if k != "hide"}}
        out.append(merged)
    for key, o in ov.items():           # manual extra entries (not on Scholar)
        if key.startswith("extra:"):
            out.append({"scholar_id": key, "citations": 0, "authors": [], **o})
    return out


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def venue_line(r: dict) -> str:
    v = html.escape(r.get("venue", ""))
    bits = []
    if r.get("volume"):
        bits.append(html.escape(str(r["volume"])) +
                    (f"({html.escape(str(r['issue']))})" if r.get("issue") else ""))
    if r.get("pages") and r.get("type") == "journal":
        bits.append(html.escape(str(r["pages"])))
    return f"<em>{v}</em>" + (", " + ", ".join(bits) if bits else "")


def entry_html(r: dict) -> str:
    links = []
    if r.get("doi"):
        doi = html.escape(r["doi"])
        links.append(f'<a href="https://doi.org/{doi}" target="_blank" rel="noopener">DOI: {doi}</a>')
    elif r.get("url"):
        links.append(f'<a href="{html.escape(r["url"])}" target="_blank" rel="noopener">Full text</a>')
    if r.get("scholar_url") and not str(r["scholar_id"]).startswith("extra:"):
        links.append(f'<a href="{html.escape(r["scholar_url"])}" target="_blank" '
                     f'rel="noopener">Google Scholar</a>')
    cites = r.get("citations") or 0
    cite_badge = (f'<span class="pub-cites">Cited by {cites}</span>' if cites else "")
    extra = f'<div class="pub-note">{r["note"]}</div>' if r.get("note") else ""
    if r.get("authors_text"):   # verbatim author string from the overrides file
        authors = html.escape(r["authors_text"]).replace(
            html.escape(f"{OWNER_FAMILY}, P."), f"<strong>{OWNER_FAMILY}, P.</strong>")
    else:
        authors = format_authors(r.get("authors", []), r.get("authors_truncated", False))
    year = f" ({r['year']})." if r.get("year") else "."
    return (
        '<div class="publication">\n'
        f'<div class="pub-authors">{authors}{year}</div>\n'
        f'<div class="publication-title">{html.escape(r["title"])}</div>\n'
        f'<div class="pub-venue">{venue_line(r)}</div>\n'
        f'{extra}'
        f'<div class="pub-links">{" · ".join(links)} {cite_badge}</div>\n'
        '</div>'
    )


def sort_key(r: dict):
    rank = {"journal": 0, "conference": 1, "other": 2, "thesis": 3}.get(r.get("type"), 2)
    return (-(r.get("year") or 0), rank, r.get("title", ""))


def write_includes(data: dict) -> None:
    pubs = sorted(data["publications"], key=sort_key)
    updated = data.get("updated", "")
    INCLUDE_DIR.mkdir(parents=True, exist_ok=True)

    note = ("<!-- AUTO-GENERATED by scripts/update_publications.py — do not edit by hand. "
            "Edit publications_overrides.json instead. -->\n\n")
    stamp = (f'<p class="pub-updated">List synchronised automatically with '
             f'<a href="{SCHOLAR_BASE}/citations?user={SCHOLAR_USER}" target="_blank" '
             f'rel="noopener">Google Scholar</a>. Last update: {updated}.</p>')

    parts = [note, "```{=html}\n" + stamp + "\n```\n"]
    for key, label in SECTIONS:
        group = [p for p in pubs if p.get("type") == key]
        if not group:
            continue
        parts.append(f"\n## {label}\n\n```{{=html}}\n" +
                     "\n".join(entry_html(p) for p in group) + "\n```\n")
    FULL_INCLUDE.write_text("".join(parts), encoding="utf-8")

    recent = [p for p in pubs if p.get("type") in ("journal", "conference")][:RECENT_COUNT]
    RECENT_INCLUDE.write_text(
        note + "```{=html}\n" + "\n".join(entry_html(p) for p in recent) + "\n```\n",
        encoding="utf-8")
    print(f"Wrote {FULL_INCLUDE.relative_to(ROOT)} and {RECENT_INCLUDE.relative_to(ROOT)}")


def main() -> int:
    offline = "--offline" in sys.argv
    old = {"publications": []}
    if DATA_FILE.exists():
        old = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    prev_by_id = {p["scholar_id"]: p for p in old.get("scholar_raw", [])}

    if not offline:
        try:
            print("Fetching Google Scholar profile...")
            items = fetch_profile()
            print(f"  {len(items)} publications found")
            raw = [build_record(it, prev_by_id.get(it["scholar_id"])) for it in items]
            old = {
                "updated": dt.date.today().isoformat(),
                "source": f"{SCHOLAR_BASE}/citations?user={SCHOLAR_USER}",
                "scholar_raw": raw,
            }
        except Exception as err:  # noqa: BLE001
            print(f"WARNING: could not update from Google Scholar: {err}")
            print("Keeping the previous data; will retry on the next run.")

    if not old.get("scholar_raw"):
        print("No publication data available; include files left untouched.")
        return 0
    # Overrides are always (re)applied, so editing them takes effect on the next run.
    old["publications"] = apply_overrides(old["scholar_raw"])
    DATA_FILE.write_text(json.dumps(old, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {DATA_FILE.name}")
    write_includes(old)
    return 0


if __name__ == "__main__":
    sys.exit(main())
