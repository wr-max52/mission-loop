"""The Wayback Machine (web.archive.org), so archived pages can be read and cited like live ones.

A snapshot address names its original page: https://web.archive.org/web/<timestamp>/<original url>.
Code treats it as that page's website (domain counts and the block list) and reads its raw copy
(<timestamp>id_), which has the original's text and links without the archive's toolbar.
Addresses handed to the model are the normal viewer form, so readers who click a citation see the
archive's banner and the date. No model is involved here.
"""
import re
from datetime import date, datetime
import httpx

AVAILABLE = "https://archive.org/wayback/available"
CDX = "https://web.archive.org/cdx/search/cdx"
MAX_VERSIONS = 8
HEADERS = {"User-Agent": "Mozilla/5.0 (MissionLoop source validator)"}
TRANSPORT = None                           # tests put an httpx.MockTransport here so nothing leaves the machine
SNAPSHOT = re.compile(r"^https?://web\.archive\.org/web/(\d{4,14})(?:[a-z]{2}_)?/(.+)$", re.I)

class Unavailable(Exception):
    """The archive could not be reached or gave an answer that makes no sense."""

def _parts(url: str) -> tuple[str, str] | None:
    m = SNAPSHOT.match(url.strip())
    if not m:
        return None
    original = m.group(2)
    return m.group(1), original if re.match(r"https?://", original, re.I) else "http://" + original

def original(url: str) -> str | None:
    """The page a snapshot address is a copy of, or None if url is not a snapshot."""
    parts = _parts(url)
    return parts[1] if parts else None

def raw(url: str) -> str:
    """The address to download: a snapshot's raw copy, any other url unchanged."""
    parts = _parts(url)
    return f"https://web.archive.org/web/{parts[0]}id_/{parts[1]}" if parts else url

def viewer(timestamp: str, page: str) -> str:
    return f"https://web.archive.org/web/{timestamp}/{page}"

def day(timestamp: str) -> str:
    """'20240104050913' -> '2024-01-04' (shorter timestamps give what they have)."""
    return "-".join(p for p in (timestamp[:4], timestamp[4:6], timestamp[6:8]) if p)

def day_of(url: str) -> str:
    """The date of a snapshot address, or '' if url is not a snapshot."""
    parts = _parts(url)
    return day(parts[0]) if parts else ""

def _get(url: str, params) -> object:
    try:
        with httpx.Client(timeout=30, transport=TRANSPORT, headers=HEADERS, follow_redirects=True) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
            return r.json() if r.content.strip() else []
    except (httpx.HTTPError, ValueError) as e:
        raise Unavailable(type(e).__name__) from e

def snapshot(page: str, when: str = "") -> tuple[str, str] | None:
    """The archived copy closest to `when` (any of YYYY, YYYYMM, YYYYMMDD; default now), as
    (timestamp, viewer url), or None if the archive has no good copy."""
    params = {"url": page} | ({"timestamp": when} if when else {})
    found = (_get(AVAILABLE, params) or {}).get("archived_snapshots", {}).get("closest") or {}
    if not found.get("available") or str(found.get("status")) != "200" or not found.get("timestamp"):
        return None
    return found["timestamp"], viewer(found["timestamp"], original(found.get("url", "")) or page)

def history(page: str, limit: int = MAX_VERSIONS) -> list[tuple[str, str]]:
    """Up to `limit` archived copies that differ from the copy before them, oldest first, spread over the
    whole time the page was archived (always the first and the last). One copy per month is considered."""
    rows = _get(CDX, {"url": page, "output": "json", "fl": "timestamp,original,digest",
                      "filter": ["statuscode:200", "mimetype:text/html"], "collapse": "timestamp:6"})
    if not isinstance(rows, list):
        raise Unavailable("unexpected answer")
    changed, last = [], None
    for row in rows[1:]:                   # the first row is the header
        if len(row) == 3 and row[2] != last:
            changed.append((row[0], viewer(row[0], row[1])))
            last = row[2]
    if len(changed) <= limit:
        return changed
    step = (len(changed) - 1) / (limit - 1)
    return [changed[round(i * step)] for i in range(limit)]

def as_timestamp(text: str) -> str:
    """'2021-05-01' -> '20210501'; '' for anything that isn't a date the archive understands."""
    digits = re.sub(r"\D", "", text)[:8]
    formats = {4: "%Y", 6: "%Y%m", 8: "%Y%m%d"}
    try:
        when = datetime.strptime(digits, formats[len(digits)])
    except (KeyError, ValueError):
        return ""
    return digits if 1996 <= when.year <= date.today().year else ""   # the archive starts in 1996
