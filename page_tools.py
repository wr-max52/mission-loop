"""Local MCP server that lets the research agent search a whole page, follow its links and look at
the Wayback Machine's copies of it.

Pages are read the way the source validator reads them, so a sentence found here passes the quote
check. URLs come from the model and from web pages, so only public addresses are ever requested.
A live page that can't be read is swapped for its latest Wayback copy, and the result says which
address to cite, so the model never has to think of the archive itself.
The Toolbox starts this through the "pages" entry in mcp.json.
"""
import ipaddress, math, re, socket
from urllib.parse import urldefrag, urljoin, urlparse
import httpx, lxml.html
from lxml import etree
from mcp.server.mcpserver import MCPServer
import wayback
from checks import validator

MAX_BYTES = 5_000_000
MAX_REDIRECTS = 10
MAX_SENTENCES = 5
MAX_SENTENCE_CHARS = 600
MAX_LINKS = 30
MAX_LINK_TEXT = 120
MAX_URL_CHARS = 300
STOPWORDS = frozenset("a an and are as at be by did does for from had has have in is it of on or that "
                      "the this to was were what when where who why with".split())
NOT_SOURCES = ("apps.apple.com", "bsky.app", "facebook.com", "instagram.com", "linkedin.com",
               "pinterest.com", "play.google.com", "reddit.com", "t.me", "threads.net", "tiktok.com",
               "twitter.com", "whatsapp.com", "x.com", "youtube.com")   # share buttons and app badges

server = MCPServer("pages")
_pages: dict[str, tuple[str, str]] = {}    # successful reads, so find and links share one download

class CannotRead(Exception):
    def __init__(self, reason: str, final: bool = False):
        super().__init__(reason)
        self.final = final                 # never look for an archived copy (blocked site, private address)

def _check_address(url: str) -> None:
    """Raise CannotRead unless every address the host resolves to is on the public internet.
    A name that doesn't resolve may be a site that has gone, so its Wayback copy is still worth a look;
    a private or local address never is."""
    not_public = CannotRead("it is not a reachable public web address", final=True)
    try:
        parts = urlparse(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise not_public
        found = socket.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)
    except ValueError:
        raise not_public
    except OSError:
        raise CannotRead("it is not a reachable public web address (the name does not resolve)")
    for *_, address in found:
        ip = ipaddress.ip_address(address[0].split("%")[0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not ip.is_global or ip.is_multicast:
            raise not_public
    if not found:
        raise not_public

def _download(url: str) -> str:
    """Fetch HTML, checking the address again at every redirect."""
    for _ in range(MAX_REDIRECTS + 1):
        _check_address(url)
        with httpx.stream("GET", url, follow_redirects=False, timeout=20, headers=validator.HEADERS) as r:
            if r.has_redirect_location:
                url = urljoin(url, r.headers["location"])
                continue
            if r.status_code != 200:
                raise CannotRead(f"the site answered with status {r.status_code}")
            body = bytearray()
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise CannotRead("the page is too large")
            return body.decode(r.encoding or "utf-8", errors="replace")
    raise CannotRead("it redirects too many times")

def _page(url: str) -> tuple[str, str]:
    """Return (raw_html, readable_text), or raise CannotRead with a reason the model can use."""
    if validator._is_blocked(validator._domain(url), validator.blocked_domains()):
        raise CannotRead("its domain is on the blocked list", final=True)
    if url not in _pages:
        try:
            html = _download(wayback.raw(url))
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:
            raise CannotRead(f"it could not be loaded ({type(e).__name__})")
        _pages[url] = (html, validator.readable_text(html))
    return _pages[url]

def _read(url: str) -> tuple[str, str, str, str]:
    """Like _page, but a live page that can't be read is replaced by its latest Wayback copy.
    Returns (url to cite, raw_html, readable_text, note telling the model what happened)."""
    try:
        return (url, *_page(url), "")
    except CannotRead as e:
        if e.final or wayback.original(url):
            raise
        try:
            found = wayback.snapshot(url)
            html, text = _page(found[1]) if found else ("", "")
        except (wayback.Unavailable, CannotRead):
            found = None
        if not found:
            raise
        return (found[1], html, text, f"The live page could not be read ({e}), so this is the Wayback Machine's "
                f"copy from {wayback.day(found[0])}. Cite it with this url: {found[1]}\n")

def _words(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", validator._norm(text))
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words}

def _terms(query: str) -> set[str]:
    return _words(query) - STOPWORDS

def _same_site(a: str, b: str) -> bool:
    return a == b or a.endswith("." + b) or b.endswith("." + a)

def _cannot_cite(url: str, reason: object) -> str:
    return f"CANNOT CITE {url}: {reason}. Use a different source."

@server.tool()
def find_in_page(url: str, query: str) -> str:
    """Search the full text of a web page and return the sentences that match, copied exactly.

    Use this after fetch, because fetch shows only the start of a long page. Returned sentences can
    be used as source quotes. If the result says CANNOT CITE, do not use that page as a source.
    """
    try:
        url, _, text, note = _read(url)
    except CannotRead as e:
        return _cannot_cite(url, e)
    if not text:
        return _cannot_cite(url, "it has no readable text")
    terms = _terms(query)
    if not terms:
        return "ERROR: query needs at least one meaningful word."
    needed = math.ceil(len(terms) / 2)
    scored = [(len(terms & _words(s)), -i, s) for i, s in enumerate(validator.sentences(text))]
    matches = sorted((m for m in scored if m[0] >= needed), reverse=True)[:MAX_SENTENCES]
    if not matches:
        return f"{note}The page is readable ({len(text)} characters), but no sentence matches '{query}'. Try other words."
    lines = [f"{n}. {m[2][:MAX_SENTENCE_CHARS]}" for n, m in enumerate(matches, 1)]
    return (f"{note}The page is readable ({len(text)} characters). Exact sentences matching '{query}':\n"
            + "\n".join(lines))

@server.tool()
def page_links(url: str, about: str = "") -> str:
    """List the links a web page makes to other websites, with their link text.

    Use this to go from an article to what it cites: a bill, court filing, vote record, report or
    rebuttal. Optional: set about to words that the link text or address should contain.
    """
    try:
        url, html, _, note = _read(url)
        tree = lxml.html.fromstring(html)
    except CannotRead as e:
        return _cannot_cite(url, e)
    except (etree.ParserError, ValueError):
        return f"ERROR: could not read the links on {url}."
    host = validator._domain(url)
    base = wayback.original(url) or url    # an archived page's own links point at its original site
    blocked = validator.blocked_domains()
    wanted = _terms(about)
    links: dict[str, str] = {}
    for a in tree.xpath("//a[@href][not(ancestor::nav or ancestor::header or ancestor::footer or ancestor::aside)]"):
        label = " ".join(a.text_content().split())[:MAX_LINK_TEXT]
        try:
            target = urldefrag(urljoin(base, a.get("href").strip())).url
            parts = urlparse(target)
            other = (parts.hostname or "").removeprefix("www.")
        except ValueError:
            continue
        if (not label or parts.scheme not in ("http", "https") or not other or len(target) > MAX_URL_CHARS
                or _same_site(other, host) or validator._is_blocked(other, blocked)
                or any(_same_site(other, site) for site in NOT_SOURCES)):
            continue
        if wanted and not wanted & _words(f"{label} {target}"):
            continue
        links.setdefault(target, label)
    if not links:
        return f"{note}No matching links to other websites found on {url}."
    shown = list(links.items())[:MAX_LINKS]
    return f"{note}Links from {url} to other websites:\n" + "\n".join(f"- {label}: {target}" for target, label in shown)

@server.tool()
def wayback_versions(url: str, date: str = "") -> str:
    """List the Wayback Machine's archived copies of a web page, to read a page that is gone or to see
    whether a page has changed what it says over time.

    Optional: set date (YYYY-MM-DD) to get the copy closest to that day. Every address returned works
    like a live page: read it with fetch or find_in_page, and cite that address as the source url.
    """
    page = wayback.original(url) or url
    if validator._is_blocked(validator._domain(page), validator.blocked_domains()):
        return _cannot_cite(page, "its domain is on the blocked list")
    when = wayback.as_timestamp(date)
    try:
        versions = wayback.history(page)
        closest = wayback.snapshot(page, when) if when else None
    except wayback.Unavailable as e:
        return f"ERROR: the Wayback Machine could not be reached ({e}). Try again later or use another source."
    if not versions and not closest:
        return f"The Wayback Machine has no archived copy of {page}."
    lines = [f"Archived copies of {page}, oldest first. Each one says something different from the copy before it:"]
    lines += [f"- {wayback.day(ts)}: {copy}" for ts, copy in versions]
    if closest:
        lines.append(f"Closest to {date}: {wayback.day(closest[0])}: {closest[1]}")
    elif date:
        lines.append(f"(date '{date}' was not understood or has no copy; use YYYY-MM-DD)")
    lines.append("To see what changed, run find_in_page with the same words on an old copy and on the live page.")
    return "\n".join(lines)

if __name__ == "__main__":
    server.run()
