"""Thin wrapper over the Ollama client so tests can replace one function."""
import asyncio, os
import ollama
import stopping, trail

MODEL = os.environ.get("MISSION_LOOP_MODEL", "qwen3.8:27b")
SEARCH_MODEL = os.environ.get("MISSION_LOOP_SEARCH_MODEL", "bonsai-2:4b")   # research tool loops only
OPTIONS = {"num_ctx": 32768, "temperature": 0.2}

class Cut(Exception):
    """The stop button's grace ran out mid-reply. text is whatever the model had written by then."""
    def __init__(self, text: str = ""):
        super().__init__("cut off by the stop button")
        self.text = text

async def chat(messages, tools=None, format=None, model=None, stoppable=True) -> ollama.Message:
    """One model call. Structured outputs (format set) are streamed so that, if stop is pressed, what
    was written before the grace ran out isn't lost: it comes back on Cut. A tool turn in flight when
    stop is pressed gets the shorter turn grace, so the write-out still has time.
    stoppable=False ignores the stop button: the chat assistant isn't loop work, and data/STOP can
    outlive a stopped loop."""
    client = ollama.AsyncClient()          # new client per call: safe across asyncio.run() loops
    if not stoppable:
        return (await client.chat(model=model or MODEL, messages=messages, tools=tools, format=format,
                                  options=OPTIONS, keep_alive="24h")).message
    parts: list[str] = []

    async def call():
        kw = dict(model=model or MODEL, messages=messages, tools=tools, format=format,
                  options=OPTIONS, keep_alive="24h")
        if format is None:
            return (await client.chat(**kw)).message
        async for chunk in await client.chat(**kw, stream=True):
            parts.append(chunk.message.content or "")
        return ollama.Message(role="assistant", content="".join(parts))

    grace = stopping.GRACE if format is not None else stopping.TURN_GRACE
    task = asyncio.ensure_future(call())
    try:
        while True:
            left = stopping.left(grace)
            if left is not None and left <= 0:
                raise Cut("".join(parts))
            await asyncio.wait({task}, timeout=1 if left is None else min(1, left))
            if task.done():
                trail.model_reply(task.result())   # only inside the loop; the chat assistant leaves no trail
                return task.result()
    finally:
        if not task.done():                # cut off or cancelled from outside: closing the request
            task.cancel()                  # makes Ollama stop generating
            try:
                await task
            except BaseException:
                pass

def ready() -> tuple[bool, str]:
    """Is Ollama up with both models pulled? Checked before each pass so a stopped Ollama app
    doesn't burn every queued mission's attempts and park them all."""
    try:
        names = {m.model for m in ollama.list().models}
    except Exception as e:
        return False, f"Ollama is not reachable ({type(e).__name__}); start the Ollama app"
    for model in (MODEL, SEARCH_MODEL):
        if model not in names:
            return False, f"model {model} is not pulled; run: ollama pull {model}"
    return True, f"{MODEL} + {SEARCH_MODEL}"
