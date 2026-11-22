"""Every model call goes through here, so tests can replace one function. A model is an Ollama model (a bare name,
sent to settings.ollama_host) or a cloud provider's ("anthropic/claude-...", sent by agents/providers.py); either
way the agents get an ollama.Message back. Models come from config (settings.model_for); none are written here."""
import asyncio, time
import ollama
import settings, stopping, trail
from agents import providers

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
# A cloud model has far more room than a local 32k context, but every token is paid for, so the loop still keeps
# its prompts bounded. Its replies get a bigger cap: reasoning models spend part of it thinking before they answer.
CLOUD_CONTEXT = 65536
CLOUD_REPLY_TOKENS = 16384

def context_for(model: str | None) -> int:
    """How many tokens of prompt and reply the loop gives a model: CONTEXT on Ollama, CLOUD_CONTEXT elsewhere."""
    return CONTEXT if settings.split_model(model or "")[0] == settings.LOCAL else CLOUD_CONTEXT

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

def invalid(e) -> str:
    """The repair request for a reply that failed its schema: one short line per problem (pydantic's own
    text repeats the whole input and adds links, which only confuse a small model)."""
    lines = [f"- {'.'.join(map(str, err['loc'])) or 'reply'}: {err['msg']}" for err in e.errors()[:10]]
    return "That JSON was invalid:\n" + "\n".join(lines) + "\nReturn corrected JSON only."

COMPACT_TO = 0.7                           # share of the room a compaction leaves used, so the next turns only append

def fit(messages: list, reply_tokens: int, model: str | None = None) -> list:
    """The messages, cut to fit the context with room for the reply. Prompts that overflow get cut
    from the front by Ollama, which loses the instructions, or not at all, which lets the model run
    on forever. Only tool results are shortened: the longest ones lose their ends first, so short
    results (searches, quoted sentences) stay whole. The list passed in is not changed. model picks the room
    (context_for); none means Ollama's."""
    room = (context_for(model) - reply_tokens - TOOLS_ROOM) * CHARS_PER_TOKEN
    return _cut(messages, room, room, _tool_results(messages))

def compact(messages: list, reply_tokens: int, model: str | None = None) -> list:
    """fit, for a transcript that keeps growing (the search loop). Ollama reuses its cache for the
    unchanged start of a prompt, but once a transcript is full fit recuts every long result on every
    turn, so the whole prompt is read again each turn. compact cuts deeper (to COMPACT_TO of the
    room), so the next few turns only append. Results the model hasn't read yet, after its last
    reply, stay whole when the room allows."""
    room = (context_for(model) - reply_tokens - TOOLS_ROOM) * CHARS_PER_TOKEN
    tools = _tool_results(messages)
    last = max((i for i, m in enumerate(messages) if _get(m, "role") == "assistant"), default=-1)
    seen = [i for i in tools if i < last]
    target = int(room * COMPACT_TO)
    fixed = sum(_size(m) for i, m in enumerate(messages) if i not in seen)
    return _cut(messages, room, target, seen if fixed <= target else tools)

def _tool_results(messages: list) -> list[int]:
    return [i for i, m in enumerate(messages) if _get(m, "role") == "tool"]

def _size(m) -> int:
    """Characters a message adds to the prompt: its text, thinking and tool calls."""
    calls = _get(m, "tool_calls")
    return len(_get(m, "content") or "") + len(_get(m, "thinking") or "") + (len(str(calls)) if calls else 0)

def _cut(messages: list, room: int, target: int, cuttable: list[int]) -> list:
    """messages unchanged if they fit in room, else with the cuttable results cut so all fit in target."""
    if sum(_size(m) for m in messages) <= room:
        return messages
    fixed = sum(_size(m) for i, m in enumerate(messages) if i not in cuttable)
    sizes = [_size(messages[i]) for i in cuttable]
    if not sizes:
        return messages
    cap = _cap(sizes, max(target - fixed, 0))
    out = list(messages)
    for i, size in zip(cuttable, sizes):
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

async def chat(messages, tools=None, format=None, model=None, stoppable=True, think=None) -> ollama.Message:
    """One model call, on the Principal Investigator's model unless `model` names another. Structured outputs
    (format set) are streamed so that, if stop is pressed, what was written before the grace ran out isn't
    lost: it comes back on Cut. A tool turn in flight when stop is pressed gets the shorter turn grace, so the
    write-out still has time. A reply that hits its token cap or REPLY_TIME_LIMIT raises Overrun with what it
    had written. stoppable=False ignores the stop button: the chat assistant isn't loop work, and data/STOP
    can outlive a stopped loop. A cloud model ("provider/name") goes the same way through agents/providers.py.
    think=False asks an Ollama thinking model to answer straight away (the avatar studio's quick edits); None leaves
    the model's own default, and cloud models ignore it."""
    model = model or model_for("pi")
    provider, name = settings.split_model(model)
    local = provider == settings.LOCAL
    if not stoppable:
        if not local:
            return (await providers.chat(provider, name, messages, tools, format, CLOUD_REPLY_TOKENS))[0]
        client = ollama.AsyncClient(host=settings.ollama_host())
        extra = {} if think is None else {"think": think}
        return (await client.chat(model=name, messages=messages, tools=tools, format=format,
                                  options=OPTIONS, keep_alive="24h", **extra)).message
    parts: list[str] = []
    reply_tokens = TURN_TOKENS if format is None else REPLY_TOKENS

    async def call():
        if not local:
            msg, reason = await providers.chat(provider, name, fit(messages, reply_tokens, model), tools, format,
                                               CLOUD_REPLY_TOKENS, parts)
            if reason == "length" and not msg.tool_calls:
                raise Overrun(f"reply ran past {CLOUD_REPLY_TOKENS} tokens", msg.content or "".join(parts))
            return msg
        client = ollama.AsyncClient(host=settings.ollama_host())   # new client per call: safe across asyncio.run()
        kw = dict(model=name, messages=fit(messages, reply_tokens), tools=tools, format=format,
                  options={**OPTIONS, "num_predict": reply_tokens}, keep_alive="24h")
        try:
            if format is None:
                response = await client.chat(**kw)
                if response.done_reason == "length" and not response.message.tool_calls:
                    raise Overrun(f"tool turn ran past {reply_tokens} tokens", response.message.content or "")
                return response.message
            async for chunk in await client.chat(**kw, stream=True):
                parts.append(chunk.message.content or "")
                if chunk.done and chunk.done_reason == "length":
                    raise Overrun(f"reply ran past {reply_tokens} tokens", "".join(parts))
        except ollama.ResponseError as e:  # Ollama gives up on a model repeating itself: that is stuck, not broken
            if "repeat" not in str(e).lower():
                raise
            raise Overrun("the model got stuck repeating itself", "".join(parts)) from e
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
            task.cancel()                  # closes the request, which makes the model stop generating
            try:
                await task
            except BaseException:
                pass

async def ask(messages: list, schema: type, who: str, failure: type[Exception] = RuntimeError,
              model: str | None = None):
    """One structured reply on the Principal Investigator's model (unless `model`), as a `schema` object.
    Malformed JSON gets one repair round. A reply that overruns keeps the valid part it wrote before (if
    none, `failure` is raised); a stop raises stopping.Interrupted with whatever valid part got out."""
    messages, error = list(messages), None
    for _ in range(2):
        try:
            msg = await chat(messages, format=schema.model_json_schema(), **({"model": model} if model else {}))
        except Cut as cut:                 # stop's grace ran out: keep what got written
            raise stopping.Interrupted(stopping.salvage(cut.text, schema))
        except Overrun as e:               # stuck: keep what it wrote before, if that holds an answer
            got = stopping.salvage(e.text, schema)
            if got is None:
                raise failure(f"the {who} got stuck: {e}")
            print(f"{who}: {e}; keeping what it wrote before that", flush=True)
            return got
        try:
            return schema.model_validate_json(msg.content)
        except Exception as e:             # pydantic's ValidationError
            if stopping.requested():       # no time for a repair round
                raise stopping.Interrupted(stopping.salvage(msg.content, schema))
            error = e
            messages += [{"role": "assistant", "content": msg.content},
                         {"role": "user", "content": invalid(e) if hasattr(e, "errors") else str(e)}]
    raise failure(f"the {who} could not produce valid JSON: {error}")

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

def catalog(conn: dict | None = None) -> list[dict]:
    """Every model the workers can pick, Ollama's first: [{"ref", "provider", "name", "size", "loaded"}]. ref is what
    is saved (a bare name on Ollama, "provider/name" elsewhere). Cloud lists are cached; nothing here raises."""
    conn = conn or connection()
    out = [{"ref": m["name"], "provider": settings.LOCAL, "name": m["name"], "size": m["size"],
            "loaded": conn["loaded"].get(m["name"], 0)} for m in conn["models"]]
    for pid in settings.cloud_providers():
        out += [{"ref": settings.model_ref(pid, n), "provider": pid, "name": n, "size": 0, "loaded": 0}
                for n in providers.models(pid)[0]]
    return out

def ready() -> tuple[bool, str]:
    """Can every working model answer? Ollama up with the model pulled, or the cloud provider connected and taking
    its key. Checked before each pass so a stopped Ollama app or a revoked key doesn't burn every queued mission's
    attempts and park them all. Ollama is only asked when a worker uses it."""
    missing = settings.missing_models()
    if missing:
        who = " and ".join(f"the {settings.WORKERS[w]}" for w in missing)
        return False, f"no model chosen for {who}; pick one on the Settings page (or set it in .env)"
    working = [w for w in settings.WORKERS if settings.worker_on(w)]
    by_provider: dict[str, list[str]] = {}
    for w in working:
        by_provider.setdefault(settings.split_model(settings.worker_model(w))[0], []).append(w)
    for pid, workers in by_provider.items():
        if pid == settings.LOCAL:
            host = settings.ollama_host()
            try:
                names = {m.model for m in ollama.Client(host=host, timeout=10).list().models}
            except Exception as e:
                return False, f"Ollama is not reachable at {host} ({type(e).__name__}); start the Ollama app"
            for w in workers:
                model = settings.split_model(settings.worker_model(w))[1]
                if model not in names:
                    return False, f"model {model} ({settings.WORKERS[w]}) is not pulled; run: ollama pull {model}"
            continue
        label = providers.name(pid)
        if not settings.provider_on(pid):
            env = settings.PROVIDERS[pid]["key_env"]
            return False, (f"{label} isn't connected, so the {settings.WORKERS[workers[0]]} can't use it; add its "
                           f"API key on the Settings page" + (f" (or {env} in .env)" if env else ""))
        try:
            providers.list_models(pid, timeout=15)
        except providers.ProviderError as e:
            return False, str(e)
    def who(w):
        n = settings.worker_count(w)
        return f"{settings.WORKERS[w]}{f' x{n}' if n > 1 else ''}"
    return True, " + ".join(f"{settings.worker_model(w)} ({who(w)})" for w in working) \
        + (", in parallel" if len(settings.seats()) > 1 else "")
