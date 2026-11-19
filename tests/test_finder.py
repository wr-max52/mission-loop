from datetime import datetime, timezone
from pathlib import Path
import pytest
from ollama import Message
from starlette.testclient import TestClient
import activity, agents, orchestrator, run as cli, store, ui
from agents import finder, llm
from contracts import Mission, MissionIdea, State

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
SEEN = "https://fringe.example.com/theory"

def idea(topic="Did the agency bury the 1998 audit?", url=SEEN):
    return MissionIdea(topic=topic, objective="Bloggers say so. Find the audit.", source_url=url)

def as_json(*ideas):
    return Message(role="assistant", content=finder.MissionIdeas(missions=list(ideas)).model_dump_json())

def search_call(query="audit cover-up"):
    return Message(role="assistant", content="", tool_calls=[
        Message.ToolCall(function=Message.ToolCall.Function(name="search", arguments={"query": query}))])

DONE = Message(role="assistant", content="DONE")

class FakeToolbox:
    enabled, calls, reply = None, [], f"Result: {SEEN}/"
    def __init__(self, enabled):
        FakeToolbox.enabled = enabled
        self.tools = [{"type": "function", "function": {"name": "search"}}]
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): pass
    async def call(self, name, args):
        FakeToolbox.calls.append(args)
        return FakeToolbox.reply

@pytest.fixture(autouse=True)
def reset():
    FakeToolbox.calls, FakeToolbox.reply = [], f"Result: {SEEN}/"

def script(monkeypatch, replies):
    seen, it = [], iter(replies)
    async def fake_chat(messages, tools=None, format=None, model=None):
        seen.append({"messages": list(messages), "format": format, "model": model})
        return next(it)
    monkeypatch.setattr(llm, "chat", fake_chat)
    return seen

async def find(topics=()):
    return await finder.run_finder("Who runs the Fed?", list(topics), T0, toolbox_factory=FakeToolbox)

# ---------- the agent ----------

async def test_small_model_searches_and_main_model_picks(monkeypatch):
    seen = script(monkeypatch, [search_call(), DONE, as_json(idea())])
    assert [i.topic for i in await find()] == [idea().topic]
    assert FakeToolbox.enabled == ["search"]      # searching only; research does the reading
    assert [c["model"] for c in seen] == [llm.SEARCH_MODEL, llm.SEARCH_MODEL, None]
    assert seen[-1]["format"] is not None

async def test_prompt_carries_inspiration_two_angles_and_existing_missions(monkeypatch):
    seen = script(monkeypatch, [search_call(), DONE, as_json(idea())])
    await find(["An older mission"])
    system = seen[0]["messages"][0]["content"]
    assert "Who runs the Fed?" in system and "- An older mission" in system
    assert sum(angle in system for angle in finder.ANGLES) == 2

async def test_only_a_few_searches(monkeypatch):
    replies = [search_call(str(i)) for i in range(finder.MAX_SEARCHES)]
    script(monkeypatch, replies + [as_json(idea())])
    await find()
    assert len(FakeToolbox.calls) == finder.MAX_SEARCHES

async def test_lead_the_searches_never_returned_is_dropped(monkeypatch):
    invented = idea("Was the vault emptied at midnight?", "https://invented.example.com/page")
    script(monkeypatch, [search_call(), DONE, as_json(invented, idea())])
    assert [i.source_url for i in await find()] == [SEEN]

async def test_repeats_of_existing_missions_and_each_other_are_dropped(monkeypatch):
    fresh = idea("Are regional bank presidents chosen by the banks they regulate?")
    twin = idea("Are regional bank presidents really chosen by banks they regulate?")
    known = idea("Did the agency bury the 1998 audit report?")
    script(monkeypatch, [search_call(), DONE, as_json(known, fresh, twin)])
    assert [i.topic for i in await find(["Did the agency bury the 1998 audit?"])] == [fresh.topic]

async def test_at_most_three_missions(monkeypatch):
    many = [idea(f"{word} anomaly question") for word in ("alpha", "bravo", "charlie", "delta", "echo")]
    script(monkeypatch, [search_call(), DONE, as_json(*many)])
    assert len(await find()) == finder.MAX_IDEAS

async def test_failed_searches_stop_before_the_main_model(monkeypatch):
    FakeToolbox.reply = "ERROR: search failed"
    seen = script(monkeypatch, [search_call(), DONE])
    with pytest.raises(finder.FinderFailed):
        await find()
    assert all(c["model"] == llm.SEARCH_MODEL for c in seen)

async def test_bad_json_gets_one_repair(monkeypatch):
    script(monkeypatch, [search_call(), DONE, Message(role="assistant", content="{nope"), as_json(idea())])
    assert len(await find()) == 1

# ---------- every pass ----------

def calls_to_finder(monkeypatch, result=None):
    calls = []
    def fake(inspiration, topics, now):
        calls.append((inspiration, topics))
        return result if result is not None else []
    monkeypatch.setattr(agents, "find_missions", fake)
    return calls

def run_pass():
    return orchestrator.run_until_idle(lambda: T0)

@pytest.mark.usefixtures("stub_agents")
def test_no_inspiration_means_no_finder(monkeypatch):
    calls = calls_to_finder(monkeypatch)
    run_pass()
    assert calls == []

@pytest.mark.usefixtures("stub_agents")
def test_runs_once_every_pass_and_sees_existing_topics(monkeypatch):
    store.set_inspiration("Who runs the Fed?")
    store.save_mission(Mission(slug="old", topic="Old topic"))
    calls = calls_to_finder(monkeypatch)
    run_pass(); run_pass()
    assert calls == [("Who runs the Fed?", ["Old topic"])] * 2

@pytest.mark.usefixtures("stub_agents")
def test_found_mission_is_researched_in_the_same_pass():
    store.set_inspiration("Who runs the Fed?")
    run_pass()
    m = store.all_missions()[0]
    assert m.state == State.BRIEF_READY and m.topic.startswith("Stub lead 1")
    assert "Starting lead: https://a.example.com/lead" in m.objective
    assert "found by the mission finder" in store.events(m.slug)[0]["note"]

@pytest.mark.usefixtures("stub_agents")
def test_same_opening_words_get_different_slugs(monkeypatch):
    store.set_inspiration("x")
    store.save_mission(Mission(slug="is-it-possible-that-the", topic="Earlier"))
    calls_to_finder(monkeypatch, [idea("Is it possible that the moon"), idea("Is it possible that the sea")])
    run_pass()
    slugs = [m.slug for m in store.all_missions()]
    assert slugs == ["is-it-possible-that-the", "is-it-possible-that-the-2", "is-it-possible-that-the-3"]

@pytest.mark.usefixtures("stub_agents")
def test_finder_failure_does_not_stop_the_pass(monkeypatch, capsys):
    store.set_inspiration("x")
    store.save_mission(Mission(slug="m", topic="Anything"))
    def boom(*a): raise RuntimeError("no search")
    monkeypatch.setattr(agents, "find_missions", boom)
    run_pass()
    assert store.get_mission("m").state == State.BRIEF_READY
    assert "mission finder failed: RuntimeError: no search" in capsys.readouterr().out
    assert activity.current() is None

@pytest.mark.usefixtures("stub_agents")
def test_stop_flag_skips_the_finder(monkeypatch):
    store.set_inspiration("x")
    calls = calls_to_finder(monkeypatch)
    orchestrator.STOP_FLAG.parent.mkdir(exist_ok=True)
    orchestrator.STOP_FLAG.touch()
    run_pass()
    assert calls == []

# ---------- setting the line ----------

def test_inspiration_is_one_line_and_blank_clears_it():
    assert store.set_inspiration("  Who runs\n the Fed?  ") == "Who runs the Fed?"
    assert store.get_inspiration() == "Who runs the Fed?"
    store.set_inspiration("   ")
    assert store.get_inspiration() == "" and not store.INSPIRATION.exists()

def test_cli_sets_and_clears(capsys):
    cli.inspire("Who runs the Fed?")
    assert store.get_inspiration() == "Who runs the Fed?"
    cli.inspire("")
    assert "finder is off" in capsys.readouterr().out

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(orchestrator, "LOCK_FILE", Path("data/loop.lock"))
    monkeypatch.setattr(orchestrator, "WAKE_FLAG", Path("data/WAKE"))
    return TestClient(ui.app)

def test_ui_saves_and_shows_the_line_escaped(client):
    r = client.post("/inspiration", data={"inspiration": 'The "real" Fed'}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?done=inspired"
    assert store.get_inspiration() == 'The "real" Fed'
    assert 'value="The &quot;real&quot; Fed"' in client.get("/").text

def test_ui_blank_turns_it_off(client):
    store.set_inspiration("x")
    r = client.post("/inspiration", data={"inspiration": ""}, follow_redirects=False)
    assert r.headers["location"] == "/?done=uninspired" and store.get_inspiration() == ""

def test_ui_refuses_cross_site_post(client):
    r = client.post("/inspiration", data={"inspiration": "x"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and store.get_inspiration() == ""

def test_agent_strip_shows_the_finder_without_a_mission():
    html = ui.agent_strip({"slug": "", "agent": "finder", "since": 0})
    assert "<strong>Mission finder</strong> is looking for new missions" in html and "/m/" not in html
