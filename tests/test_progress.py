"""Stopping and saving progress: every finished piece is on disk the moment it is done, a stop never starts
work only for the grace to cut it off, and the next start carries on from the last saved piece."""
from datetime import datetime, timezone
import pytest
from ollama import Message
import agents, checkpoint, orchestrator, stopping, store
from agents import llm, researcher, stubs, writer
from checks import validator
from contracts import ArticleDraft, ArticlePlan, Evidence, Mission, PlannedSection, Section, SourcePick, State

pytestmark = pytest.mark.usefixtures("stub_agents")
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
BRIEF = stubs.research(Mission(slug="x", topic="t"))
PICK_JSON = SourcePick(thesis=BRIEF.thesis, sources=BRIEF.sources).model_dump_json()

def tool_call(name, **args):
    return Message(role="assistant", content="", tool_calls=[
        Message.ToolCall(function=Message.ToolCall.Function(name=name, arguments=args))])

class Box:
    calls: list = []
    def __init__(self, enabled): self.tools = [{"type": "function", "function": {"name": "search"}}]
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): pass
    async def call(self, name, args):
        Box.calls.append(args)
        return f"result {args}"

def replies(monkeypatch, items, sent=None):
    it = iter(items)
    async def fake_chat(messages, tools=None, format=None, model=None):
        if sent is not None:
            sent.append(list(messages))
        reply = next(it)
        if callable(reply):
            reply = reply()
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, Message) else Message(role="assistant", content=reply)
    monkeypatch.setattr(llm, "chat", fake_chat)

def press():
    stopping.press(orchestrator.STOP_FLAG)

@pytest.fixture(autouse=True)
def reset():
    Box.calls = []
    yield
    checkpoint.use(None)

# --- the search ---------------------------------------------------------------------------------------------

async def test_a_stop_mid_search_comes_back_unfinished_with_every_result_saved(monkeypatch):
    saves = []
    checkpoint.use(lambda m, artifacts: saves.append(artifacts["evidence"]))
    def second():
        press()                            # pressed while the model thinks about its second call
        return tool_call("search", query="b")
    replies(monkeypatch, [tool_call("search", query="a"), second])
    m = Mission(slug="s", topic="t", attempts=1)
    ev = await researcher.run_gather(m, toolbox_factory=Box)
    assert not ev.finished and ev.attempt == 1 and Box.calls == [{"query": "a"}]   # b was never run
    assert saves and not saves[-1].finished and saves[-1].messages[-1]["content"].startswith("result")

async def test_a_paused_search_carries_on_with_what_it_has_left(monkeypatch):
    paused = Evidence(messages=[{"role": "system", "content": "the search prompt"}, {"role": "user", "content": "Begin."},
                                {"role": "assistant", "content": ""}, {"role": "tool", "content": "result a"}],
                      finished=False, attempt=1, seconds=60)
    sent = []
    replies(monkeypatch, [tool_call("search", query="b"), Message(role="assistant", content="DONE")], sent)
    monkeypatch.setattr(researcher, "MAX_TOOL_CALLS", 2)
    ev = await researcher.run_gather(Mission(slug="s", topic="t", attempts=1), toolbox_factory=Box, resume=paused)
    assert ev.finished and Box.calls == [{"query": "b"}]
    assert sent[0][0]["content"] == "the search prompt" and sent[0][-1]["content"] == researcher.CARRY_ON
    results = [x["content"] for x in ev.messages if x.get("role") == "tool"]
    assert results[0] == "result a" and "[0 tool calls left" in results[1]   # the paused run's call counts too

async def test_a_paused_search_out_of_budget_ends_without_another_call(monkeypatch):
    paused = Evidence(messages=[{"role": "system", "content": "p"}] + [{"role": "tool", "content": "r"}] * 3,
                      finished=False, attempt=1)
    monkeypatch.setattr(researcher, "MAX_TOOL_CALLS", 3)
    replies(monkeypatch, [])
    ev = await researcher.run_gather(Mission(slug="s", topic="t"), toolbox_factory=Box, resume=paused)
    assert ev.finished and Box.calls == []

def test_the_loop_carries_on_a_stopped_search_as_the_same_attempt(monkeypatch):
    store.save_mission(Mission(slug="r", topic="t", state=State.RESEARCHING))
    def stopped(m, now, resume=None):
        press()
        return Evidence(messages=[{"role": "tool", "content": "page a"}], finished=False, attempt=m.attempts)
    monkeypatch.setattr(agents, "gather", stopped)
    orchestrator.run_until_idle(lambda: T0)
    m = store.get_mission("r")
    assert m.state == State.RESEARCHING and m.attempts == 1 and not m.gathered and m.search_paused
    assert "carries on" in store.events("r")[-1]["note"]
    import ui
    assert ui.next_step(m) == "Carrying on the paused search."      # still attempt 1, not "attempt 2"

    orchestrator.STOP_FLAG.unlink()
    got = []
    def carry_on(m, now, resume=None):
        got.append(resume)
        return Evidence(messages=resume.messages + [{"role": "tool", "content": "page b"}])
    monkeypatch.setattr(agents, "gather", carry_on)
    orchestrator.run_until_idle(lambda: T0)
    m = store.get_mission("r")
    assert got[0].messages[0]["content"] == "page a" and m.attempts == 1     # not a second attempt
    assert m.state == State.BRIEF_READY and not m.search_paused

def test_a_search_saved_by_a_killed_loop_carries_on_and_counts_once(monkeypatch):
    store.save_mission(Mission(slug="k", topic="t", state=State.RESEARCHING))   # attempts never got saved
    store.save_artifact("k", "evidence", Evidence(messages=[{"role": "tool", "content": "a"}], finished=False, attempt=1))
    got = []
    monkeypatch.setattr(agents, "gather", lambda m, now, resume=None: got.append(resume) or Evidence(messages=[]))
    orchestrator.run_until_idle(lambda: T0)
    m = store.get_mission("k")
    assert got[0] is not None and m.attempts == 1 and m.state == State.BRIEF_READY

def test_a_finished_search_saved_just_before_a_kill_is_not_run_again(monkeypatch):
    store.save_mission(Mission(slug="f", topic="t", state=State.RESEARCHING))
    store.save_artifact("f", "evidence", Evidence(messages=[{"role": "tool", "content": "a"}], attempt=1))
    monkeypatch.setattr(agents, "gather", lambda *a: pytest.fail("the search is already done"))
    orchestrator.run_until_idle(lambda: T0)
    assert store.get_mission("f").state == State.BRIEF_READY and store.get_mission("f").attempts == 1

def test_a_crashed_search_leaves_nothing_to_carry_on(monkeypatch):
    store.save_mission(Mission(slug="c", topic="t", state=State.RESEARCHING, max_attempts=2))
    store.save_artifact("c", "evidence", Evidence(messages=[{"role": "tool", "content": "a"}], finished=False, attempt=1))
    calls = []
    def crash(m, now, resume=None):
        calls.append(resume)
        raise RuntimeError("ollama went away")
    monkeypatch.setattr(agents, "gather", crash)
    orchestrator.run_until_idle(lambda: T0)
    assert calls[0] is not None and calls[1] is None    # the retry starts afresh, it doesn't resume a crash

# --- the brief ----------------------------------------------------------------------------------------------

@pytest.fixture
def checks_pass(monkeypatch):
    monkeypatch.setattr(validator, "source_problem", lambda s, blocked=None, archive=False: (None, s.url))

async def test_a_stop_after_step_1_saves_the_checked_sources_and_does_not_start_step_2(monkeypatch):
    saved = []
    checkpoint.use(lambda m, artifacts: saved.append((m.brief_pick, m.brief_sources)))
    def check(s, blocked=None, archive=False):
        if s.id == "s4":
            press()                        # pressed during the last check: step 1 finishes, step 2 never starts
        return None, s.url
    monkeypatch.setattr(validator, "source_problem", check)
    replies(monkeypatch, [PICK_JSON])      # a second call would raise StopIteration
    m = Mission(slug="b", topic="t")
    with pytest.raises(stopping.Interrupted) as stop:
        await researcher.run_write_brief(m, Evidence(messages=[{"role": "system", "content": "s"}]))
    assert stop.value.draft is None and len(m.brief_sources.sources) == 4 and m.brief_pick is None
    assert saved[0][0] is not None and saved[-1][1] is not None   # the raw pick, then the checked sources

async def test_a_stop_during_the_source_checks_keeps_the_pick_and_checks_it_next_time(monkeypatch):
    checked = []
    def check(s, blocked=None, archive=False):
        checked.append(s.id)
        if s.id == "s2" and not orchestrator.STOP_FLAG.parent.joinpath("pressed").exists():
            orchestrator.STOP_FLAG.parent.joinpath("pressed").touch()
            press()                        # pressed (once) while s2's page is fetched
        return None, s.url
    monkeypatch.setattr(validator, "source_problem", check)
    replies(monkeypatch, [PICK_JSON])
    m = Mission(slug="b", topic="t")
    ev = Evidence(messages=[{"role": "system", "content": "s"}])
    with pytest.raises(stopping.Interrupted):
        await researcher.run_write_brief(m, ev)
    assert checked == ["s1", "s2"] and m.brief_pick is not None and m.brief_sources is None

    orchestrator.STOP_FLAG.unlink()
    checked.clear()
    claims = '{"counterpoint": "c", "claims": [{"text": "A said B.", "source_ids": ["s1"], "kind": "fact"}]}'
    replies(monkeypatch, [claims])         # only step 2 asks the model: the pick is not asked for again
    brief = await researcher.run_write_brief(m, ev)
    assert checked == ["s1", "s2", "s3", "s4"] and len(brief.sources) == 4

# --- the article --------------------------------------------------------------------------------------------

PLAN = ArticlePlan(title="T", dek="D", claim="Big idea [s1].",
                   sections=[PlannedSection(heading=h, point="p", counterpoint=h == "C")
                             for h in "ABC"])

def good(heading):
    cite = "s4" if heading == "C" else "s1"
    return Section(heading=heading, paragraphs=[f"Words about it [{cite}]. " * 40]).model_dump_json()

async def test_every_finished_section_is_saved_and_a_stop_starts_no_new_one(monkeypatch):
    saved = []
    checkpoint.use(lambda m, artifacts: saved.append(len(m.article_draft.sections)))
    def second():
        press()                            # pressed while section B is written: B finishes, C never starts
        return good("B")
    replies(monkeypatch, [PLAN.model_dump_json(), good("A"), second])
    m = Mission(slug="a", topic="t")
    with pytest.raises(stopping.Interrupted):
        await writer.run_writer(m, BRIEF, [])
    assert saved == [0, 1, 2] and [s.heading for s in m.article_draft.sections] == ["A", "B"]

def test_progress_is_on_disk_while_the_task_is_still_running(monkeypatch):
    store.save_mission(Mission(slug="w", topic="t", state=State.WRITING, approved=True))
    store.save_artifact("w", "brief", BRIEF)
    seen = []
    def write(m, brief, records):
        m.article_draft = ArticleDraft(plan=PLAN)
        checkpoint.save(m)
        seen.append(store.get_mission("w").article_draft)   # what a killed loop would find on restart
        return stubs.write(m, brief, records)
    monkeypatch.setattr(agents, "write", write)
    orchestrator.run_until_idle(lambda: T0)
    assert seen[0] == ArticleDraft(plan=PLAN)

def test_a_checkpoint_writes_only_progress_and_never_undoes_a_change_made_meanwhile():
    store.save_mission(Mission(slug="p", topic="t", state=State.WRITING))
    worker_copy = store.get_mission("p")
    meanwhile = store.get_mission("p")
    meanwhile.epic = "new-epic"            # a human moves it to an epic while the writer works
    store.save_mission(meanwhile)
    worker_copy.article_draft = ArticleDraft(plan=PLAN)
    orchestrator.save_progress(worker_copy, {})
    stored = store.get_mission("p")
    assert stored.article_draft == ArticleDraft(plan=PLAN) and stored.epic == "new-epic"

def test_the_mission_card_says_how_far_saved_work_got():
    import ui
    m = Mission(slug="u", topic="t", state=State.WRITING, write_attempts=1,
                article_draft=ArticleDraft(plan=PLAN, sections=[Section(heading="A", paragraphs=["x [s1]."])]))
    assert ui.next_step(m) == "Writing attempt 2 of 3. 1 of 3 sections written."
    brief = Mission(slug="v", topic="t", state=State.RESEARCHING, attempts=1, gathered=True,
                    brief_sources=SourcePick(thesis="t", sources=BRIEF.sources))
    assert ui.next_step(brief) == "Writing the brief's claims from its checked sources."
