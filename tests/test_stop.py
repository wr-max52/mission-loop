from datetime import datetime, timezone
import pytest
import agents, orchestrator, stopping, store
from agents import stubs
from contracts import Mission, State, ResearchBrief

pytestmark = pytest.mark.usefixtures("stub_agents")
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)

def test_salvage_keeps_what_a_cut_off_brief_got_out():
    cut = ('{"thesis": "It is possible that X", "claims": [{"text": "A said B", "source_ids": ["s1"], "kind": "fact"},'
           ' {"text": "It is possible th')
    brief = stopping.salvage(cut, ResearchBrief)
    assert brief.thesis == "It is possible that X"
    assert [c.text for c in brief.claims] == ["A said B"]     # the half-written claim is dropped
    assert brief.sources == [] and brief.counterpoint == ""   # the validator will catch these
    assert stopping.salvage("no json here", ResearchBrief) is None

def test_stopped_research_keeps_its_draft_and_the_next_pass_checks_it(monkeypatch):
    store.save_mission(Mission(slug="m", topic="Anything", state=State.RESEARCHING))
    def stopped(m, now=None):
        stopping.press(orchestrator.STOP_FLAG)
        raise stopping.Interrupted(stubs.research(m, now))
    monkeypatch.setattr(agents, "research", stopped)
    orchestrator.run_until_idle(lambda: T0)
    m = store.get_mission("m")
    assert m.state == State.RESEARCHING and m.attempts == 0 and m.interrupted_brief is not None

    orchestrator.STOP_FLAG.unlink()
    monkeypatch.setattr(agents, "research", lambda m, now=None: pytest.fail("research should not rerun"))
    orchestrator.run_until_idle(lambda: T0)
    m = store.get_mission("m")
    assert m.state == State.BRIEF_READY and m.attempts == 1 and m.interrupted_brief is None
