"""Thin wrapper over the Ollama client so tests can replace one function. Every call goes to the configured
LLM connection (settings.ollama_host) with the configured models (settings.model_for); none are written here."""
import asyncio
import ollama
import settings, stopping, trail

def model_for(role: str) -> str:
    """The model for a role's steps: "assistant" (search and read) or "pi" (write). See settings.WORKERS."""
    return settings.model_for(role)

OPTIONS = {"num_ctx": 32768, "temperature": 0.2}

class Cut(Exception):
    """The stop button's grace ran out mid-reply. text is whatever the model had written by then."""
    def __init__(self, text: str = ""):
        super().__init__("cut off by the stop button")
        self.text = text

async def chat(messages, tools=None, format=None, model=None, stoppable=True) -> ollama.Message:
    """One model call, on the Principal Investigator's model unless `model` names another. Structured outputs
    (format set) are streamed so that, if stop is pressed, what was written before the grace ran out isn't
    lost: it comes back on Cut. A tool turn in flight when stop is pressed gets the shorter turn grace, so the
    write-out still has time. stoppable=False ignores the stop button: the chat assistant isn't loop work, and
    data/STOP can outlive a stopped loop."""
    client = ollama.AsyncClient(host=settings.ollama_host())   # new client per call: safe across asyncio.run() loops
    if not stoppable:
        return (await client.chat(model=model or model_for("pi"), messages=messages, tools=tools, format=format,
                                  options=OPTIONS, keep_alive="24h")).message
    parts: list[str] = []

    async def call():
        kw = dict(model=model or model_for("pi"), messages=messages, tools=tools, format=format,
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

def connection(timeout: float = 2) -> dict:
    """What the Ollama connection offers: {"host", "ok", "error", "models": [{"name", "size"}], "loaded": {name: size}}.
    Never raises, so the Settings page renders with Ollama closed."""
    host = settings.ollama_host()
    out = {"host": host, "ok": False, "error": "", "models": [], "loaded": {}}
    try:
        client = ollama.Client(host=host, timeout=timeout)
        out["models"] = sorted(({"name": m.model, "size": m.size or 0} for m in client.list().models),
                               key=lambda m: m["name"])
        out["loaded"] = {m.model: m.size or 0 for m in client.ps().models}
        out["ok"] = True
    except Exception as e:
        out["error"] = type(e).__name__
    return out

def ready() -> tuple[bool, str]:
    """Is Ollama up with every working model pulled? Checked before each pass so a stopped Ollama app
    doesn't burn every queued mission's attempts and park them all."""
    missing = settings.missing_models()
    if missing:
        who = " and ".join(f"the {settings.WORKERS[w]}" for w in missing)
        return False, f"no model chosen for {who}; pick one on the Settings page (or set it in .env)"
    host = settings.ollama_host()
    try:
        names = {m.model for m in ollama.Client(host=host, timeout=10).list().models}
    except Exception as e:
        return False, f"Ollama is not reachable at {host} ({type(e).__name__}); start the Ollama app"
    working = [w for w in settings.WORKERS if settings.worker_on(w)]
    for w in working:
        model = settings.worker_model(w)
        if model not in names:
            return False, f"model {model} ({settings.WORKERS[w]}) is not pulled; run: ollama pull {model}"
    return True, " + ".join(f"{settings.worker_model(w)} ({settings.WORKERS[w]})" for w in working) \
        + (", in parallel" if len(working) > 1 else "")
