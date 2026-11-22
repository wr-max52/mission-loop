"""Thin wrapper over the Ollama client so tests can replace one function. Every call goes to the configured
LLM connection (settings.ollama_host) with the configured models (settings.model_for); none are written here."""
import asyncio, time
import ollama
import settings, stopping, trail

def model_for(role: str) -> str:
    """The model for a role's steps: "assistant" (search and read) or "pi" (write). See settings.WORKERS."""
    return settings.model_for(role)

CONTEXT = 32768                            # tokens the model sees: prompt and reply together
OPTIONS = {"num_ctx": CONTEXT, "temperature": 0.2}
REPLY_TOKENS = 8192                        # longest structured reply (a brief is about 4k tokens)
TURN_TOKENS = 2048                         # longest tool turn
REPLY_TIME_LIMIT = 10 * 60                 # seconds one call may take (a brief takes about 2 minutes)
CHARS_PER_TOKEN = 3                        # rough and on the safe side: English runs nearer 4
TOOLS_ROOM = 2048                          # tokens left over for the tool definitions and chat template

class Cut(Exception):
    """The stop button's grace ran out mid-reply. text is whatever the model had written by then."""
    def __init__(self, text: str = ""):
        super().__init__("cut off by the stop button")
        self.text = text

class Overrun(Exception):
    """A reply hit its token cap or the time limit, usually a model stuck repeating itself.
    text is whatever it had written, which may still hold a usable answer before the junk."""
    def __init__(self, why: str, text: str = ""):
        super().__init__(why)
        self.text = text

def fit(messages: list, reply_tokens: int) -> list:
    """The messages, cut to fit the context with room for the reply. Prompts that overflow get cut
    from the front by Ollama, which loses the instructions, or not at all, which lets the model run
    on forever. Only tool results are shortened: the longest ones lose their ends first, so short
    results (searches, quoted sentences) stay whole. The list passed in is not changed."""
    room = (CONTEXT - reply_tokens - TOOLS_ROOM) * CHARS_PER_TOKEN
    tools = [i for i, m in enumerate(messages) if _get(m, "role") == "tool"]
    fixed = sum(len(_get(m, "content") or "") for i, m in enumerate(messages) if i not in tools)
    sizes = [len(_get(messages[i], "content") or "") for i in tools]
    left = room - fixed
    if sum(sizes) <= left:
        return messages
    cap = _cap(sizes, max(left, 0))
    out = list(messages)
    for i, size in zip(tools, sizes):
        if size > cap:
            out[i] = {**_as_dict(messages[i]), "content": _get(messages[i], "content")[:cap] + "\n[cut to fit]"}
    return out

def _cap(sizes: list[int], room: int) -> int:
    """The largest per-result length so that the results, each cut to it, fit in room."""
    lo, hi = 0, max(sizes)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if sum(min(s, mid) for s in sizes) <= room:
            lo = mid
        else:
            hi = mid - 1
    return lo

def _get(m, key):
    return m.get(key) if isinstance(m, dict) else getattr(m, key, None)

def _as_dict(m) -> dict:
    return m if isinstance(m, dict) else m.model_dump(exclude_none=True)

async def chat(messages, tools=None, format=None, model=None, stoppable=True) -> ollama.Message:
    """One model call, on the Principal Investigator's model unless `model` names another. Structured outputs
    (format set) are streamed so that, if stop is pressed, what was written before the grace ran out isn't
    lost: it comes back on Cut. A tool turn in flight when stop is pressed gets the shorter turn grace, so the
    write-out still has time. A reply that hits its token cap or REPLY_TIME_LIMIT raises Overrun with what it
    had written. stoppable=False ignores the stop button: the chat assistant isn't loop work, and data/STOP
    can outlive a stopped loop."""
    client = ollama.AsyncClient(host=settings.ollama_host())   # new client per call: safe across asyncio.run() loops
    if not stoppable:
        return (await client.chat(model=model or model_for("pi"), messages=messages, tools=tools, format=format,
                                  options=OPTIONS, keep_alive="24h")).message
    parts: list[str] = []
    reply_tokens = TURN_TOKENS if format is None else REPLY_TOKENS

    async def call():
        kw = dict(model=model or model_for("pi"), messages=fit(messages, reply_tokens), tools=tools, format=format,
                  options={**OPTIONS, "num_predict": reply_tokens}, keep_alive="24h")
        if format is None:
            response = await client.chat(**kw)
            if response.done_reason == "length" and not response.message.tool_calls:
                raise Overrun(f"tool turn ran past {reply_tokens} tokens", response.message.content or "")
            return response.message
        async for chunk in await client.chat(**kw, stream=True):
            parts.append(chunk.message.content or "")
            if chunk.done and chunk.done_reason == "length":
                raise Overrun(f"reply ran past {reply_tokens} tokens", "".join(parts))
        return ollama.Message(role="assistant", content="".join(parts))

    grace = stopping.GRACE if format is not None else stopping.TURN_GRACE
    started = time.monotonic()
    task = asyncio.ensure_future(call())
    try:
        while True:
            left = stopping.left(grace)
            if left is not None and left <= 0:
                raise Cut("".join(parts))
            if time.monotonic() - started > REPLY_TIME_LIMIT:
                raise Overrun(f"reply took over {REPLY_TIME_LIMIT // 60} minutes", "".join(parts))
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
