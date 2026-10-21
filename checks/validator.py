"""Source gate. Code decides whether sources are valid; the model never does."""
import re
from urllib.parse import urlparse
import httpx, trafilatura
from rapidfuzz import fuzz
from contracts import Mission, ResearchBrief

MIN_SUPPORT_DOMAINS = 3
QUOTE_MATCH = 90                           # rapidfuzz partial_ratio threshold
MIN_QUOTE_CHARS = 20
MAX_TEXT_CHARS = 300_000
_cache: dict[str, tuple[str, str] | None] = {}

def fetch(url: str) -> tuple[str, str] | None:
    """Return (raw_html, readable_text) or None if the page can't be fetched."""
    if url in _cache:
        return _cache[url]
    page = None
    try:
        r = httpx.get(url, follow_redirects=True, timeout=20,
                      headers={"User-Agent": "Mozilla/5.0 (MissionLoop source validator)"})
        if r.status_code == 200:
            page = (r.text, trafilatura.extract(r.text) or "")
    except httpx.HTTPError:
        page = None
    _cache[url] = page
    return page

def _norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip()

def _domain(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")

def validate(m: Mission, brief: ResearchBrief) -> tuple[bool, str]:
    problems: list[str] = []
    ids = {s.id for s in brief.sources}

    # 1. Every claim cites at least one real source id
    for c in brief.claims:
        if not c.source_ids:
            problems.append(f"Claim cites no sources: '{c.text[:80]}'")
        for sid in c.source_ids:
            if sid not in ids:
                problems.append(f"Claim cites unknown source id '{sid}'")

    # 2 + 3. Every source fetches, and its quote appears on the page
    verified: set[str] = set()
    for s in brief.sources:
        page = fetch(s.url)
        if page is None:
            problems.append(f"{s.id}: could not fetch {s.url}")
            continue
        text = _norm(page[1])[:MAX_TEXT_CHARS]
        quote = _norm(s.quote)
        if not text:
            problems.append(f"{s.id}: no readable text at {s.url}")
        elif len(quote) < MIN_QUOTE_CHARS:
            problems.append(f"{s.id}: quote is too short to verify; copy a full sentence")
        else:
            score = fuzz.partial_ratio(quote, text)
            if score < QUOTE_MATCH:
                problems.append(f"{s.id}: quote not found on {s.url} (best match {score:.0f}%)")
            else:
                verified.add(s.id)

    # 4. At least 3 distinct domains among verified supporting sources
    domains = {_domain(s.url) for s in brief.sources if s.stance == "supports" and s.id in verified}
    if len(domains) < MIN_SUPPORT_DOMAINS:
        problems.append(f"Only {len(domains)} verified supporting domain(s); need {MIN_SUPPORT_DOMAINS}")

    # 5. At least 1 verified contradicting source, and a counterpoint
    if not any(s.stance == "contradicts" and s.id in verified for s in brief.sources):
        problems.append("No verified source contradicts the thesis")
    if not brief.counterpoint.strip():
        problems.append("Counterpoint is empty")

    # 6. Every POC's email appears on their contact page
    for p in brief.pocs:
        page = fetch(p.contact_source_url)
        if page is None or p.email.lower() not in page[0].lower():
            problems.append(f"POC {p.name}: {p.email} not found on {p.contact_source_url}")

    return (not problems, "\n".join(problems))
