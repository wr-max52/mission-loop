"""Search outages (#105): a search error is sorted into a kind and said once, plainly; the search moves on and the
step finishes; the finder skips quietly; Setup and the top bar say it in one place; a working search clears it."""
from pathlib import Path
import httpx, pytest
from ollama import Message
from starlette.testclient import TestClient
import activity, agents, checkpoint, doctor, orchestrator, search_sources, search_status, settings, store, ui
from agents import finder, llm, researcher
from contracts import Evidence, Mission, State

CREDIT = ('ERROR: 402 { "type": "ErrorResponse", "error": { "status": 402, "detail": "Prepaid credit balance is '
          'insufficient for this request. Top up to continue." } }')

def call(name, **args):
    return Message(role="assistant", content="", tool_calls=[
        Message.ToolCall(function=Message.ToolCall.Function(name=name, arguments=args))])

DONE = Message(role="assistant", content="DONE")

class Box:
    """A toolbox whose web search is Brave's tools on the "search" server, like mcp.json's."""
    calls: list = []
    answers: list = []
    def __init__(self, enabled, env=None):
        names = ["brave_web_search", "brave_news_search", "fetch"]
        self.tools = [{"type": "function", "function": {"name": n}} for n in names]
        self.server = {"brave_web_search": "search", "brave_news_search": "search", "fetch": "fetch"}
        self.skipped = {}
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): pass
    async def call(self, name, args):
        Box.calls.append(name)
        if name == "fetch":
            return "a page"
        return Box.answers.pop(0) if Box.answers else "Title: x\nURL: https://ok.example.com/a"

def replies(monkeypatch, items):
    it = iter(items)
    async def fake_chat(messages, tools=None, format=None, model=None):
        return next(it)
    monkeypatch.setattr(llm, "chat", fake_chat)

@pytest.fixture(autouse=True)
def reset():
    Box.calls, Box.answers = [], []
    yield
    checkpoint.use(None)

# ---------- sorting an error ----------

@pytest.mark.parametrize("text, kind", [
    (CREDIT, "credit"),
    ("ERROR: Error executing tool brave_web_search: 402 Payment Required", "credit"),
    ('ERROR: 401 {"detail": "invalid subscription token"}', "key"),
    ("ERROR: 403 Forbidden", "key"),
    ("ERROR: 429 Too Many Requests", "rate"),
    ("ERROR: brave_web_search failed: fetch failed", "unreachable"),
    ("ERROR: brave_web_search failed: ConnectError('All connection attempts failed')", "unreachable"),
])
def test_a_search_error_is_sorted_into_its_kind(text, kind):
    assert search_status.classify(text).kind == kind

@pytest.mark.parametrize("text", ["ERROR: No web results found", "Title: a\nURL: https://a.example.com 402 words",
                                  "ERROR: bad argument: count must be 1-20"])
def test_no_results_and_working_answers_are_not_outages(text):
    assert search_status.classify(text) is None

def test_the_reason_is_one_plain_sentence():
    assert search_status.classify(CREDIT).reason == "Brave Search is out of prepaid credit"

# ---------- research ----------

async def test_research_searches_once_then_reads_what_it_has(monkeypatch):
    Box.answers = [CREDIT]
    replies(monkeypatch, [call("brave_web_search", query="a"), call("brave_news_search", query="b"),
                          call("fetch", url="https://known.example.com/page"), DONE])
    ev = await researcher.run_gather(Mission(slug="s", topic="t", objective="start at https://known.example.com/page",
                                             attempts=1), toolbox_factory=Box)
    assert Box.calls == ["brave_web_search", "fetch"]           # the news search was answered without running
    tool_answers = [m["content"] for m in ev.messages if m.get("role") == "tool"]
    assert tool_answers[0].startswith("Web search is down (Brave Search is out of prepaid credit)")
    assert tool_answers[1].startswith("Not run: Web search is down")
    assert "ErrorResponse" not in " ".join(tool_answers)        # the raw JSON never reaches the model
    assert ev.finished and ev.search_down == "Brave Search is out of prepaid credit"
    assert search_status.current()["kind"] == "credit"

async def test_a_rate_limit_gets_one_retry_before_it_counts(monkeypatch):
    Box.answers = ["ERROR: 429 Too Many Requests", "Title: x\nURL: https://ok.example.com/a"]
    replies(monkeypatch, [call("brave_web_search", query="a"), DONE])
    ev = await researcher.run_gather(Mission(slug="s", topic="t", objective="start at https://known.example.com/page",
                                             attempts=1), toolbox_factory=Box)
    assert Box.calls == ["brave_web_search", "brave_web_search"] and not ev.search_down
    assert search_status.current() is None

async def test_a_search_that_works_clears_the_outage(monkeypatch):
    search_status.record(search_status.Outage("credit"))
    replies(monkeypatch, [call("brave_web_search", query="a"), DONE])
    await researcher.run_gather(Mission(slug="s", topic="t", attempts=1), toolbox_factory=Box)
    assert search_status.current() is None

def test_the_attempt_finishes_and_history_names_the_outage(monkeypatch):
    m = Mission(slug="s", topic="t", state=State.RESEARCHING)
    store.save_mission(m)
    monkeypatch.setattr(agents, "gather", lambda m, now, resume=None: Evidence(
        messages=[{"role": "tool", "content": "x"}], attempt=1, search_down="Brave Search is out of prepaid credit"))
    note = orchestrator._gather(m, None)
    assert "web search was down: Brave Search is out of prepaid credit" in note and m.gathered
    monkeypatch.setattr(agents, "write_brief", lambda m, ev, now: (_ for _ in ()).throw(
        researcher.ResearchFailed("Only 1 verified supporting domain(s); need 3.")))
    note = orchestrator._write_brief(m, None)
    assert note == "attempt 1 failed validation (web search was down)"
    assert "Web search was down during this attempt: Brave Search is out of prepaid credit" in m.validator_notes

# ---------- the finder ----------

async def test_the_finder_says_search_is_down_not_that_it_found_nothing(monkeypatch):
    Box.answers = [CREDIT]
    replies(monkeypatch, [call("brave_web_search", query="a"), call("brave_web_search", query="b"), DONE])
    with pytest.raises(finder.SearchDown, match="web search is down \\(Brave Search is out of prepaid credit\\)"):
        await finder.run_finder("Who runs the Fed?", [], None, toolbox_factory=Box)

def test_the_finder_skips_quietly_while_search_is_down(monkeypatch, capsys):
    store.set_inspiration("Who runs the Fed?")
    runs = []
    def down(*args):
        runs.append(1)
        search_status.record(search_status.Outage("credit"))
        raise finder.SearchDown("Brave Search is out of prepaid credit")
    monkeypatch.setattr(agents, "find_missions", down)
    for _ in range(3):
        assert orchestrator.find_new_missions(None) == 0
    out = capsys.readouterr().out
    assert runs == [1]                                          # the next sweeps don't search at all
    assert out.count("finder skipped: web search is down (Brave Search is out of prepaid credit)") == 1
    assert "returned nothing" not in out and "mission finder failed" not in out

def test_the_finder_tries_again_after_a_while(monkeypatch):
    store.set_inspiration("Who runs the Fed?")
    search_status.record(search_status.Outage("credit"))
    monkeypatch.setattr(search_status, "FINDER_RETRY", 0)
    runs = []
    monkeypatch.setattr(agents, "find_missions", lambda *a: runs.append(1) or [])
    orchestrator.find_new_missions(None)
    assert runs == [1]

# ---------- Setup, the top bar, the step log ----------

def test_setup_shows_one_failing_check_with_the_fix(monkeypatch):
    settings.save_search_key("brave-key")
    assert "search_up" not in {c.key for c in doctor.checks()}
    search_status.record(search_status.Outage("credit"))
    down = [c for c in doctor.checks() if not c.ok and c.key.startswith("search")]
    assert [c.key for c in down] == ["search_up"]
    assert down[0].detail == "Web search is down: Brave Search is out of prepaid credit."
    assert "Top up" in down[0].fix

def only_brave(monkeypatch, status):
    """Brave alone is on, answering with this status."""
    settings.save_search_key("brave-key")
    settings.set_search_on("ddgs", False)
    monkeypatch.setattr(search_sources, "TRANSPORT", httpx.MockTransport(
        lambda r: httpx.Response(status, json={"web": {"results": []}})))

@pytest.mark.parametrize("status, reason", [(402, "Brave Search is out of prepaid credit"),
                                            (401, "Brave Search refused the key"),
                                            (429, "Brave Search is over its rate limit"), (200, None)])
def test_a_real_test_search_finds_a_spent_key(monkeypatch, status, reason):
    monkeypatch.setattr(doctor, "_probed", 0.0)
    only_brave(monkeypatch, status)
    search_status.record(search_status.Outage("unreachable"))
    found = doctor.probe_search()
    assert (found.reason if found else None) == reason
    assert (search_status.current() or {}).get("reason") == reason

def test_the_test_search_runs_at_most_every_ten_minutes(monkeypatch):
    asked = []
    monkeypatch.setattr(doctor, "_probed", 0.0)
    only_brave(monkeypatch, 200)
    monkeypatch.setattr(search_sources, "TRANSPORT", httpx.MockTransport(
        lambda r: asked.append(1) or httpx.Response(200, json={"web": {"results": []}})))
    doctor.probe_search(), doctor.probe_search()
    assert asked == [1]

def test_a_spent_key_is_saved_with_a_note(monkeypatch):
    monkeypatch.setattr(search_sources, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(402, json={})))
    ok, note = doctor.check_search_key("brave-key")
    assert ok and "out of prepaid credit" in note

def test_the_first_run_card_does_not_come_back_for_an_outage():
    found = [doctor.Check("models", "Models", True, "fine"), doctor.Check("search", "Key", True, "set"),
             doctor.Check("search_up", "Web search works", False, "down")]
    assert ui.setup_card(found) == ""

def test_the_top_bar_says_search_down_and_nothing_else_does(monkeypatch):
    monkeypatch.setattr(orchestrator, "LOCK_FILE", Path("data/loop.lock"))
    settings.save_search_key("brave-key")
    store.set_inspiration("A line")
    client = TestClient(ui.app)
    assert "Search down" not in client.get("/").text
    search_status.record(search_status.Outage("credit"))
    page = client.get("/").text
    assert page.count(">Search down</a>") == 1 and "Brave Search is out of prepaid credit" in page

def test_a_failed_step_logs_a_short_reason():
    assert activity.reason(ConnectionError("[Errno 61] Connection refused to https://api.example.com/x")) \
        == "couldn't reach api.example.com"
    assert activity.reason(TimeoutError()) == "a server took too long to answer"
    assert activity.reason(finder.SearchDown("Brave Search is out of prepaid credit")) \
        == "web search is down (Brave Search is out of prepaid credit)"
    long = activity.reason(KeyError("x" * 400))
    assert long.startswith("KeyError: ") and len(long) < 180
