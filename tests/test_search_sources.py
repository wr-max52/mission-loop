"""Web search sources (#106): every source answers in one shape, an outage falls through to the next and rests that
source, no results is an answer, DuckDuckGo, GDELT and Wikipedia need no key, and every agent works with the tools
whichever source answers. No network: every source is a fake."""
import json
import httpx, pytest
from ollama import Message
import search_sources, search_status, settings
from agents import assistant, finder, llm, researcher
from contracts import Mission

URL = {"brave": "https://brave.example.org/a", "tavily": "https://tavily.example.org/a", "exa": "https://exa.example.org/a",
       "serper": "https://serper.example.org/a", "ddgs": "https://ddg.example.org/a", "gdelt": "https://gdelt.example.org/a"}

def body_for(host: str, path: str) -> dict:
    """Each source's real answer shape, with one result."""
    if "brave" in host:
        row = {"title": "Brave result", "url": URL["brave"], "description": "<strong>A</strong> snippet", "age": "2 days ago"}
        return {"results": [row]} if path.endswith("/news/search") else {"web": {"results": [row]}}
    if "tavily" in host:
        return {"results": [{"title": "Tavily result", "url": URL["tavily"], "content": "snippet"}]}
    if "exa" in host:
        return {"results": [{"title": "Exa result", "url": URL["exa"], "highlights": ["a", "b"], "publishedDate": "2026-11-01"}]}
    if "serper" in host:
        row = {"title": "Serper result", "link": URL["serper"], "snippet": "snippet", "date": "1 day ago"}
        return {"news": [row]} if path.endswith("/news") else {"organic": [row]}
    if "gdelt" in host:
        return {"articles": [{"title": "GDELT result", "url": URL["gdelt"], "domain": "gdelt.example.org", "seendate": "20261101"}]}
    if "wikipedia" in host:
        return {"query": {"search": [{"title": "Strategic Petroleum Reserve", "snippet": 'The <span class="m">reserve</span>'}]}}
    raise AssertionError(host)

class Net:
    """A fake network: each source answers with a status (200 by default) and its shape; records what was asked."""
    def __init__(self, monkeypatch, **status):
        self.status, self.asked, self.ddg = status, [], []
        monkeypatch.setattr(search_sources, "TRANSPORT", httpx.MockTransport(self.handle))
        monkeypatch.setattr(search_sources, "DDG", self.duck)
        monkeypatch.setattr(search_sources, "DDG_GAP", 0)

    def source(self, host):
        return next((s for s in ("brave", "tavily", "exa", "serper", "gdelt", "wikipedia") if s in host), host)

    def handle(self, request):
        name = self.source(request.url.host)
        self.asked.append((name, request))
        code = self.status.get(name, 200)
        if code == "down":
            raise httpx.ConnectError("no route")
        if code == "empty":
            return httpx.Response(200, json={"web": {"results": []}, "results": [], "organic": [], "articles": []})
        return httpx.Response(code, json=body_for(request.url.host, request.url.path) if code == 200 else {})

    def duck(self, kind, query, count, timelimit):
        self.ddg.append((kind, query, count, timelimit))
        what = self.status.get("ddgs", 200)
        if what == "rate":
            raise type("RatelimitException", (Exception,), {})("202 Ratelimit")
        if what == "empty":
            raise type("DDGSException", (Exception,), {})("No results found.")
        return [{"title": "Duck result", "href": URL["ddgs"], "body": "snippet"}]

def keys(*sources):
    for s in sources:
        settings.save_search_key(f"{s}-key", s)

def asked(net):
    return [name for name, _ in net.asked]

# ---------- each source, one shape ----------

@pytest.mark.parametrize("source", ["brave", "tavily", "exa", "serper"])
@pytest.mark.parametrize("kind", ["web", "news"])
def test_each_keyed_source_answers_in_one_shape(monkeypatch, source, kind):
    Net(monkeypatch)
    keys(source)
    settings.set_search_on("ddgs", False)
    settings.set_search_on("gdelt", False)
    rows, used, notes = search_sources.search(kind, "petroleum reserve", 5, "week")
    assert used == source and notes == [] and rows[0]["url"] == URL[source]
    assert set(rows[0]) == {"title", "url", "snippet", "date"} and "<" not in rows[0]["snippet"]

def test_the_key_and_freshness_reach_each_source(monkeypatch):
    net = Net(monkeypatch)
    for source, header, value, fresh in (("brave", "x-subscription-token", "brave-key", "freshness=pw"),
                                         ("tavily", "authorization", "Bearer tavily-key", '"time_range":"week"'),
                                         ("exa", "x-api-key", "exa-key", "startPublishedDate"),
                                         ("serper", "x-api-key", "serper-key", '"tbs":"qdr:w"')):
        keys(source)
        search_sources.RUN[source]("web", "q", 3, "week", settings.search_key(source))
        request = net.asked[-1][1]
        assert request.headers[header] == value
        assert fresh in (str(request.url) + request.content.decode())

def test_freshness_takes_words_and_brave_codes():
    assert [search_sources.freshness(f) for f in ("day", "pw", "Month", "py", "", "soon")] == \
        ["day", "week", "month", "year", "", ""]

def test_free_sources_need_no_key(monkeypatch):
    net = Net(monkeypatch)
    rows, used, _ = search_sources.search("web", "q", 5, "month")
    assert used == "ddgs" and rows[0]["url"] == URL["ddgs"] and net.ddg == [("web", "q", 5, "m")]
    net.status["ddgs"] = "rate"
    rows, used, _ = search_sources.search("news", "q", 5)
    assert used == "gdelt" and rows[0]["url"] == URL["gdelt"]

# ---------- falling through ----------

def test_an_outage_falls_through_and_the_source_rests(monkeypatch):
    net = Net(monkeypatch, brave=402, tavily=429)
    keys("brave", "tavily")
    rows, used, notes = search_sources.search("web", "q")
    assert used == "ddgs" and asked(net) == ["brave", "tavily"]
    assert notes == ["Brave Search is out of prepaid credit, so Brave Search was skipped",
                     "Tavily is over its rate limit, so Tavily was skipped"]
    assert search_sources.resting("brave")["reason"] == "Brave Search is out of prepaid credit"
    net.asked.clear()
    search_sources.search("web", "again")
    assert asked(net) == []                                     # both rest: DuckDuckGo answers straight away

def test_a_source_that_answers_again_wakes(monkeypatch):
    net = Net(monkeypatch, brave="down")
    keys("brave")
    assert search_sources.search("web", "q")[1] == "ddgs"
    search_sources.wake("brave")                              # a human saved a new key
    net.status["brave"] = 200
    assert search_sources.search("web", "q")[1] == "brave" and not search_sources.resting("brave")

def test_no_results_is_an_answer_not_an_outage(monkeypatch):
    net = Net(monkeypatch, brave="empty")
    keys("brave")
    rows, used, _ = search_sources.search("web", "q")
    assert rows == [] and used == "brave" and net.ddg == []
    out = search_sources.run_search("web", "q")
    assert "none found" in out and not out.startswith("ERROR")

def test_every_source_down_is_one_outage_with_each_reason(monkeypatch):
    Net(monkeypatch, brave=401, ddgs="rate")
    keys("brave")
    with pytest.raises(search_sources.AllDown) as e:
        search_sources.run_search("web", "q")
    outage = search_status.classify(f"ERROR: Error executing tool web_search: {e.value}")
    assert outage.kind == "all"
    assert outage.reason == "Brave Search refused the key; DuckDuckGo is over its rate limit"

def test_nothing_switched_on_says_so(monkeypatch):
    Net(monkeypatch)
    settings.set_search_on("ddgs", False)
    with pytest.raises(search_sources.AllDown, match="no search source is switched on"):
        search_sources.run_search("web", "q")

def test_duckduckgo_is_used_politely(monkeypatch):
    net = Net(monkeypatch)
    monkeypatch.setattr(search_sources, "DDG_PER_MINUTE", 2)
    search_sources._ddg_times.clear()
    search_sources.search("web", "a"), search_sources.search("web", "b")
    with pytest.raises(search_sources.AllDown, match="DuckDuckGo is over its rate limit"):
        search_sources.search("web", "c")
    assert len(net.ddg) == 2                                   # the third never reached it

# ---------- the tools' answers ----------

def test_results_name_the_source_and_carry_each_address(monkeypatch):
    Net(monkeypatch, brave=402)
    keys("brave")
    out = search_sources.run_search("web", "petroleum reserve", 3)
    assert out.startswith('Web results from DuckDuckGo for "petroleum reserve":')
    assert f"URL: {URL['ddgs']}" in out and "Brave Search is out of prepaid credit" in out

def test_wiki_search_is_its_own_tool_and_says_it_is_background(monkeypatch):
    Net(monkeypatch)
    out = search_sources.wiki("strategic reserve")
    assert "URL: https://en.wikipedia.org/wiki/Strategic_Petroleum_Reserve" in out and "<span" not in out
    assert "background, not proof" in out

def test_a_wikipedia_failure_is_text_not_an_outage(monkeypatch):
    Net(monkeypatch, wikipedia="down")
    out = search_sources.wiki("x")
    assert out.startswith("Wikipedia search didn't work") and search_status.classify(out) is None

def test_nothing_personal_goes_out(monkeypatch):
    net = Net(monkeypatch)
    search_sources.wiki("x")
    assert net.asked[0][1].headers["user-agent"] == search_sources.USER_AGENT
    assert "mission-loop" not in search_sources.USER_AGENT and "github" not in search_sources.USER_AGENT

# ---------- every agent works with the tools, whichever source answers ----------

class SearchBox:
    """The search server's three tools, answered by search_sources (as search_tools.py does), plus fetch."""
    def __init__(self, enabled, env=None):
        self.tools = [{"type": "function", "function": {"name": n}}
                      for n in ("web_search", "news_search", "wiki_search", "fetch")]
        self.server = {"web_search": "search", "news_search": "search", "wiki_search": "search", "fetch": "fetch"}
        self.skipped = {}
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): pass
    async def call(self, name, args):
        try:
            if name == "wiki_search":
                return search_sources.wiki(args.get("query"))
            if name in ("web_search", "news_search"):
                return search_sources.run_search(name.split("_")[0], args.get("query"), args.get("count", 10),
                                                 args.get("freshness", ""))
        except search_sources.AllDown as e:
            return f"ERROR: Error executing tool {name}: {e}"
        return "a page"

def call(name, **args):
    return Message(role="assistant", content="", tool_calls=[
        Message.ToolCall(function=Message.ToolCall.Function(name=name, arguments=args))])

def replies(monkeypatch, items):
    it = iter(items)
    async def fake_chat(messages, tools=None, format=None, model=None):
        return next(it)
    monkeypatch.setattr(llm, "chat", fake_chat)

SOURCES = ["brave", "tavily", "exa", "serper", "ddgs"]

def only(source):
    for other in SOURCES:
        if other != source:
            settings.set_search_on(other, False)
    if source != "ddgs":
        keys(source)

@pytest.mark.parametrize("source", SOURCES)
async def test_research_gets_results_from_any_source(monkeypatch, source):
    Net(monkeypatch)
    only(source)
    replies(monkeypatch, [call("web_search", query="a"), call("wiki_search", query="b"), Message(role="assistant", content="DONE")])
    ev = await researcher.run_gather(Mission(slug="s", topic="t", attempts=1), toolbox_factory=SearchBox)
    answers = [m["content"] for m in ev.messages if m.get("role") == "tool"]
    assert f"URL: {URL[source]}" in answers[0] and "wikipedia.org/wiki/" in answers[1] and not ev.search_down

@pytest.mark.parametrize("source", SOURCES)
async def test_the_finder_keeps_leads_from_any_source(monkeypatch, source):
    Net(monkeypatch)
    only(source)
    idea = finder.MissionIdeas(missions=[{"topic": "Did the reserve deal change anything?", "objective": "Check it.",
                                          "source_url": URL[source]}])
    replies(monkeypatch, [call("web_search", query="a"), Message(role="assistant", content="DONE"),
                          Message(role="assistant", content=idea.model_dump_json())])
    kept = await finder.run_finder("Who stores oil?", [], None, toolbox_factory=SearchBox)
    assert [k.source_url for k in kept] == [URL[source]]       # the "URL must be in the results" rule holds

async def test_every_source_down_stops_searching_but_not_the_step(monkeypatch):
    Net(monkeypatch, ddgs="rate")
    replies(monkeypatch, [call("web_search", query="a"), call("news_search", query="b"), call("wiki_search", query="c"),
                          Message(role="assistant", content="DONE")])
    ev = await researcher.run_gather(Mission(slug="s", topic="t", attempts=1), toolbox_factory=SearchBox)
    answers = [m["content"] for m in ev.messages if m.get("role") == "tool"]
    assert answers[0].startswith("Web search is down (DuckDuckGo is over its rate limit)")
    assert answers[1].startswith("Not run: Web search is down")
    assert "wikipedia.org/wiki/" in answers[2]                 # Wikipedia is its own tool and still answers
    assert ev.finished and ev.search_down

def test_researcher_chat_has_search_with_no_key():
    assert "search" in assistant.servers()
