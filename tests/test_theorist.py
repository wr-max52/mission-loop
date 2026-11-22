from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from ollama import Message
from starlette.testclient import TestClient
import activity, agents, orchestrator, run as cli, status_tools, store, trail, ui
from agents import llm, stubs, theorist
from agents.theorist import NewEpic, NewMission, Theory, TheoryReply
from contracts import Claim, Mission, State

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
LINE = "Who really decides what the Federal Reserve does?"
EVOLVED = "Who really decides what the Federal Reserve does, and who decides for the regional banks?"

def reply(line=EVOLVED, **kw):
    return TheoryReply(patterns=["Fed Capture: fed-a and fed-b both lead to the same lobbyist"], inspiration=line,
                       why="Two missions point at the regional banks.", **kw)

def as_msg(r):
    return Message(role="assistant", content=r if isinstance(r, str) else r.model_dump_json())

def script(monkeypatch, replies):
    seen, it = [], iter(replies)
    async def fake_chat(messages, tools=None, format=None, model=None):
        seen.append({"messages": list(messages), "format": format, "model": model})
        return as_msg(next(it))
    monkeypatch.setattr(llm, "chat", fake_chat)
    return seen

def desk():
    """Two epics, a mission in neither, and a published article that left a possibility open."""
    fed = store.create_epic("Fed Capture")
    water = store.create_epic("Water Rights")
    store.save_mission(Mission(slug="fed-a", topic="Who wrote the 2019 rate memo?", epic=fed.slug))
    store.save_mission(Mission(slug="fed-b", topic="Did a lobbyist shape the stress tests?", epic=fed.slug,
                               state=State.DONE, published_at=T0))
    store.save_mission(Mission(slug="dam", topic="Who profits from the new dam permits?", epic=water.slug))
    store.save_mission(Mission(slug="loose", topic="Why did the grid fail in July?"))
    brief = stubs.research(store.get_mission("fed-b"))
    brief.claims.append(Claim(text="It is possible that the memo was drafted by the bank", source_ids=["s1"],
                              kind="possibility"))
    store.save_artifact("fed-b", "brief", brief)
    store.save_artifact("fed-b", "article", stubs.write(store.get_mission("fed-b"), brief, []))
    store.set_inspiration(LINE)

async def think():
    return await theorist.run_theorist(store.get_inspiration(), store.inspiration_history(), store.all_missions(), T0)

# ---------- the agent ----------

async def test_reads_the_whole_desk_on_the_big_model(monkeypatch):
    desk()
    seen = script(monkeypatch, [reply()])
    theory = await think()
    system = seen[0]["messages"][0]["content"]
    assert seen[0]["model"] is None and seen[0]["format"] is not None   # None: the Principal Investigator's model
    assert LINE in system and "Today's date: 2026-11-02" in system
    assert "Epic: Fed Capture, 2 missions" in system and "- fed-a | queued | Who wrote the 2019 rate memo?" in system
    assert "- fed-b | published 2026-11-02 |" in system and "Epic: Water Rights, 1 missions" in system
    assert "Missions in no epic:\n- loose | queued" in system
    assert "Left open: It is possible that the memo was drafted by the bank" in system
    assert "the editor:" in system                                  # how the line got here
    assert theory.inspiration == EVOLVED and theory.based_on == LINE and theory.dropped == []

async def test_line_must_be_questions_and_gets_one_repair(monkeypatch):
    desk()
    statement = "Who really decides what the Federal Reserve does. Follow the regional banks."
    seen = script(monkeypatch, [reply(statement), reply()])
    assert (await think()).inspiration == EVOLVED
    assert "question mark" in seen[1]["messages"][-1]["content"]

async def test_a_rewrite_twice_keeps_the_line(monkeypatch):
    desk()
    rewrite = "Are central banks run by hedge funds?"
    script(monkeypatch, [reply(rewrite), reply(rewrite)])
    theory = await think()
    assert theory.inspiration == LINE
    assert "instead of evolving it" in theory.dropped[0]

def test_line_rules():
    assert theorist.line_problems(EVOLVED, LINE) == []
    assert theorist.line_problems(LINE, LINE) == []                 # unchanged is fine
    assert "question mark" in theorist.line_problems("Who decides what the Federal Reserve does", LINE)[0]
    assert "at most 3" in theorist.line_problems("Who really decides? Federal? Reserve? Does?", LINE)[0]
    assert "characters" in theorist.line_problems(LINE[:-1] + " really" * 80 + "?", LINE)[0]
    assert "empty" in theorist.line_problems("  ", LINE)[0]

async def test_new_epics_need_two_missions_and_a_new_name(monkeypatch):
    desk()
    script(monkeypatch, [reply(new_epics=[
        NewEpic(title="Permit Money", missions=["dam", "fed-a", "nope"]),       # kept, unknown slug left out
        NewEpic(title="Lone Thread", missions=["loose"]),                       # one mission: dropped
        NewEpic(title="water rights", missions=["dam", "loose"]),               # an existing epic: dropped
        NewEpic(title="Grid Silence", missions=["loose"]),                      # kept: the new mission joins it
        NewEpic(title="Third Theme", missions=["dam", "loose"]),                # over the limit: dropped
    ], new_mission=NewMission(topic="Did the grid operator and the dam builder share lawyers?",
                              objective="loose and dam both name one firm.", epic="grid silence"))])
    theory = await think()
    assert [(e.title, e.missions) for e in theory.new_epics] == [("Permit Money", ["dam", "fed-a"]),
                                                                ("Grid Silence", ["loose"])]
    assert theory.new_mission.epic == "Grid Silence"
    assert len(theory.dropped) == 3

async def test_new_mission_must_be_new(monkeypatch):
    desk()
    script(monkeypatch, [reply(new_mission=NewMission(topic="Who wrote the 2019 rate memo really?", objective="x"))])
    theory = await think()
    assert theory.new_mission is None and "already has one like it" in theory.dropped[0]

async def test_new_mission_goes_to_an_existing_epic_or_none(monkeypatch):
    desk()
    script(monkeypatch, [reply(new_mission=NewMission(topic="Do dam permits follow Fed lending?", objective="x",
                                                      epic="FED CAPTURE")),
                         reply(new_mission=NewMission(topic="Do dam permits follow Fed lending?", objective="x",
                                                      epic="Made Up"))])
    assert (await think()).new_mission.epic == "Fed Capture"
    assert (await think()).new_mission.epic == ""

async def test_bad_json_gets_one_repair(monkeypatch):
    desk()
    script(monkeypatch, ["{nope", reply()])
    assert (await think()).inspiration == EVOLVED

async def test_a_stuck_reply_keeps_what_it_wrote(monkeypatch):
    desk()
    async def stuck(*a, **k):
        raise llm.Overrun("reply ran past 8192 tokens", reply().model_dump_json()[:-40])
    monkeypatch.setattr(llm, "chat", stuck)
    theory = await think()
    assert theory.patterns and theory.inspiration in (EVOLVED, LINE)

# ---------- in the loop ----------

def fake_theorist(monkeypatch, make=None):
    calls = []
    def fake(now):
        calls.append(("theorist", store.get_inspiration()))
        line = store.get_inspiration()
        return make(line) if make else Theory(patterns=["p"], inspiration=EVOLVED, why="w", based_on=line)
    monkeypatch.setattr(agents, "theorize", fake)
    return calls

def fake_finder(monkeypatch, calls):
    def fake(inspiration, topics, now, epics=None):
        calls.append(("finder", inspiration))
        return []
    monkeypatch.setattr(agents, "find_missions", fake)

def run_at(t=T0):
    return orchestrator.run_until_idle(lambda: t)

@pytest.mark.usefixtures("stub_agents")
def test_theorist_goes_first_and_the_finder_searches_its_line(monkeypatch):
    desk()
    calls = fake_theorist(monkeypatch)
    fake_finder(monkeypatch, calls)
    run_at()
    assert calls == [("theorist", LINE), ("finder", EVOLVED)]
    assert store.get_inspiration() == EVOLVED
    assert [(h["by"], h["line"]) for h in store.inspiration_history()] == [("human", LINE), ("theorist", EVOLVED)]
    assert store.inspiration_history()[-1]["why"] == "w"
    assert store.last_theory()["line_before"] == LINE and store.last_theory()["patterns"] == ["p"]

@pytest.mark.usefixtures("stub_agents")
def test_no_line_or_no_missions_means_no_theorist(monkeypatch):
    calls = fake_theorist(monkeypatch)
    store.set_inspiration(LINE)
    run_at()                                   # nothing on the desk yet
    store.set_inspiration("")
    store.save_mission(Mission(slug="m", topic="Anything"))
    run_at()
    assert calls == []

@pytest.mark.usefixtures("stub_agents")
def test_waits_for_news_and_the_gap(monkeypatch):
    desk()
    calls = fake_theorist(monkeypatch, lambda line: Theory(patterns=[], inspiration=line, based_on=line))
    run_at()
    run_at(T0 + timedelta(days=1))             # nothing new on the desk
    store.save_mission(Mission(slug="new", topic="A new topic"))
    run_at(T0 + timedelta(hours=1))            # news, but too soon
    run_at(T0 + orchestrator.THEORY_GAP)
    assert len(calls) == 2

@pytest.mark.usefixtures("stub_agents")
def test_asking_skips_the_wait_once(monkeypatch, capsys):
    desk()
    calls = fake_theorist(monkeypatch, lambda line: Theory(patterns=[], inspiration=line, based_on=line))
    run_at()
    cli.theorize()
    assert "goes first the next time the loop starts" in capsys.readouterr().out
    run_at()
    run_at()
    assert len(calls) == 2 and not orchestrator.THINK_FLAG.exists()

def test_cli_needs_a_line():
    with pytest.raises(Exception):
        cli.theorize()
    assert not orchestrator.THINK_FLAG.exists()

@pytest.mark.usefixtures("stub_agents")
def test_failure_is_noted_and_never_stops_the_loop(monkeypatch, capsys):
    desk()
    def boom(now): raise RuntimeError("no model")
    monkeypatch.setattr(agents, "theorize", boom)
    run_at()
    assert store.get_mission("fed-a").state == State.BRIEF_READY
    assert "theorist failed: RuntimeError: no model" in capsys.readouterr().out
    assert store.last_theory()["failed"] == "RuntimeError: no model" and activity.current() is None
    assert not orchestrator.theorist_due(T0 + timedelta(hours=1))   # it waits before trying again

@pytest.mark.usefixtures("stub_agents")
def test_stub_theorist_changes_nothing():
    desk()
    run_at()
    assert store.get_inspiration() == LINE and store.last_theory()["patterns"] == ["Stub pattern"]

# ---------- applying a theory ----------

def test_a_human_edit_meanwhile_wins():
    desk()
    store.set_inspiration("The editor's new line?")
    orchestrator.apply_theory(Theory(patterns=[], inspiration=EVOLVED, based_on=LINE), T0)
    assert store.get_inspiration() == "The editor's new line?"
    assert "changed it while the Theorist was thinking" in store.last_theory()["notes"][0]

def test_proposed_epic_moves_missions_and_rejecting_puts_them_back():
    desk()
    fed = store.find_epic("Fed Capture").slug
    theory = Theory(patterns=[], inspiration=LINE, based_on=LINE,
                    new_epics=[NewEpic(title="Permit Money", why="both name one firm", missions=["dam", "fed-b"])],
                    new_mission=NewMission(topic="Do the permit firm and the Fed lobbyist overlap?", objective="o",
                                           epic="Permit Money"))
    assert orchestrator.apply_theory(theory, T0) == 3
    epic = store.find_epic("Permit Money")
    assert epic.status == "proposed"
    new = [m for m in store.all_missions() if m.topic.startswith("Do the permit")][0]
    assert new.epic == epic.slug and new.state == State.QUEUED and "Proposed by the Theorist" in new.objective
    assert "proposed by the Theorist" in store.events(new.slug)[0]["note"]
    assert store.get_mission("fed-b").epic == epic.slug and store.get_mission("fed-b").epic_fallback == fed
    assert "Theorist proposes the new epic Permit Money: both name one firm" in store.events("dam")[-1]["note"]
    assert store.last_theory()["epics"] == ["Permit Money"] and store.last_theory()["mission"] == new.slug
    orchestrator.reject_epic(epic.slug, T0)
    assert store.get_mission("fed-b").epic == fed and store.get_mission("dam").epic == store.find_epic("Water Rights").slug
    assert store.get_mission(new.slug).epic == ""

def test_missions_being_worked_on_stay_put():
    desk()
    theory = Theory(patterns=[], inspiration=LINE, based_on=LINE,
                    new_epics=[NewEpic(title="Permit Money", missions=["dam", "fed-a"])])
    assert orchestrator.apply_theory(theory, T0, busy={"fed-a"}) == 0
    assert store.find_epic("Permit Money") is None and "not enough of its missions" in store.last_theory()["notes"][0]

# ---------- what humans and the chat assistant see ----------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(orchestrator, "LOCK_FILE", Path("data/loop.lock"))
    monkeypatch.setattr(orchestrator, "WAKE_FLAG", Path("data/WAKE"))
    monkeypatch.setattr(orchestrator, "THINK_FLAG", Path("data/THINK"))
    return TestClient(ui.app)

def test_ui_shows_the_notes_and_the_line_over_time(client):
    desk()
    orchestrator.apply_theory(Theory(patterns=["A <pattern>"], inspiration=EVOLVED, why="Because.", based_on=LINE,
                                     new_epics=[NewEpic(title="Permit Money", missions=["dam", "fed-a"])]), T0)
    html = client.get("/").text
    assert "Theorist&#x27;s notes" in html or "Theorist's notes" in html
    assert "<li>A &lt;pattern&gt;</li>" in html and "Because." in html
    assert "Proposed epic <strong>Permit Money</strong>" in html
    assert "Theorist:</span> " + EVOLVED in html and "you:</span> " + LINE in html
    assert "Proposed by Theorist · 2 missions" in html

def test_ui_asks_the_theorist(client):
    store.set_inspiration(LINE)
    r = client.post("/theorist", follow_redirects=False)
    assert r.headers["location"] == "/?done=thinking" and orchestrator.THINK_FLAG.exists()

def test_ui_refuses_cross_site_ask(client):
    store.set_inspiration(LINE)
    r = client.post("/theorist", headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and not orchestrator.THINK_FLAG.exists()

def test_flow_strip_shows_the_theorist():
    html = ui.flow_status([{"worker": "pi", "slug": "", "agent": "theorist", "since": 0}], True)
    assert "<strong>Theorist</strong> · all missions" in html

def test_trail_and_chat_tools():
    desk()
    activity.begin("", "theorist")
    trail.record("said", "thinking about the Fed")
    activity.clear()
    assert trail.recent("", agent="theorist")[-1]["text"] == "thinking about the Fed"
    assert all(s["agent"] != "theorist" for s in trail.recent(""))           # the finder's trail is its own
    assert "thinking about the Fed" in status_tools.agent_steps("theorist")
    orchestrator.apply_theory(Theory(patterns=["p1"], inspiration=EVOLVED, why="w1", based_on=LINE), T0)
    notes = status_tools.theorist_notes()
    assert f"Inspiration line now: {EVOLVED}" in notes and "- pattern: p1" in notes and "by the theorist" in notes
