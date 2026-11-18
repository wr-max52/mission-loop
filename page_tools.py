"""Local MCP server that lets the research agent search a whole page and follow its links.

Pages are read the way the source validator reads them, so a sentence found here passes the quote
check. URLs come from the model and from web pages, so only public addresses are ever requested.
The Toolbox starts this through the "pages" entry in mcp.json.
"""
import ipaddress, math, re, socket
from urllib.parse import urldefrag, urljoin, urlparse
import httpx, lxml.html
from lxml import etree
from mcp.server.mcpserver import MCPServer
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
    pass

def _is_public(url: str) -> bool:
    """True only if every address the host resolves to is on the public internet."""
    try:
        parts = urlparse(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return False
        found = socket.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)
    except (OSError, ValueError):
        return False
    for *_, address in found:
        ip = ipaddress.ip_address(address[0].split("%")[0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not ip.is_global or ip.is_multicast:
            return False
    return bool(found)

def _download(url: str) -> str:
    """Fetch HTML, checking the address again at every redirect."""
    for _ in range(MAX_REDIRECTS + 1):
        if not _is_public(url):
            raise CannotRead("it is not a reachable public web address")
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
        raise CannotRead("its domain is on the blocked list")
    if url not in _pages:
        try:
            html = _download(url)
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as e:
            raise CannotRead(f"it could not be loaded ({type(e).__name__})")
        _pages[url] = (html, validator.readable_text(html))
    return _pages[url]

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
        text = _page(url)[1]
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
        return f"The page is readable ({len(text)} characters), but no sentence matches '{query}'. Try other words."
    lines = [f"{n}. {m[2][:MAX_SENTENCE_CHARS]}" for n, m in enumerate(matches, 1)]
    return f"The page is readable ({len(text)} characters). Exact sentences matching '{query}':\n" + "\n".join(lines)

@server.tool()
def page_links(url: str, about: str = "") -> str:
    """List the links a web page makes to other websites, with their link text.

    Use this to go from an article to what it cites: a bill, court filing, vote record, report or
    rebuttal. Optional: set about to words that the link text or address should contain.
    """
    try:
        html = _page(url)[0]
        tree = lxml.html.fromstring(html)
    except CannotRead as e:
        return _cannot_cite(url, e)
    except (etree.ParserError, ValueError):
        return f"ERROR: could not read the links on {url}."
    host = validator._domain(url)
    blocked = validator.blocked_domains()
    wanted = _terms(about)
    links: dict[str, str] = {}
    for a in tree.xpath("//a[@href][not(ancestor::nav or ancestor::header or ancestor::footer or ancestor::aside)]"):
        label = " ".join(a.text_content().split())[:MAX_LINK_TEXT]
        try:
            target = urldefrag(urljoin(url, a.get("href").strip())).url
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
        return f"No matching links to other websites found on {url}."
    shown = list(links.items())[:MAX_LINKS]
    return f"Links from {url} to other websites:\n" + "\n".join(f"- {label}: {target}" for target, label in shown)

if __name__ == "__main__":
    server.run()
