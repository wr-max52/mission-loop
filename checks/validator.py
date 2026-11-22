"""Source gate. Code decides whether sources are valid; the model never does."""
import re
from pathlib import Path
from urllib.parse import urlparse
import httpx, trafilatura
from rapidfuzz import fuzz
import wayback
from contracts import Mission, ResearchBrief, POSSIBILITY

MIN_SUPPORT_DOMAINS = 3
QUOTE_MATCH = 90                           # rapidfuzz partial_ratio threshold
MIN_QUOTE_CHARS = 20
MAX_TEXT_CHARS = 300_000
BLOCKED_FILE = Path("blocked_domains.txt")  # one domain per line; subdomains are blocked too
HEADERS = {"User-Agent": "Mozilla/5.0 (MissionLoop source validator)"}
_cache: dict[str, tuple[str, str]] = {}    # successful fetches only; failures may be transient
CACHE_PAGES = 50                           # the loop runs for days: keep only the latest pages

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
    if len(_cache) >= CACHE_PAGES:
        del _cache[next(iter(_cache))]     # oldest first
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

EMPTY_COUNTERPOINT = ("Counterpoint is empty; give the strongest case against the thesis, from a source if you "
                      "found one, else as the objection a skeptic would raise")

def support_domains(sources) -> set[str]:
    """The websites among these sources that support the thesis."""
    return {_domain(s.url) for s in sources if s.stance == "supports"}

def claim_problems(claims, ids: set[str]) -> list[str]:
    """What is wrong with the claims, each problem once, naming the claims (numbered from 1) it applies to:
    fifty claims citing a missing source is one fix, not fifty."""
    found: dict[tuple[str, str], list[int]] = {}       # (said of one claim, said of several) -> claim numbers
    listed = ", ".join(sorted(ids)) or "none"
    for n, c in enumerate(claims, 1):
        if not c.source_ids:
            found.setdefault(("cites no source", "cite no source"), []).append(n)
        for sid in c.source_ids:
            if sid not in ids:
                found.setdefault((f"cites unknown source id '{sid}'", f"cite unknown source id '{sid}'"), []).append(n)
        worded_as_possibility = bool(POSSIBILITY.search(c.text))
        if c.kind == "possibility" and not worded_as_possibility:
            found.setdefault(("is a possibility, so must be worded 'It is possible that ...'",
                              "are possibilities, so must be worded 'It is possible that ...'"), []).append(n)
        if c.kind == "fact" and worded_as_possibility:
            found.setdefault(("is worded as a possibility but its kind is 'fact'; set kind to 'possibility'",
                              "are worded as possibilities but their kind is 'fact'; set kind to 'possibility'"),
                             []).append(n)
    problems = []
    for (one, several), ns in found.items():
        who = f"Claim {ns[0]} {one}" if len(ns) == 1 else f"Claims {', '.join(map(str, ns))} {several}"
        if "source" in one:
            who += f"; cite only the brief's source ids ({listed})"
        problems.append(who)
    if not any(c.kind == "fact" for c in claims):
        problems.append("No claim of kind 'fact'; verify at least one fact alongside any possibilities")
    return problems

def source_problem(s, blocked: set[str] | None = None, archive: bool = False) -> tuple[str | None, str]:
    """Whether one source holds up: (None, url) if its quote is on its page, else (what is wrong, url).
    With archive=True a page that can't be fetched is checked against its Wayback copy, and the url
    returned is that copy's address when its quote is there."""
    blocked = blocked_domains() if blocked is None else blocked
    if _is_blocked(_domain(s.url), blocked):
        return f"{s.id}: {_domain(s.url)} is on the blocked list (blocked_domains.txt); use another source", s.url
    url, page = s.url, fetch(s.url)
    if page is None and archive and not wayback.original(s.url):
        try:
            found = wayback.snapshot(s.url)
        except wayback.Unavailable:
            found = None
        if found and (copy := fetch(found[1])) is not None and _quote_score(s.quote, copy[1]) >= QUOTE_MATCH:
            return None, found[1]          # the live page is gone or refuses us; the archived copy says it
    if page is None:
        return f"{s.id}: could not fetch {s.url}{archived_copy_hint(s.url)}", url
    if not _norm(page[1]):
        return f"{s.id}: no readable text at {s.url}", url
    if len(_norm(s.quote)) < MIN_QUOTE_CHARS:
        return f"{s.id}: quote is too short to verify; copy a full sentence", url
    score = _quote_score(s.quote, page[1])
    if score < QUOTE_MATCH:
        hint = closest_sentence(s.quote, page[1])
        return (f"{s.id}: quote not found on {s.url} (best match {score:.0f}%)"
                + (f". Closest sentence actually on the page: \"{hint}\"" if hint else ""), url)
    return None, url

def _quote_score(quote: str, page_text: str) -> float:
    return fuzz.partial_ratio(_norm(quote), _norm(page_text)[:MAX_TEXT_CHARS])

def validate(m: Mission, brief: ResearchBrief) -> tuple[bool, str]:
    problems: list[str] = []
    ids = {s.id for s in brief.sources}

    # 1. Every claim cites real source ids, and a possibility is never passed off as a fact
    problems += claim_problems(brief.claims, ids)

    # 2 + 3. Every source is allowed, fetches, and its quote appears on the page
    blocked = blocked_domains()
    verified: set[str] = set()
    for s in brief.sources:
        problem, _ = source_problem(s, blocked)
        if problem:
            problems.append(problem)
        else:
            verified.add(s.id)

    # 4. At least 3 distinct domains among verified supporting sources
    domains = support_domains([s for s in brief.sources if s.id in verified])
    if len(domains) < MIN_SUPPORT_DOMAINS:
        problems.append(f"Only {len(domains)} verified supporting domain(s); need {MIN_SUPPORT_DOMAINS}")

    # 5. A counterpoint. Research must look for the case against the thesis, but a broad thesis often has no
    #    source that answers it, so a contradicting source is optional: any it cites is checked like the rest
    #    (above), and without one the counterpoint is shown as unsourced.
    if not brief.counterpoint.strip():
        problems.append(EMPTY_COUNTERPOINT)

    problems = list(dict.fromkeys(problems))   # each problem once: 100 claims citing 's9' is one fix
    return (not problems, "\n".join(problems))
