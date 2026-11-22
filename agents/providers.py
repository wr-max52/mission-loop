"""Cloud model providers: the same model call as Ollama, sent to Anthropic, OpenAI, xAI, DeepSeek, Gemini, Mistral,
Groq, OpenRouter, Together or any OpenAI-compatible server (settings.PROVIDERS says which and where).

Only agents/llm.py calls this. It takes the loop's messages exactly as they are kept for Ollama (dicts and
ollama.Message, tool results by tool name) and gives back an ollama.Message, so no agent knows which provider
answered. Two wire shapes cover every provider: Anthropic's Messages API and the OpenAI chat completions shape.
Plain httpx, like every other network call here, so tests block it with one TRANSPORT.

Thinking is never sent back: a provider's reasoning stays with the turn that produced it (Anthropic accepts a
transcript without its thinking blocks, and DeepSeek refuses one with them). The loop cuts long tool results to
fit, which would invalidate replayed thinking anyway."""
import json, re, time
import httpx, ollama
import settings

TRANSPORT: httpx.BaseTransport | None = None   # tests swap in a MockTransport; async calls get an async twin
TIMEOUT = httpx.Timeout(600, connect=15)
ANTHROPIC_VERSION = "2023-06-01"
TEMPERATURE = 0.2
MODEL_CACHE_SECONDS = 600                  # the Settings page and the chat picker list models often
NOT_CHAT = re.compile(r"embed|whisper|tts|dall-e|image|moderation|audio|realtime|transcri|speech|rerank|guard|"
                      r"ocr|vision-preview|search-preview|computer-use|codex-mini|babbage|davinci", re.I)
EMBED = re.compile(r"embed", re.I)        # an embedding model's name says so (text-embedding-3-small, mistral-embed)
_models: dict[str, tuple[float, str, list[str]]] = {}   # provider -> (when, key+url it was listed with, names)

class ProviderError(Exception):
    """The provider refused or couldn't be reached. The message is safe to show: it never holds the key."""

def spec(pid: str) -> dict:
    return settings.PROVIDERS[pid]

def name(pid: str) -> str:
    return spec(pid)["name"]

# ---------- HTTP ----------

class _AsyncMock(httpx.AsyncBaseTransport):
    """Runs a test's sync MockTransport for the async client."""
    def __init__(self, inner):
        self.inner = inner
    async def handle_async_request(self, request):
        await request.aread()
        return self.inner.handle_request(request)

def _client(timeout=TIMEOUT) -> httpx.Client:
    return httpx.Client(timeout=timeout, transport=TRANSPORT) if TRANSPORT else httpx.Client(timeout=timeout)

def _async_client(timeout=TIMEOUT) -> httpx.AsyncClient:
    if TRANSPORT:
        return httpx.AsyncClient(timeout=timeout, transport=_AsyncMock(TRANSPORT))
    return httpx.AsyncClient(timeout=timeout)

def _headers(pid: str, key: str) -> dict:
    if spec(pid)["api"] == "anthropic":
        return {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
    out = {"content-type": "application/json"}
    if key:
        out["authorization"] = f"Bearer {key}"
    if pid == "openrouter":                # OpenRouter asks apps to name themselves; no personal details
        out["x-title"] = "Mission Loop"
    return out

def _why(r: httpx.Response) -> str:
    """The provider's own short reason, e.g. 'invalid x-api-key'."""
    try:
        body = r.json()
        err = body.get("error") if isinstance(body, dict) else None
        msg = (err.get("message") if isinstance(err, dict) else err) or (body.get("message") if isinstance(body, dict) else "")
    except ValueError:
        msg = r.text
    return f"HTTP {r.status_code}" + (f": {str(msg)[:200]}" if msg else "")

def _fail(pid: str, e: Exception) -> ProviderError:
    if isinstance(e, ProviderError):
        return e
    return ProviderError(f"{name(pid)} is not reachable ({type(e).__name__})")

# ---------- models ----------

def list_models(pid: str, key: str | None = None, url: str | None = None, timeout: float = 15,
                embed: bool = False) -> list[str]:
    """The chat models the provider offers this key, sorted (embed=True: its embedding models instead, for the
    library). Raises ProviderError for a refused key or no answer.
    key and url default to the saved ones; the Settings page passes a key it is about to save, to check it first."""
    key = settings.provider_key(pid) if key is None else key
    url = (settings.provider_url(pid) if url is None else url).rstrip("/")
    path = "/v1/models?limit=1000" if spec(pid)["api"] == "anthropic" else "/models"
    try:
        with _client(timeout) as c:
            r = c.get(url + path, headers=_headers(pid, key))
    except httpx.HTTPError as e:
        raise _fail(pid, e)
    if r.status_code >= 400:
        raise ProviderError(f"{name(pid)} refused ({_why(r)})")
    try:
        body = r.json()
    except ValueError:
        raise ProviderError(f"{name(pid)} answered with something that isn't a model list")
    items = body if isinstance(body, list) else body.get("data") or body.get("models") or []
    names = {str(m.get("id") or m.get("name") or "") if isinstance(m, dict) else str(m) for m in items}
    names = {n.removeprefix("models/") for n in names if n}    # Gemini lists "models/gemini-..."
    if embed:
        return sorted(n for n in names if EMBED.search(n))
    return sorted(n for n in names if not NOT_CHAT.search(n))

def models(pid: str, embed: bool = False) -> tuple[list[str], str]:
    """(names, error) for the provider, cached for MODEL_CACHE_SECONDS. Never raises, so pages render offline.
    embed=True lists its embedding models instead."""
    stamp = settings.provider_key(pid) + "|" + settings.provider_url(pid)
    slot = f"{pid}:embed" if embed else pid
    hit = _models.get(slot)
    if hit and hit[1] == stamp and time.monotonic() - hit[0] < MODEL_CACHE_SECONDS:
        return hit[2], ""
    try:
        found = list_models(pid, timeout=8, embed=embed)
    except ProviderError as e:
        return [], str(e)
    _models[slot] = (time.monotonic(), stamp, found)
    return found, ""

def forget_models(pid: str | None = None) -> None:
    if pid is None:
        _models.clear()
    else:
        _models.pop(pid, None)
        _models.pop(f"{pid}:embed", None)

def family(model: str) -> str:
    """'claude-sonnet-5-5' -> 'claude', 'meta-llama/llama-4-scout' -> 'llama': who the chat panel sends in."""
    last = model.rsplit("/", 1)[-1]
    return re.split(r"[-:.\d]", last, maxsplit=1)[0] or last

# ---------- the loop's messages, in each provider's shape ----------

def _get(m, key):
    return m.get(key) if isinstance(m, dict) else getattr(m, key, None)

def _call(tc) -> tuple[str, dict]:
    fn = _get(tc, "function")
    args = _get(fn, "arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    return str(_get(fn, "name") or ""), dict(args)

def _schema_note(schema: dict) -> str:
    return ("Reply with one JSON object and nothing else (no code fences), matching this JSON schema:\n"
            + json.dumps(schema))

def openai_messages(messages: list) -> list[dict]:
    """Tool calls get ids (Ollama keeps none) and each tool result answers the oldest call still open; a call left
    unanswered is answered with a note, which the API requires."""
    out, open_calls, n = [], [], 0

    def close_open():
        while open_calls:
            out.append({"role": "tool", "tool_call_id": open_calls.pop(0), "content": "No result."})

    for m in messages:
        role, content = _get(m, "role"), _get(m, "content") or ""
        if role == "tool":
            if open_calls:
                out.append({"role": "tool", "tool_call_id": open_calls.pop(0), "content": content})
            else:
                out.append({"role": "user", "content": f"[{_get(m, 'tool_name') or 'tool'} result]\n{content}"})
            continue
        close_open()
        if role == "assistant" and _get(m, "tool_calls"):
            calls = []
            for tc in _get(m, "tool_calls"):
                n += 1
                fname, args = _call(tc)
                calls.append({"id": f"call_{n}", "type": "function",
                              "function": {"name": fname, "arguments": json.dumps(args)}})
                open_calls.append(f"call_{n}")
            out.append({"role": "assistant", "content": content or None, "tool_calls": calls})
        else:
            out.append({"role": role if role in ("system", "user", "assistant") else "user", "content": content})
    close_open()
    return out

def anthropic_body(model: str, messages: list, tools, schema, max_tokens: int) -> dict:
    """System prompts go to `system`; tool calls become tool_use blocks and results tool_result blocks in the
    next user turn, turns of one role are merged, and no text block is empty."""
    system, out, open_calls, n = [], [], [], 0

    def add(role, blocks):
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    def close_open():
        if open_calls:
            add("user", [{"type": "tool_result", "tool_use_id": i, "content": "No result."} for i in open_calls])
            open_calls.clear()

    for m in messages:
        role, content = _get(m, "role"), _get(m, "content") or ""
        if role == "system":
            system.append(content)
        elif role == "tool":
            if open_calls:
                add("user", [{"type": "tool_result", "tool_use_id": open_calls.pop(0), "content": content or "(empty)"}])
            else:
                add("user", [{"type": "text", "text": content or "(empty)"}])
        elif role == "assistant":
            close_open()
            blocks = [{"type": "text", "text": content}] if content.strip() else []
            for tc in _get(m, "tool_calls") or []:
                n += 1
                fname, args = _call(tc)
                blocks.append({"type": "tool_use", "id": f"toolu_{n:04d}", "name": fname, "input": args})
                open_calls.append(f"toolu_{n:04d}")
            add("assistant", blocks or [{"type": "text", "text": "(no reply)"}])
        else:
            close_open()
            add("user", [{"type": "text", "text": content or "."}])
    close_open()
    if not out or out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "Begin."}]})
    body = {"model": model, "max_tokens": max_tokens, "messages": out}
    if schema is not None:
        body["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    if system:
        body["system"] = "\n\n".join(system)
    if tools:
        body["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description") or "",
                          "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
                         for t in tools]
    return body

def openai_body(pid: str, model: str, messages: list, tools, schema, max_tokens: int, schema_mode: bool) -> dict:
    msgs = openai_messages(messages)
    if schema is not None and not schema_mode:
        msgs = [{"role": "system", "content": _schema_note(schema)}, *msgs]
    body = {"model": model, "messages": msgs, spec(pid).get("tokens", "max_tokens"): max_tokens}
    if spec(pid).get("temperature", True):
        body["temperature"] = TEMPERATURE
    if tools:
        body["tools"] = tools               # the loop's tool schemas are already in this shape
    if schema is not None:
        body["response_format"] = ({"type": "json_schema", "json_schema": {"name": "reply", "schema": schema,
                                                                            "strict": False}}
                                   if schema_mode else {"type": "json_object"})
    return body

# ---------- replies ----------

def unfence(text: str) -> str:
    """A JSON reply without the ```json fence some models put around it anyway."""
    m = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", text or "", re.S)
    return m.group(1) if m else (text or "")

def _anthropic_reply(body: dict) -> tuple[ollama.Message, str]:
    if body.get("stop_reason") == "refusal":
        raise ProviderError("Anthropic: the model declined to answer (refusal)")
    text, thinking, calls = [], [], []
    for b in body.get("content") or []:
        if b.get("type") == "text":
            text.append(b.get("text") or "")
        elif b.get("type") == "thinking":
            thinking.append(b.get("thinking") or "")
        elif b.get("type") == "tool_use":
            calls.append(ollama.Message.ToolCall(function=ollama.Message.ToolCall.Function(
                name=b.get("name") or "", arguments=b.get("input") or {})))
    msg = ollama.Message(role="assistant", content="".join(text), thinking="".join(thinking) or None,
                         tool_calls=calls or None)
    return msg, "length" if body.get("stop_reason") == "max_tokens" else "stop"

def _openai_reply(body: dict) -> tuple[ollama.Message, str]:
    choice = (body.get("choices") or [{}])[0]
    m = choice.get("message") or {}
    calls = []
    for tc in m.get("tool_calls") or []:
        fname, args = _call(tc)
        calls.append(ollama.Message.ToolCall(function=ollama.Message.ToolCall.Function(name=fname, arguments=args)))
    thinking = m.get("reasoning_content") or m.get("reasoning") or None
    msg = ollama.Message(role="assistant", content=m.get("content") or "", thinking=thinking or None,
                         tool_calls=calls or None)
    return msg, "length" if choice.get("finish_reason") == "length" else "stop"

async def _sse(response: httpx.Response):
    """The data of each server-sent event."""
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            data = line[5:].strip()
            if data and data != "[DONE]":
                try:
                    yield json.loads(data)
                except ValueError:
                    continue

async def _post(pid: str, path: str, body: dict, parts: list | None) -> tuple[dict | None, str]:
    """POST body; with parts, stream it, adding each piece of text to parts (so a stop keeps what was written).
    Returns (reply body, "") or, streamed, (None, stop reason). Raises ProviderError."""
    url, key = settings.provider_url(pid) + path, settings.provider_key(pid)
    anthropic = spec(pid)["api"] == "anthropic"
    async with _async_client() as c:
        try:
            if parts is None:
                r = await c.post(url, headers=_headers(pid, key), json=body)
                if r.status_code >= 400:
                    raise ProviderError(f"{name(pid)} refused ({_why(r)})")
                return r.json(), ""
            reason = "stop"
            async with c.stream("POST", url, headers=_headers(pid, key), json={**body, "stream": True}) as r:
                if r.status_code >= 400:
                    await r.aread()
                    raise ProviderError(f"{name(pid)} refused ({_why(r)})")
                async for event in _sse(r):
                    if anthropic:
                        if event.get("type") == "content_block_delta" and event["delta"].get("type") == "text_delta":
                            parts.append(event["delta"].get("text") or "")
                        elif event.get("type") == "message_delta":
                            stop = (event.get("delta") or {}).get("stop_reason")
                            if stop == "refusal":
                                raise ProviderError("Anthropic: the model declined to answer (refusal)")
                            reason = "length" if stop == "max_tokens" else reason
                        elif event.get("type") == "error":
                            raise ProviderError(f"Anthropic: {(event.get('error') or {}).get('message', 'stream error')}")
                    else:
                        choice = (event.get("choices") or [{}])[0]
                        parts.append((choice.get("delta") or {}).get("content") or "")
                        if choice.get("finish_reason") == "length":
                            reason = "length"
            return None, reason
        except (httpx.HTTPError, ValueError) as e:
            raise _fail(pid, e)

async def chat(pid: str, model: str, messages: list, tools=None, schema=None, max_tokens: int = 8192,
               parts: list | None = None) -> tuple[ollama.Message, str]:
    """One call to a cloud provider. Returns (message, done reason: "length" when it hit max_tokens, else "stop").
    A structured reply (schema set) is streamed into `parts` when given. A provider that refuses the schema itself
    (some can't hold every JSON schema) is asked once more with the schema in words instead."""
    if not settings.provider_on(pid):
        raise ProviderError(f"{name(pid)} isn't connected: add its API key on the Settings page")
    stream = parts if schema is not None and not tools else None
    anthropic = spec(pid)["api"] == "anthropic"
    schema_mode = schema is not None and spec(pid).get("schema", True)
    for attempt in (1, 2):
        if anthropic:
            body = anthropic_body(model, messages, tools, schema if schema_mode else None, max_tokens)
            if schema is not None and not schema_mode:
                body["system"] = (body.get("system", "") + "\n\n" + _schema_note(schema)).strip()
            path = "/v1/messages"
        else:
            body, path = openai_body(pid, model, messages, tools, schema, max_tokens, schema_mode), "/chat/completions"
        try:
            got, reason = await _post(pid, path, body, stream)
        except ProviderError as e:
            if schema_mode and attempt == 1 and "HTTP 400" in str(e):
                schema_mode = False        # e.g. a schema feature it doesn't support: JSON mode and the schema in words
                if stream is not None:
                    stream.clear()
                continue
            raise
        break
    if got is None:                         # streamed: the text is in parts
        msg = ollama.Message(role="assistant", content=unfence("".join(stream)))
    else:
        msg, reason = _anthropic_reply(got) if anthropic else _openai_reply(got)
        if schema is not None:
            msg.content = unfence(msg.content or "")
    return msg, reason
