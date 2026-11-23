"""Web search over an ordered list of sources (#106), in code (no model). search_tools.py serves it as the agents'
web_search, news_search and wiki_search tools, so no agent knows which source answered.

Each search tries the sources that are on (settings.search_sources_on), in order. An outage (out of credit, key
refused, rate limited, unreachable: the kinds in search_status.py) moves on to the next source and rests that one
for a while (data/search_sources.json, shared by every search process); "no results" is an answer, not an outage.
Only when every source is down does the search fail, with each source's reason, and the search loop's own outage
handling (#105) takes over. Every source answers in one shape: title, url, snippet, date.

DuckDuckGo goes through the open-source ddgs library: free and keyless but unofficial, so it is used politely (a gap
between searches, a cap a minute) and, when it is limited, it rests and says so; nothing tries to get past a block.
"""
import json, re, time
from html import unescape
from pathlib import Path
import httpx
import search_status, settings

TRANSPORT: httpx.BaseTransport | None = None   # tests swap in a mock; None is the real network
DDG = None                                 # tests swap in their own ddgs stand-in: fn(kind, query, count, timelimit)
STATE = Path("data/search_sources.json")   # source -> {"until": epoch seconds, "reason", "kind"} while it rests
TIMEOUT = 20                               # seconds a source may take
MAX_COUNT = 20
SNIPPET_CHARS = 300
USER_AGENT = "research-desk/1.0 (python-httpx)"   # Wikimedia asks for a User-Agent; a generic one, nothing personal
REST = {"credit": 30 * 60, "key": 30 * 60, "rate": 5 * 60, "unreachable": 5 * 60, "error": 5 * 60}   # seconds
DDG_GAP = 2.0                              # seconds between DuckDuckGo searches from one process
DDG_PER_MINUTE = 10                        # at most this many DuckDuckGo searches a minute from one process
GDELT_GAP = 5.0                            # GDELT asks for at most one request every 5 seconds
FRESHNESS = {"day": "day", "pd": "day", "d": "day", "week": "week", "pw": "week", "w": "week",
             "month": "month", "pm": "month", "m": "month", "year": "year", "py": "year", "y": "year"}

class SourceDown(Exception):
    def __init__(self, outage: search_status.Outage):
        super().__init__(outage.reason)
        self.outage = outage

class AllDown(Exception):
    """Every source that is on is down (or none is on). The message lists why, source by source."""

def _name(source: str) -> str:
    return settings.SEARCH_SOURCES[source]["name"]

def _down(source: str, kind: str) -> SourceDown:
    return SourceDown(search_status.Outage(kind, _name(source)))

def _client() -> httpx.Client:
    return httpx.Client(transport=TRANSPORT, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})

def _send(source: str, method: str, url: str, **kw) -> dict:
    """One request; an outage raises SourceDown with its kind."""
    try:
        with _client() as client:
            r = client.request(method, url, **kw)
    except httpx.HTTPError:
        raise _down(source, "unreachable")
    if r.status_code == 402:
        raise _down(source, "credit")
    if r.status_code in (401, 403):
        raise _down(source, "key")
    if r.status_code == 429:
        raise _down(source, "rate")
    if r.status_code >= 400:
        raise _down(source, "unreachable" if r.status_code >= 500 else "error")
    try:
        return r.json()
    except ValueError:
        raise _down(source, "error")

def _clean(text) -> str:
    text = unescape(re.sub(r"<[^>]+>", "", str(text or "")))
    text = " ".join(text.split())
    return text if len(text) <= SNIPPET_CHARS else text[:SNIPPET_CHARS - 3].rstrip() + "..."

def _result(title, url, snippet="", date="") -> dict:
    return {"title": _clean(title) or str(url), "url": str(url or ""), "snippet": _clean(snippet), "date": str(date or "")}

# ---------- the sources ----------

def _brave(kind, query, count, fresh, key):
    params = {"q": query, "count": count}
    if fresh:
        params["freshness"] = "p" + fresh[0]   # pd, pw, pm, py
    data = _send("brave", "GET", f"{settings.SEARCH_SOURCES['brave']['url']}/{'news' if kind == 'news' else 'web'}/search",
                 params=params, headers={"X-Subscription-Token": key, "Accept": "application/json"})
    rows = data.get("results", []) if kind == "news" else (data.get("web") or {}).get("results", [])
    return [_result(r.get("title"), r.get("url"), r.get("description"), r.get("age") or r.get("page_age")) for r in rows]

def _tavily(kind, query, count, fresh, key):
    body = {"query": query, "max_results": count, "topic": "news" if kind == "news" else "general"}
    if fresh:
        body["time_range"] = fresh
    data = _send("tavily", "POST", f"{settings.SEARCH_SOURCES['tavily']['url']}/search", json=body,
                 headers={"Authorization": f"Bearer {key}"})
    return [_result(r.get("title"), r.get("url"), r.get("content"), r.get("published_date")) for r in data.get("results", [])]

def _exa(kind, query, count, fresh, key):
    body = {"query": query, "numResults": count, "type": "auto", "contents": {"highlights": True}}
    if kind == "news":
        body["category"] = "news"
    if fresh:
        days = {"day": 1, "week": 7, "month": 31, "year": 366}[fresh]
        body["startPublishedDate"] = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(time.time() - days * 86400))
    data = _send("exa", "POST", f"{settings.SEARCH_SOURCES['exa']['url']}/search", json=body, headers={"x-api-key": key})
    return [_result(r.get("title"), r.get("url"), " ".join(r.get("highlights") or []) or r.get("text"),
                    r.get("publishedDate")) for r in data.get("results", [])]

def _serper(kind, query, count, fresh, key):
    body = {"q": query, "num": count}
    if fresh:
        body["tbs"] = "qdr:" + fresh[0]
    data = _send("serper", "POST", f"{settings.SEARCH_SOURCES['serper']['url']}/{'news' if kind == 'news' else 'search'}",
                 json=body, headers={"X-API-KEY": key})
    rows = data.get("news" if kind == "news" else "organic", [])
    return [_result(r.get("title"), r.get("link"), r.get("snippet"), r.get("date")) for r in rows]

_ddg_times: list[float] = []

def _ddgs_call(kind, query, count, timelimit):
    from ddgs import DDGS                  # late: only searches that reach DuckDuckGo need it
    with DDGS(timeout=TIMEOUT) as d:
        return (d.news if kind == "news" else d.text)(query, max_results=count, timelimit=timelimit)

def _ddgs(kind, query, count, fresh, key):
    now = time.monotonic()
    _ddg_times[:] = [t for t in _ddg_times if now - t < 60]
    if len(_ddg_times) >= DDG_PER_MINUTE:  # our own cap: rest instead of hammering it
        raise _down("ddgs", "rate")
    if _ddg_times and now - _ddg_times[-1] < DDG_GAP:
        time.sleep(DDG_GAP - (now - _ddg_times[-1]))
    _ddg_times.append(time.monotonic())
    call = DDG or _ddgs_call
    try:
        rows = call(kind, query, count, fresh[0] if fresh else None)
    except Exception as e:                 # ddgs raises its own exceptions; read them by name, not import
        name, text = type(e).__name__, str(e).lower()
        if "no results" in text:
            return []
        if "ratelimit" in name.lower() or "202" in text or "ratelimit" in text:
            raise _down("ddgs", "rate")
        raise _down("ddgs", "unreachable")
    return [_result(r.get("title"), r.get("href") or r.get("url"), r.get("body"), r.get("date")) for r in rows or []]

_gdelt_last = [0.0]

def _gdelt(kind, query, count, fresh, key):
    wait = GDELT_GAP - (time.monotonic() - _gdelt_last[0])
    if wait > 0 and _gdelt_last[0]:
        time.sleep(wait)
    _gdelt_last[0] = time.monotonic()
    span = {"day": "1d", "week": "1w", "month": "1m"}.get(fresh, "3m")
    data = _send("gdelt", "GET", settings.SEARCH_SOURCES["gdelt"]["url"],
                 params={"query": query, "mode": "artlist", "format": "json", "maxrecords": count, "timespan": span,
                         "sort": "hybridrel"})
    return [_result(r.get("title"), r.get("url"), r.get("domain"), r.get("seendate")) for r in data.get("articles", [])]

RUN = {"brave": _brave, "tavily": _tavily, "exa": _exa, "serper": _serper, "ddgs": _ddgs, "gdelt": _gdelt}

# ---------- resting sources ----------

def _state() -> dict:
    try:
        data = json.loads(STATE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}

def _save_state(data: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(data))

def resting(source: str) -> dict | None:
    """{"until", "reason", "kind"} while a source rests after an outage, else None."""
    entry = _state().get(source)
    return entry if entry and entry.get("until", 0) > time.time() else None

def _rest(outage: search_status.Outage, source: str) -> None:
    data = _state()
    data[source] = {"until": time.time() + REST.get(outage.kind, 300), "reason": outage.reason, "kind": outage.kind}
    _save_state(data)

def _woke(source: str) -> None:
    data = _state()
    if data.pop(source, None) is not None:
        _save_state(data)

def wake(source: str) -> None:
    """A human changed this source (a new key, switched on): try it again at once."""
    _woke(source)

# ---------- searching ----------

def freshness(value: str) -> str:
    return FRESHNESS.get(str(value or "").strip().lower(), "")

def search(kind: str, query: str, count: int = 10, fresh: str = "") -> tuple[list[dict], str, list[str]]:
    """(results, the source that answered, notes on sources it skipped) for a "web" or "news" search. Raises AllDown
    when no source could answer."""
    count = max(1, min(int(count or 10), MAX_COUNT))
    fresh = freshness(fresh)
    notes, reasons = [], []
    sources = settings.search_sources_on(kind)
    if not sources:
        raise AllDown("no search source is switched on (Settings → Web search)")
    for source in sources:
        if rest := resting(source):
            reasons.append(rest["reason"])
            continue
        try:
            rows = RUN[source](kind, query, count, fresh, settings.search_key(source))
        except SourceDown as e:
            _rest(e.outage, source)
            reasons.append(e.outage.reason)
            notes.append(f"{e.outage.reason}, so {_name(source)} was skipped")
            continue
        _woke(source)
        return [r for r in rows if r["url"]][:count], source, notes
    raise AllDown("; ".join(reasons))

def as_text(kind: str, query: str, rows: list[dict], source: str, notes: list[str], fresh: str = "") -> str:
    """What a search tool answers the model with: which source, then each result."""
    head = f'{"News" if kind == "news" else "Web"} results from {_name(source)} for "{query}"'
    lines = [head + (":" if rows else ": none found. Try different words.")]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['title']}\n   URL: {r['url']}" + (f"\n   Date: {r['date']}" if r["date"] else "")
                     + (f"\n   {r['snippet']}" if r["snippet"] else ""))
    if notes:
        lines.append("(" + "; ".join(notes) + ".)")
    return "\n".join(lines)

def run_search(kind: str, query: str, count: int = 10, fresh: str = "") -> str:
    """A search, as the tool's answer. Every source down raises AllDown with search_status's marker, so the search
    loop knows it is an outage, not a bad query."""
    query = " ".join(str(query or "").split())
    if not query:
        return "Give a query: the words to search for."
    try:
        rows, source, notes = search(kind, query, count, fresh)
    except AllDown as e:
        raise AllDown(search_status.ALL_DOWN + str(e))
    return as_text(kind, query, rows, source, notes, freshness(fresh))

def wiki(query: str, count: int = 5) -> str:
    """Wikipedia's own search. Its failures are answered as text, never as an outage: web search still works."""
    query = " ".join(str(query or "").split())
    if not query:
        return "Give a query: the topic, name or term to look up."
    count = max(1, min(int(count or 5), 10))
    try:
        with _client() as client:
            r = client.get(settings.WIKI_API, params={"action": "query", "list": "search", "srsearch": query,
                                                     "srlimit": count, "format": "json", "utf8": 1})
        r.raise_for_status()
        rows = r.json().get("query", {}).get("search", [])
    except (httpx.HTTPError, ValueError) as e:
        return f"Wikipedia search didn't work ({type(e).__name__}). Use web_search instead."
    base = settings.WIKI_API.rsplit("/w/", 1)[0] + "/wiki/"
    lines = [f'Wikipedia articles for "{query}"' + (":" if rows else ": none found.")]
    for i, row in enumerate(rows, 1):
        title = row.get("title", "")
        lines.append(f"{i}. {title}\n   URL: {base}{title.replace(' ', '_')}\n   {_clean(row.get('snippet'))}")
    lines.append("(Wikipedia is background, not proof: fetch the sources an article cites and cite those.)")
    return "\n".join(lines)

def status() -> list[dict]:
    """Every source in order, for Settings → Web search: id, name, on, needs a key, has a key, resting reason."""
    out = []
    for source in settings.search_order():
        spec = settings.SEARCH_SOURCES[source]
        rest = resting(source)
        out.append({"id": source, "name": spec["name"], "kinds": spec["kinds"], "blurb": spec["blurb"],
                    "keyed": bool(spec["key_env"]), "key": bool(settings.search_key(source)),
                    "saved": settings.search_key_saved(source), "off": settings.search_switched_off(source),
                    "on": settings.search_on(source), "resting": rest["reason"] if rest else "",
                    "keys": spec["keys"], "env": spec["key_env"]})
    return out
