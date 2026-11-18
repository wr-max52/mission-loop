from datetime import datetime, timezone
import pytest
import orchestrator, store
from contracts import Mission, State

pytestmark = pytest.mark.usefixtures("stub_agents")
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)

def test_reject_sends_brief_back_with_notes():
    store.save_mission(Mission(slug="m", topic="x"))
    orchestrator.run_until_idle(lambda: T0)
    orchestrator.reject(store.get_mission("m"), "thesis too vague", T0)
    m = store.get_mission("m")
    assert m.state == State.RESEARCHING and m.attempts == 0
    assert "thesis too vague" in m.validator_notes

def test_reject_only_from_brief_ready():
    store.save_mission(Mission(slug="m", topic="x"))
    with pytest.raises(ValueError):
        orchestrator.reject(store.get_mission("m"), "no", T0)

def test_requeue_revives_parked_mission():
    store.save_mission(Mission(slug="p", topic="FAIL"))
    orchestrator.run_until_idle(lambda: T0)
    assert store.get_mission("p").state == State.PARKED
    orchestrator.requeue(store.get_mission("p"), T0)
    m = store.get_mission("p")
    assert m.state == State.QUEUED and m.attempts == 0

def test_research_crash_counts_as_failed_attempt(monkeypatch):
    import agents
    def boom(m, now): raise RuntimeError("ollama is not running")
    monkeypatch.setattr(agents, "research", boom)
    store.save_mission(Mission(slug="c", topic="x"))
    orchestrator.run_until_idle(lambda: T0)
    m = store.get_mission("c")
    assert m.state == State.PARKED and "ollama is not running" in m.validator_notes

def test_failed_brief_is_kept_for_the_next_attempt():
    store.save_mission(Mission(slug="k", topic="FAIL"))
    orchestrator.run_until_idle(lambda: T0)
    kept = store.get_mission("k").rejected_brief
    assert kept.brief.thesis == "Stub thesis about FAIL" and kept.notes == "stub: forced failure"

def test_passing_brief_clears_the_rejected_one(monkeypatch):
    import agents
    verdicts = iter([(False, "s1: quote not found"), (True, "")])
    monkeypatch.setattr(agents, "validate", lambda m, brief: next(verdicts))
    store.save_mission(Mission(slug="ok", topic="x"))
    orchestrator.run_until_idle(lambda: T0)
    m = store.get_mission("ok")
    assert m.state == State.BRIEF_READY and m.attempts == 2 and m.rejected_brief is None

def test_crash_keeps_the_rejected_brief_and_its_notes(monkeypatch):
    import agents
    from agents import stubs
    results = iter([stubs.research, RuntimeError("ollama is not running")])
    def flaky(m, now):
        result = next(results)
        if isinstance(result, Exception):
            raise result
        return result(m, now)
    monkeypatch.setattr(agents, "research", flaky)
    monkeypatch.setattr(agents, "validate", lambda m, brief: (False, "s1: quote not found"))
    store.save_mission(Mission(slug="cr", topic="x", max_attempts=2))
    orchestrator.run_until_idle(lambda: T0)
    m = store.get_mission("cr")
    assert "ollama is not running" in m.validator_notes
    assert m.rejected_brief.notes == "s1: quote not found"

def test_brief_md_written_for_review():
    from pathlib import Path
    store.save_mission(Mission(slug="b", topic="x"))
    orchestrator.run_until_idle(lambda: T0)
    assert "**Thesis:**" in Path("missions/b/brief.md").read_text()

def test_rejected_brief_is_kept_for_humans():
    from pathlib import Path
    store.save_mission(Mission(slug="rj", topic="FAIL"))
    orchestrator.run_until_idle(lambda: T0)
    text = Path("missions/rj/brief.rejected.md").read_text()
    assert "**Thesis:**" in text and "stub: forced failure" in text
