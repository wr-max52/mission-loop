"""Source gate. Code decides whether sources are valid; the model never does."""
import re
from pathlib import Path
from urllib.parse import urlparse
import httpx, trafilatura
from rapidfuzz import fuzz
import settings
from contracts import Mission, ResearchBrief

MIN_SUPPORT_DOMAINS = 3
QUOTE_MATCH = 90                           # rapidfuzz partial_ratio threshold
MIN_QUOTE_CHARS = 20
MIN_POCS = 3
MAX_TEXT_CHARS = 300_000
BLOCKED_FILE = Path("blocked_domains.txt")  # one domain per line; subdomains are blocked too
_cache: dict[str, tuple[str, str]] = {}    # successful fetches only; failures may be transient

def blocked_domains(path: Path | None = None) -> set[str]:
    path = path or BLOCKED_FILE
    if not path.exists():
        return set()
    lines = (l.split("#")[0].strip().lower().removeprefix("www.") for l in path.read_text().splitlines())
    return {l for l in lines if l}

def _is_blocked(domain: str, blocked: set[str]) -> bool:
    return any(domain == b or domain.endswith("." + b) for b in blocked)

def fetch(url: str) -> tuple[str, str] | None:
    """Return (raw_html, readable_text) or None if the page can't be fetched."""
    if url in _cache:
        return _cache[url]
    try:
        r = httpx.get(url, follow_redirects=True, timeout=20,
                      headers={"User-Agent": "Mozilla/5.0 (MissionLoop source validator)"})
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    page = (r.text, trafilatura.extract(r.text) or "")
    _cache[url] = page
    return page

def _norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip()

def closest_sentence(quote: str, page_text: str, max_chars: int = 300) -> str:
    """The page's real sentence nearest to a failed quote, so the next attempt can copy it exactly."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", page_text[:MAX_TEXT_CHARS])
                 if len(s.strip()) >= MIN_QUOTE_CHARS]
    if not sentences:
        return ""
    q = _norm(quote)
    best = max(sentences, key=lambda s: fuzz.ratio(q, _norm(s)))
    return best[:max_chars] if fuzz.ratio(q, _norm(best)) >= 50 else ""

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

    # 2 + 3. Every source is allowed, fetches, and its quote appears on the page
    blocked = blocked_domains()
    verified: set[str] = set()
    for s in brief.sources:
        if _is_blocked(_domain(s.url), blocked):
            problems.append(f"{s.id}: {_domain(s.url)} is on the blocked list (blocked_domains.txt); use another source")
            continue
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
                hint = closest_sentence(s.quote, page[1])
                problems.append(f"{s.id}: quote not found on {s.url} (best match {score:.0f}%)"
                                + (f". Closest sentence actually on the page: \"{hint}\"" if hint else ""))
            else:
                verified.add(s.id)

    # 4. At least 3 distinct domains among verified supporting sources
    domains = {_domain(s.url) for s in brief.sources if s.stance == "supports" and s.id in verified}
    if len(domains) < MIN_SUPPORT_DOMAINS:
        problems.append(f"Only {len(domains)} verified supporting domain(s); need {MIN_SUPPORT_DOMAINS}")

    # 5. At least 1 verified contradicting source on a website no supporting source uses, and a counterpoint
    against = {_domain(s.url) for s in brief.sources if s.stance == "contradicts" and s.id in verified}
    if not against:
        problems.append("No verified source contradicts the thesis")
    elif not against - {_domain(s.url) for s in brief.sources if s.stance == "supports"}:
        problems.append("Every source that contradicts the thesis is on the same website as a supporting "
                        "source; find the opposing view on a different website")
    if not brief.counterpoint.strip():
        problems.append("Counterpoint is empty")

    # 6. Outreach only: 3+ POCs, and every POC's email appears on their contact page
    if settings.OUTREACH and len(brief.pocs) < MIN_POCS:
        problems.append(f"Only {len(brief.pocs)} point(s) of contact; need {MIN_POCS}")
    for p in brief.pocs if settings.OUTREACH else []:
        page = fetch(p.contact_source_url)
        if page is None or p.email.lower() not in page[0].lower():
            problems.append(f"POC {p.name}: {p.email} not found on {p.contact_source_url}")

    return (not problems, "\n".join(problems))
