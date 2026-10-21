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
    assert moves == [("QUEUED", "RESEARCHING"), ("RESEARCHING", "BRIEF_READY")]
