"""The Ollama wrapper's limits: prompts cut to fit the context, replies capped in tokens and time."""
import asyncio
import pytest
from ollama import ChatResponse, Message
from agents import llm

def room(reply_tokens):
    return (llm.CONTEXT - reply_tokens - llm.TOOLS_ROOM) * llm.CHARS_PER_TOKEN

def test_fit_leaves_a_small_prompt_alone():
    messages = [{"role": "system", "content": "rules"}, {"role": "tool", "content": "page"}]
    assert llm.fit(messages, llm.REPLY_TOKENS) is messages

def test_fit_cuts_the_longest_tool_results_and_keeps_the_rest_whole():
    big = room(llm.REPLY_TOKENS)
    messages = [{"role": "system", "content": "rules " * 100},
                {"role": "tool", "content": "short search result"},
                {"role": "tool", "content": "a" * big},
                Message(role="assistant", content="thinking"),
                {"role": "tool", "content": "b" * big},
                {"role": "user", "content": "Write the brief."}]
    out = llm.fit(messages, llm.REPLY_TOKENS)
    assert sum(len(m["content"] if isinstance(m, dict) else m.content) for m in out) <= big + 50
    assert out[0] is messages[0] and out[-1] is messages[-1]           # instructions and the request stay whole
    assert out[1]["content"] == "short search result"
    assert out[2]["content"].endswith("[cut to fit]") and out[2]["content"].startswith("aaa")
    assert len(out[2]["content"]) == len(out[4]["content"])            # the two big pages share the room
    assert len(messages[2]["content"]) == big                          # the transcript itself is untouched

class FakeClient:
    """Stands in for ollama.AsyncClient; records what it was sent."""
    sent: list = []
    def __init__(self, chunks=None, response=None, delay=0):
        self.chunks, self.response, self.delay = chunks, response, delay
    async def chat(self, stream=False, **kw):
        FakeClient.sent.append(kw)
        await asyncio.sleep(self.delay)
        if not stream:
            return self.response
        async def gen():
            for c in self.chunks:
                yield c
        return gen()

def use(monkeypatch, **kw):
    FakeClient.sent = []
    monkeypatch.setattr(llm.ollama, "AsyncClient", lambda: FakeClient(**kw))

def chunk(text, done_reason=None):
    return ChatResponse(message=Message(role="assistant", content=text),
                        done=done_reason is not None, done_reason=done_reason)

async def test_structured_reply_is_capped_in_tokens(monkeypatch):
    use(monkeypatch, chunks=[chunk('{"thesis": "x"'), chunk("\n" * 50, "length")])
    with pytest.raises(llm.Overrun) as e:
        await llm.chat([{"role": "user", "content": "go"}], format={"type": "object"})
    assert e.value.text.startswith('{"thesis": "x"')
    assert FakeClient.sent[0]["options"]["num_predict"] == llm.REPLY_TOKENS
    assert FakeClient.sent[0]["options"]["num_ctx"] == llm.CONTEXT

async def test_a_finished_reply_comes_back(monkeypatch):
    use(monkeypatch, chunks=[chunk('{"a": '), chunk("1}", "stop")])
    msg = await llm.chat([{"role": "user", "content": "go"}], format={"type": "object"})
    assert msg.content == '{"a": 1}'

async def test_tool_turns_get_a_smaller_cap(monkeypatch):
    use(monkeypatch, response=ChatResponse(message=Message(role="assistant", content="DONE"),
                                           done=True, done_reason="stop"))
    msg = await llm.chat([{"role": "user", "content": "go"}], tools=[])
    assert msg.content == "DONE" and FakeClient.sent[0]["options"]["num_predict"] == llm.TURN_TOKENS

async def test_a_reply_that_takes_too_long_is_given_up(monkeypatch):
    monkeypatch.setattr(llm, "REPLY_TIME_LIMIT", 0.5)
    use(monkeypatch, chunks=[chunk("{}", "stop")], delay=30)
    with pytest.raises(llm.Overrun, match="took over"):
        await asyncio.wait_for(llm.chat([{"role": "user", "content": "go"}], format={}), 10)

async def test_the_prompt_sent_is_cut_to_fit(monkeypatch):
    use(monkeypatch, chunks=[chunk("{}", "stop")])
    page = "p" * room(llm.REPLY_TOKENS) * 2
    await llm.chat([{"role": "user", "content": "go"}, {"role": "tool", "content": page}], format={})
    assert FakeClient.sent[0]["messages"][1]["content"].endswith("[cut to fit]")
