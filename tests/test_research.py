import asyncio, json
import pytest
from ollama import Message
import activity
from agents import researcher as research_mod
from agents import llm
from checks import validator
import settings
from agents.stubs import research as stub_brief
from contracts import Mission, RejectedBrief, POC

M = Mission(slug="r", topic="Heat pumps", tools=["search"])
BRIEF_JSON = stub_brief(M).model_dump_json()
POCS = [POC(name=f"Person {i}", role="Analyst", email=f"poc{i}@example.com",
            contact_source_url="https://a.example.com/team", why_them="x") for i in range(1, 4)]
CONTACTS_JSON = research_mod.Contacts(contacts=POCS).model_dump_json()
TEAM_PAGE = ("<p>Person poc1@example.com poc2@example.com poc3@example.com</p>", "team")

def tool_call(name, **args):
    return Message(role="assistant", content="", tool_calls=[
        Message.ToolCall(function=Message.ToolCall.Function(name=name, arguments=args))])

def text(content):
    return Message(role="assistant", content=content)

class FakeToolbox:
    calls: list = []
    def __init__(self, enabled): self.tools = [{"type": "function", "function": {"name": "search"}}]
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): pass
    async def call(self, name, args):
        FakeToolbox.calls.append((name, args))
        return "result"

@pytest.fixture(autouse=True)
def reset(monkeypatch):
    FakeToolbox.calls = []
    monkeypatch.setattr(settings, "OUTREACH", False)
    monkeypatch.setattr(validator, "fetch", lambda url: TEAM_PAGE)   # contact check never hits the web

def script(monkeypatch, replies):
    """Make llm.chat return the given replies in order; record what it was sent."""
    seen = []
    it = iter(replies)
    async def fake_chat(messages, tools=None, format=None, model=None):
        seen.append({"tools": tools, "format": format, "model": model})
        reply = next(it)
        if isinstance(reply, Exception):   # a reply that raises, like llm.Overrun
            raise reply
        return reply
    monkeypatch.setattr(llm, "chat", fake_chat)
    return seen

async def test_tool_loop_then_brief(monkeypatch):
    seen = script(monkeypatch, [tool_call("search", query="heat pumps"), text("DONE"), text(BRIEF_JSON)])
    brief = await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert brief.thesis.startswith("Stub thesis")
    assert FakeToolbox.calls == [("search", {"query": "heat pumps"})]
    assert seen[-1]["format"] is not None         # final call asks for the JSON schema
    assert [c["model"] for c in seen] == [llm.SEARCH_MODEL, llm.SEARCH_MODEL, None]   # small model searches, main model writes

async def test_tool_budget_is_enforced(monkeypatch):
    replies = [tool_call("search", query=str(i)) for i in range(100)]
    seen = script(monkeypatch, replies[:research_mod.MAX_TOOL_CALLS] + [text(BRIEF_JSON)])
    await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert len(FakeToolbox.calls) == research_mod.MAX_TOOL_CALLS

async def test_bad_json_gets_one_repair(monkeypatch):
    script(monkeypatch, [text("DONE"), text("{not json"), text(BRIEF_JSON)])
    brief = await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert len(brief.pocs) == 3

async def test_bad_json_twice_raises(monkeypatch):
    script(monkeypatch, [text("DONE"), text("{nope"), text("{still nope")])
    with pytest.raises(research_mod.ResearchFailed):
        await research_mod.run_research(M, toolbox_factory=FakeToolbox)

async def test_validator_notes_reach_the_prompt(monkeypatch):
    captured = {}
    async def fake_chat(messages, tools=None, format=None, model=None):
        captured.setdefault("system", messages[0]["content"])
        return text("DONE") if format is None else text(BRIEF_JSON)
    monkeypatch.setattr(llm, "chat", fake_chat)
    m = M.model_copy(update={"validator_notes": "s1: quote not found"})
    await research_mod.run_research(m, toolbox_factory=FakeToolbox)
    assert "s1: quote not found" in captured["system"]

async def test_brief_writer_is_shown_once_the_main_model_starts(monkeypatch):
    seen = []
    async def fake_chat(messages, tools=None, format=None, model=None):
        if format is None:
            return text("DONE")
        seen.append(activity.current()["agent"])
        return text(BRIEF_JSON)
    monkeypatch.setattr(llm, "chat", fake_chat)
    await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert seen == ["brief"]

async def test_retry_revises_the_rejected_brief(monkeypatch):
    sent = []
    async def fake_chat(messages, tools=None, format=None, model=None):
        sent.append([dict(x) if isinstance(x, dict) else x for x in messages])
        return text("DONE") if format is None else text(BRIEF_JSON)
    monkeypatch.setattr(llm, "chat", fake_chat)
    notes = "s2: could not fetch https://b.example.com\nOnly 2 verified supporting domain(s); need 3"
    m = M.model_copy(update={"validator_notes": "research crashed: RuntimeError: boom",
                             "rejected_brief": RejectedBrief(brief=stub_brief(M), notes=notes)})
    await research_mod.run_research(m, toolbox_factory=FakeToolbox)
    system = sent[0][0]["content"]
    assert "Stub thesis" in system and "https://a.example.com" in system   # the brief itself is passed on
    assert "Sources that passed verification: s1, s3, s4." in system
    assert "s2: could not fetch" in system                                 # its own notes, not the crash
    assert "research crashed" not in system
    assert sent[-1][-1]["content"] == research_mod.REVISE_FINAL

async def test_first_attempt_has_no_revision_prompt(monkeypatch):
    sent = []
    async def fake_chat(messages, tools=None, format=None, model=None):
        sent.append(list(messages))
        return text("DONE") if format is None else text(BRIEF_JSON)
    monkeypatch.setattr(llm, "chat", fake_chat)
    await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert "passed verification" not in sent[0][0]["content"]
    assert sent[-1][-1]["content"] == research_mod.FINAL

async def test_prompt_separates_exploring_possibilities_and_verifying(monkeypatch):
    captured = {}
    async def fake_chat(messages, tools=None, format=None, model=None):
        captured.setdefault("system", messages[0]["content"])
        return text("DONE") if format is None else text(BRIEF_JSON)
    monkeypatch.setattr(llm, "chat", fake_chat)
    await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    stages = ["Explore widely", "Rule out", "Report what is possible", "Pin down the record"]
    positions = [captured["system"].index(s) for s in stages]
    assert positions == sorted(positions)
    assert 'kind "possibility"' in captured["system"] and 'kind "fact"' in captured["system"]

async def test_prompt_is_skeptical_but_keeps_its_limits(monkeypatch):
    captured = {}
    async def fake_chat(messages, tools=None, format=None, model=None):
        captured.setdefault("system", messages[0]["content"])
        return text("DONE") if format is None else text(BRIEF_JSON)
    monkeypatch.setattr(llm, "chat", fake_chat)
    await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert "Assume every source is spinning" in captured["system"]
    assert "Doubt alone proves nothing" in captured["system"]          # suspicion is not evidence
    assert "a named person committed a crime" in captured["system"]    # provocation has a limit

async def test_no_contacts_run_while_outreach_is_off(monkeypatch):
    seen = script(monkeypatch, [text("DONE"), text(BRIEF_JSON)])
    await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert len(seen) == 2                          # one sources run, one brief; nothing else

async def test_outreach_runs_contacts_separately_and_checks_them(monkeypatch):
    monkeypatch.setattr(settings, "OUTREACH", True)
    prompts = []
    replies = iter([tool_call("search", query="heat pumps"), text("DONE"),
                    tool_call("search", query="experts"), text("DONE"), text(CONTACTS_JSON), text(BRIEF_JSON)])
    async def fake_chat(messages, tools=None, format=None, model=None):
        prompts.append((messages[0]["content"], model))
        return next(replies)
    monkeypatch.setattr(llm, "chat", fake_chat)
    brief = await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert "separate run finds them" in prompts[0][0]                 # sources run doesn't hunt contacts
    assert "finding people to interview" in prompts[2][0]             # contacts run has its own prompt
    assert [model for _, model in prompts] == [llm.SEARCH_MODEL] * 5 + [None]
    assert brief.pocs == POCS                                         # the checked contacts, set by code

async def test_unverified_contacts_stop_before_the_main_model(monkeypatch):
    monkeypatch.setattr(settings, "OUTREACH", True)
    monkeypatch.setattr(validator, "fetch", lambda url: ("<p>Person poc1@example.com</p>", "team"))
    seen = script(monkeypatch, [text("DONE"), text("DONE"), text(CONTACTS_JSON), text(BRIEF_JSON)])
    with pytest.raises(research_mod.ResearchFailed, match="only 1 verified contact"):
        await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert all(c["model"] == llm.SEARCH_MODEL for c in seen)          # the main model was never called

def test_source_finder_limit_is_15_minutes():
    assert research_mod.SOURCE_TIME_LIMIT == 15 * 60

async def test_source_finder_stops_at_its_time_limit_and_keeps_what_it_found(monkeypatch):
    monkeypatch.setattr(research_mod, "SOURCE_TIME_LIMIT", 0.05)
    sent = []
    async def fake_chat(messages, tools=None, format=None, model=None):
        if format is not None:
            sent.append(list(messages))
            return text(BRIEF_JSON)
        if FakeToolbox.calls:
            await asyncio.sleep(30)            # the small model hangs on its second turn
        return tool_call("search", query="heat pumps")
    monkeypatch.setattr(llm, "chat", fake_chat)
    brief = await asyncio.wait_for(research_mod.run_research(M, toolbox_factory=FakeToolbox), 5)
    assert brief.thesis.startswith("Stub thesis")
    assert any(isinstance(x, dict) and x.get("role") == "tool" for x in sent[0])   # the search result survived

async def test_time_limit_does_not_cover_the_brief_writer(monkeypatch):
    monkeypatch.setattr(research_mod, "SOURCE_TIME_LIMIT", 0.05)
    async def fake_chat(messages, tools=None, format=None, model=None):
        if format is None:
            return text("DONE")
        await asyncio.sleep(0.1)               # longer than the source finder's limit
        return text(BRIEF_JSON)
    monkeypatch.setattr(llm, "chat", fake_chat)
    assert await research_mod.run_research(M, toolbox_factory=FakeToolbox)

async def test_a_timeout_from_inside_a_call_is_still_a_failure(monkeypatch):
    async def fake_chat(messages, tools=None, format=None, model=None):
        raise TimeoutError("connection timed out")
    monkeypatch.setattr(llm, "chat", fake_chat)
    with pytest.raises(TimeoutError):
        await research_mod.run_research(M, toolbox_factory=FakeToolbox)

async def test_a_stuck_brief_writer_keeps_what_it_wrote(monkeypatch):
    script(monkeypatch, [text("DONE"), llm.Overrun("reply ran past 8192 tokens", BRIEF_JSON[:-1] + "\n" * 500)])
    brief = await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert brief.thesis.startswith("Stub thesis")                   # the validator checks it as usual

async def test_a_stuck_brief_writer_with_nothing_usable_fails_the_attempt(monkeypatch):
    script(monkeypatch, [text("DONE"), llm.Overrun("reply took over 10 minutes", "\n" * 500)])
    with pytest.raises(research_mod.ResearchFailed, match="got stuck"):
        await research_mod.run_research(M, toolbox_factory=FakeToolbox)

async def test_a_stuck_search_turn_ends_the_search_not_the_research(monkeypatch):
    script(monkeypatch, [tool_call("search", query="x"), llm.Overrun("tool turn ran past 2048 tokens"), text(BRIEF_JSON)])
    brief = await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert brief.thesis.startswith("Stub thesis") and FakeToolbox.calls == [("search", {"query": "x"})]
