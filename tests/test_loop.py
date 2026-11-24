from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
import orchestrator, store
from contracts import Mission, State

pytestmark = pytest.mark.usefixtures("stub_agents")

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)

def run_at(t):
    orchestrator.run_until_idle(lambda: t)

def state(slug):
    return store.get_mission(slug).state

def flag(slug, **fields):
    m = store.get_mission(slug)
    for k, v in fields.items():
        setattr(m, k, v)
    store.save_mission(m)

def test_happy_path_waits_full_48_hours():
    store.save_mission(Mission(slug="m1", topic="Heat pumps"))
    run_at(T0)
    assert state("m1") == State.BRIEF_READY          # blocked on approval
    flag("m1", approved=True)
    run_at(T0)
    assert state("m1") == State.AWAITING_COMMENTS
    run_at(T0 + timedelta(hours=47, minutes=59))
    assert state("m1") == State.AWAITING_COMMENTS    # still waiting
    run_at(T0 + timedelta(hours=48))
    assert state("m1") == State.ARTICLE_READY
    assert Path("missions/m1/article.md").exists()
    flag("m1", reviewed=True)
    run_at(T0 + timedelta(hours=49))
    assert state("m1") == State.DONE

def test_forced_failure_parks_after_3_attempts():
    store.save_mission(Mission(slug="bad", topic="FAIL on purpose"))
    run_at(T0)
    m = store.get_mission("bad")
    assert m.state == State.PARKED and m.attempts == 3

def test_stop_then_resume():
    store.save_mission(Mission(slug="m2", topic="Anything"))
    orchestrator.STOP_FLAG.touch()
    run_at(T0)
    assert state("m2") == State.QUEUED              # nothing ran
    orchestrator.STOP_FLAG.unlink()
    run_at(T0)
    assert state("m2") == State.BRIEF_READY         # resumed where it was

def test_every_transition_is_logged():
    store.save_mission(Mission(slug="m3", topic="Anything"))
    run_at(T0)
    moves = [(e["from_state"], e["to_state"]) for e in store.events("m3")]
    assert moves == [("QUEUED", "RESEARCHING"), ("RESEARCHING", "RESEARCHING"), ("RESEARCHING", "BRIEF_READY")]

# ---------- order of work: code steps first, then the farthest-along model work ----------

import types
import agents, settings
from agents import stubs

def one_worker():
    """With the Research Assistant off, the PI does every model task one at a time, so the order is fixed."""
    settings.set_worker("assistant", "small-test:1b", on=False)

def ready_to_write(slug, topic="Anything"):
    m = Mission(slug=slug, topic=topic, state=State.WRITING, approved=True)
    store.save_artifact(slug, "brief", stubs.research(m))
    store.save_artifact(slug, "outreach", [])
    store.save_mission(m)

def ready_to_publish(slug):
    m = Mission(slug=slug, topic="Anything", state=State.ARTICLE_READY, approved=True, reviewed=True)
    brief = stubs.research(m)
    store.save_artifact(slug, "brief", brief)
    store.save_artifact(slug, "article", stubs.write(m, brief, []))
    store.save_mission(m)

def watch(monkeypatch):
    """Record each agent call as (agent, slug), in order, while the stubs do the work."""
    calls = []
    for name in ("gather", "write_brief", "outreach", "write", "publish"):
        real = getattr(agents, name)
        def spy(m, *a, _name=name, _real=real, **k):
            calls.append((_name, m.slug))
            return _real(m, *a, **k)
        monkeypatch.setattr(agents, name, spy)
    return calls

def test_publishing_goes_first_then_the_farthest_along_model_work(monkeypatch):
    one_worker()
    store.save_mission(Mission(slug="new", topic="Anything"))
    store.save_mission(Mission(slug="half", topic="Anything", state=State.RESEARCHING, gathered=True, attempts=1))
    store.save_artifact("half", "evidence", stubs.gather(store.get_mission("half")))
    ready_to_write("draft")
    ready_to_publish("done")
    calls = watch(monkeypatch)
    run_at(T0)
    assert calls == [("publish", "done"),
                     ("write", "draft"),               # writing first,
                     ("write_brief", "half"),          # then a brief from sources already found,
                     ("gather", "new"), ("write_brief", "new")]   # then a new search

def test_channels_post_before_any_model_work(monkeypatch):
    ready_to_publish("old")
    run_at(T0)                                         # published, but no channel was set up yet
    one_worker()
    store.save_mission(Mission(slug="new", topic="Anything"))
    calls = watch(monkeypatch)
    channel = types.SimpleNamespace(KEY="fake", NAME="Fake", enabled=lambda: True, wait_until=lambda: None,
                                    post=lambda entry, now: calls.append(("post", entry["slug"])) or "x")
    monkeypatch.setattr(agents, "CHANNELS", [channel])
    run_at(T0)
    assert calls[:2] == [("post", "old"), ("gather", "new")]

def test_a_decision_made_while_a_model_works_is_acted_on_next(monkeypatch):
    monkeypatch.setattr(settings, "outreach_on", lambda: False)    # an approved brief goes straight to writing
    one_worker()
    store.save_mission(Mission(slug="a", topic="Anything", state=State.RESEARCHING))
    store.save_mission(Mission(slug="b", topic="Anything", state=State.RESEARCHING))
    store.save_artifact("b", "brief", stubs.research(store.get_mission("b")))
    calls = watch(monkeypatch)
    real = agents.gather
    def gather(m, now=None):
        if m.slug == "a":                              # the human approves b's brief meanwhile
            flag("b", state=State.BRIEF_READY, approved=True, wait_hours=0)
        return real(m, now)
    monkeypatch.setattr(agents, "gather", gather)
    run_at(T0)
    assert calls[:2] == [("gather", "a"), ("write", "b")]

def test_a_failed_attempt_lets_the_others_have_a_turn(monkeypatch):
    one_worker()
    ready_to_write("bad", topic="BADARTICLE")
    store.save_mission(Mission(slug="ok", topic="Anything", state=State.RESEARCHING))
    calls = watch(monkeypatch)
    run_at(T0)
    assert calls == [("write", "bad"), ("gather", "ok"), ("write", "bad"), ("write_brief", "ok"), ("write", "bad")]
    assert state("bad") == State.PARKED and state("ok") == State.BRIEF_READY

def test_a_mission_that_moves_on_gets_its_turn_ahead_of_new_briefs(monkeypatch):
    one_worker()
    settings.set_auto_mode(True)                       # a's brief is approved the moment it is written
    for slug in ("a", "b"):
        store.save_mission(Mission(slug=slug, topic="Anything", state=State.RESEARCHING, gathered=True, attempts=1))
        store.save_artifact(slug, "evidence", stubs.gather(store.get_mission(slug)))
    calls = watch(monkeypatch)
    run_at(T0)
    assert calls[:3] == [("write_brief", "a"),
                         ("outreach", "a"),            # moved on to Outreach: fresh again, and farther along (#163)
                         ("write_brief", "b")]

def test_a_code_step_that_breaks_is_logged_and_the_rest_go_on():
    store.save_mission(Mission(slug="new", topic="Anything"))
    store.save_mission(Mission(slug="done", topic="Anything", state=State.ARTICLE_READY, reviewed=True))
    run_at(T0)                                         # its brief and article are missing: publishing raises
    assert state("done") == State.ARTICLE_READY and state("new") == State.BRIEF_READY
    notes = [e["note"] for e in store.events("done")]
    assert len(notes) == 1 and notes[0].startswith("ARTICLE_READY step failed: TypeError")

def test_the_loop_hears_after_every_model_task():
    one_worker()
    store.save_mission(Mission(slug="a", topic="Anything"))
    seen = []
    orchestrator.run_until_idle(lambda: T0, after_task=lambda: seen.append(state("a").value))
    assert seen == ["RESEARCHING", "BRIEF_READY"]      # after the search, and after the brief
