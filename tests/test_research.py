import json
import pytest
from ollama import Message
from agents import researcher as research_mod
from agents import llm
from agents.stubs import research as stub_brief
from contracts import Mission

M = Mission(slug="r", topic="Heat pumps", tools=["search"])
BRIEF_JSON = stub_brief(M).model_dump_json()

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
def reset():
    FakeToolbox.calls = []

def script(monkeypatch, replies):
    """Make llm.chat return the given replies in order; record what it was sent."""
    seen = []
    it = iter(replies)
    async def fake_chat(messages, tools=None, format=None):
        seen.append({"tools": tools, "format": format})
        return next(it)
    monkeypatch.setattr(llm, "chat", fake_chat)
    return seen

async def test_tool_loop_then_brief(monkeypatch):
    seen = script(monkeypatch, [tool_call("search", query="heat pumps"), text("DONE"), text(BRIEF_JSON)])
    brief = await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert brief.thesis.startswith("Stub thesis")
    assert FakeToolbox.calls == [("search", {"query": "heat pumps"})]
    assert seen[-1]["format"] is not None         # final call asks for the JSON schema

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
    async def fake_chat(messages, tools=None, format=None):
        captured.setdefault("system", messages[0]["content"])
        return text("DONE") if format is None else text(BRIEF_JSON)
    monkeypatch.setattr(llm, "chat", fake_chat)
    m = M.model_copy(update={"validator_notes": "s1: quote not found"})
    await research_mod.run_research(m, toolbox_factory=FakeToolbox)
    assert "s1: quote not found" in captured["system"]
