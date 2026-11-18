import pytest
from ollama import Message
from agents import llm, writer
from agents.stubs import research as stub_brief, write as stub_article
from contracts import Mission

M = Mission(slug="w", topic="Heat pumps")
BRIEF = stub_brief(M)
ARTICLE_JSON = stub_article(M, BRIEF, []).model_dump_json()

def script(monkeypatch, replies):
    seen = []
    it = iter(replies)
    async def fake_chat(messages, tools=None, format=None):
        seen.append({"system": messages[0]["content"], "tools": tools, "format": format})
        return Message(role="assistant", content=next(it))
    monkeypatch.setattr(llm, "chat", fake_chat)
    return seen

async def test_writer_returns_article_with_schema(monkeypatch):
    seen = script(monkeypatch, [ARTICLE_JSON])
    a = await writer.run_writer(M, BRIEF, [])
    assert a.title == BRIEF.thesis
    assert seen[0]["format"] is not None and seen[0]["tools"] is None   # structured output, no tools

async def test_bad_json_gets_one_repair(monkeypatch):
    script(monkeypatch, ["{nope", ARTICLE_JSON])
    assert (await writer.run_writer(M, BRIEF, [])).sections

async def test_bad_json_twice_raises(monkeypatch):
    script(monkeypatch, ["{nope", "{still nope"])
    with pytest.raises(writer.WritingFailed):
        await writer.run_writer(M, BRIEF, [])

async def test_prompt_keeps_possibilities_labeled(monkeypatch):
    seen = script(monkeypatch, [ARTICLE_JSON])
    await writer.run_writer(M, BRIEF, [])
    assert 'kind "possibility" are unverified' in seen[0]["system"]

async def test_prompt_has_notes_and_no_contact_emails(monkeypatch):
    seen = script(monkeypatch, [ARTICLE_JSON])
    m = M.model_copy(update={"validator_notes": "paragraph 2 has no citation"})
    await writer.run_writer(m, BRIEF, [])
    assert "paragraph 2 has no citation" in seen[0]["system"]
    assert "poc1@example.com" not in seen[0]["system"]                  # POC emails never reach the article
