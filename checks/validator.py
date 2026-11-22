"""Source gate. Code decides whether sources are valid; the model never does."""
import re
from pathlib import Path
from urllib.parse import urlparse
import httpx, trafilatura
from rapidfuzz import fuzz
import settings, wayback
from contracts import Mission, ResearchBrief, POSSIBILITY

MIN_SUPPORT_DOMAINS = 3
QUOTE_MATCH = 90                           # rapidfuzz partial_ratio threshold
MIN_QUOTE_CHARS = 20
MIN_POCS = 3
MAX_TEXT_CHARS = 300_000
BLOCKED_FILE = Path("blocked_domains.txt")  # one domain per line; subdomains are blocked too
HEADERS = {"User-Agent": "Mozilla/5.0 (MissionLoop source validator)"}
_cache: dict[str, tuple[str, str]] = {}    # successful fetches only; failures may be transient

def blocked_domains(path: Path | None = None) -> set[str]:
    path = path or BLOCKED_FILE
    if not path.exists():
        return set()
    lines = (l.split("#")[0].strip().lower().removeprefix("www.") for l in path.read_text().splitlines())
    return {l for l in lines if l}

def _is_blocked(domain: str, blocked: set[str]) -> bool:
    return any(domain == b or domain.endswith("." + b) for b in blocked)

def readable_text(html: str) -> str:
    return trafilatura.extract(html) or ""

def fetch(url: str) -> tuple[str, str] | None:
    """Return (raw_html, readable_text) or None if the page can't be fetched."""
    if url in _cache:
        return _cache[url]
    try:                                   # a Wayback snapshot is read as its raw copy, without the toolbar
        r = httpx.get(wayback.raw(url), follow_redirects=True, timeout=20, headers=HEADERS)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    page = (r.text, readable_text(r.text))
    _cache[url] = page
    return page

def _norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip()

def sentences(page_text: str) -> list[str]:
    """The page's sentences that are long enough to be used as quotes."""
    parts = (s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", page_text[:MAX_TEXT_CHARS]))
    return [s for s in parts if len(s) >= MIN_QUOTE_CHARS]

def closest_sentence(quote: str, page_text: str, max_chars: int = 300) -> str:
    """The page's real sentence nearest to a failed quote, so the next attempt can copy it exactly."""
    candidates = sentences(page_text)
    if not candidates:
        return ""
    q = _norm(quote)
    best = max(candidates, key=lambda s: fuzz.ratio(q, _norm(s)))
    return best[:max_chars] if fuzz.ratio(q, _norm(best)) >= 50 else ""

def _domain(url: str) -> str:
    """The website a source is on. A Wayback snapshot counts as the site it is a copy of."""
    return (urlparse(wayback.original(url) or url).hostname or "").removeprefix("www.")

def archived_copy_hint(url: str) -> str:
    """For a page that can't be fetched: where the Wayback Machine's latest copy is, if it has one."""
    if wayback.original(url):
        return ""
    try:
        found = wayback.snapshot(url)
    except wayback.Unavailable:
        return ""
    return (f". The Wayback Machine has a copy from {wayback.day(found[0])}: cite {found[1]} instead, "
            "with a quote from that copy") if found else ""

def poc_problems(pocs) -> list[str]:
    """Each contact's email and surname must appear on their contact page."""
    problems = []
    for p in pocs:
        page = fetch(p.contact_source_url)
        html = page[0].lower() if page else ""
        surname = (p.name.split() or [""])[-1].lower()
        if "@" not in p.email or p.email.lower() not in html:
            problems.append(f"POC {p.name}: {p.email} not found on {p.contact_source_url}")
        elif not surname or surname not in html:
            problems.append(f"POC {p.name}: name not found on {p.contact_source_url}")
    return problems

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
        # Code can't verify a possibility, but it can make sure one is never passed off as a fact
        worded_as_possibility = bool(POSSIBILITY.search(c.text))
        if c.kind == "possibility" and not worded_as_possibility:
            problems.append(f"Possibility must be worded 'It is possible that ...': '{c.text[:80]}'")
        if c.kind == "fact" and worded_as_possibility:
            problems.append(f"Claim is worded as a possibility but its kind is 'fact'; set kind to "
                            f"'possibility': '{c.text[:80]}'")
    if not any(c.kind == "fact" for c in brief.claims):
        problems.append("No claim of kind 'fact'; verify at least one fact alongside any possibilities")

    # 2 + 3. Every source is allowed, fetches, and its quote appears on the page
    blocked = blocked_domains()
    verified: set[str] = set()
    for s in brief.sources:
        if _is_blocked(_domain(s.url), blocked):
            problems.append(f"{s.id}: {_domain(s.url)} is on the blocked list (blocked_domains.txt); use another source")
            continue
        page = fetch(s.url)
        if page is None:
            problems.append(f"{s.id}: could not fetch {s.url}{archived_copy_hint(s.url)}")
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

    # 6. Outreach only: 3+ POCs, and every POC's name and email appear on their contact page
    if settings.OUTREACH and len(brief.pocs) < MIN_POCS:
        problems.append(f"Only {len(brief.pocs)} point(s) of contact; need {MIN_POCS}")
    if settings.OUTREACH:
        problems += poc_problems(brief.pocs)

    return (not problems, "\n".join(problems))
