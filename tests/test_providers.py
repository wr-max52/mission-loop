"""Model providers: Ollama stays the bare-name default; cloud providers plug in with a key and answer like Ollama."""
import asyncio, json, os, stat
from pathlib import Path
import httpx, ollama, pytest
from starlette.testclient import TestClient
import orchestrator, settings, ui
from agents import assistant, llm, providers

ANTHROPIC_MODELS = {"data": [{"id": "claude-sonnet-test"}, {"id": "claude-haiku-test"}]}
OPENAI_MODELS = {"data": [{"id": "gpt-test"}, {"id": "text-embedding-test"}, {"id": "whisper-test"}]}

def serve(monkeypatch, handler):
    """Answer every provider request with handler(request) -> httpx.Response; returns the requests seen."""
    seen = []
    def handle(request):
        seen.append(request)
        return handler(request)
    monkeypatch.setattr(providers, "TRANSPORT", httpx.MockTransport(handle))
    return seen

def sse(*events) -> httpx.Response:
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(orchestrator, "LOCK_FILE", Path("data/loop.lock"))
    monkeypatch.setattr(orchestrator, "WAKE_FLAG", Path("data/WAKE"))
    return TestClient(ui.app)

# ---------- config ----------

def test_a_bare_model_name_is_still_ollama():
    assert settings.split_model("qwen3:30b") == ("ollama", "qwen3:30b")
    assert settings.split_model("huihui_ai/qwen3:30b") == ("ollama", "huihui_ai/qwen3:30b")   # an Ollama namespace
    assert settings.split_model("anthropic/claude-x") == ("anthropic", "claude-x")
    assert settings.split_model("openrouter/meta-llama/llama-4") == ("openrouter", "meta-llama/llama-4")
    assert settings.split_model("ollama/qwen3:30b") == ("ollama", "qwen3:30b")
    assert settings.model_ref("anthropic", "claude-x") == "anthropic/claude-x" and settings.model_ref("ollama", "q") == "q"

def test_keys_are_saved_privately_and_win_over_env(monkeypatch):
    assert settings.cloud_providers() == [] and settings.provider_on("ollama")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    assert settings.cloud_providers() == ["openai"] and not settings.provider_saved("openai")
    settings.save_provider("openai", "sk-saved")
    assert settings.provider_key("openai") == "sk-saved" and settings.provider_saved("openai")
    assert stat.S_IMODE(os.stat(settings.PROVIDERS_FILE).st_mode) == 0o600
    settings.forget_provider("openai")
    assert settings.provider_key("openai") == "sk-from-env"
    with pytest.raises(ValueError):
        settings.save_provider("anthropic", "")                    # a key is needed
    with pytest.raises(ValueError):
        settings.save_provider("custom", "")                       # a custom server needs its address
    settings.save_provider("custom", "", "http://127.0.0.1:1234/v1/")
    assert settings.provider_on("custom") and settings.provider_url("custom") == "http://127.0.0.1:1234/v1"

# ---------- message shapes ----------

def _transcript():
    call = ollama.Message(role="assistant", content="", tool_calls=[
        ollama.Message.ToolCall(function=ollama.Message.ToolCall.Function(name="search", arguments={"q": "grid"})),
        ollama.Message.ToolCall(function=ollama.Message.ToolCall.Function(name="fetch", arguments={"url": "u"}))])
    return [{"role": "system", "content": "be brief"}, {"role": "user", "content": "Begin."}, call,
            {"role": "tool", "tool_name": "search", "content": "results"}]   # fetch never answered

def test_openai_shape_gives_tool_calls_ids_and_answers_every_one():
    out = providers.openai_messages(_transcript())
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "tool"]
    ids = [c["id"] for c in out[2]["tool_calls"]]
    assert out[3] == {"role": "tool", "tool_call_id": ids[0], "content": "results"}
    assert out[4]["tool_call_id"] == ids[1] and out[4]["content"] == "No result."
    assert json.loads(out[2]["tool_calls"][0]["function"]["arguments"]) == {"q": "grid"}

def test_anthropic_shape_pairs_tool_use_with_results():
    tools = [{"type": "function", "function": {"name": "search", "description": "d", "parameters": {"type": "object"}}}]
    body = providers.anthropic_body("m", _transcript(), tools, None, 100)
    assert body["system"] == "be brief" and "temperature" not in body
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    uses = [b["id"] for b in body["messages"][1]["content"] if b["type"] == "tool_use"]
    results = body["messages"][2]["content"]
    assert [r["tool_use_id"] for r in results] == uses and results[1]["content"] == "No result."
    assert body["tools"] == [{"name": "search", "description": "d", "input_schema": {"type": "object"}}]

# ---------- calls ----------

def test_a_cloud_tool_turn_comes_back_as_an_ollama_message(monkeypatch):
    settings.save_provider("anthropic", "sk-ant-test")
    seen = serve(monkeypatch, lambda r: httpx.Response(200, json={
        "stop_reason": "tool_use", "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "Looking."},
                                               {"type": "tool_use", "id": "t1", "name": "search", "input": {"q": "x"}}]}))
    msg = asyncio.run(llm.chat([{"role": "user", "content": "hi"}], tools=[], model="anthropic/claude-sonnet-test"))
    assert msg.content == "Looking." and msg.tool_calls[0].function.name == "search"
    assert msg.tool_calls[0].function.arguments == {"q": "x"}
    request = seen[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages" and request.headers["x-api-key"] == "sk-ant-test"
    assert json.loads(request.content)["model"] == "claude-sonnet-test"

def test_a_structured_reply_is_streamed_and_unfenced(monkeypatch):
    settings.save_provider("anthropic", "sk-ant-test")
    seen = serve(monkeypatch, lambda r: sse(
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": '```json\n{"a": '}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "1}\n```"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}))
    msg = asyncio.run(llm.chat([{"role": "user", "content": "hi"}], format={"type": "object"},
                               model="anthropic/claude-sonnet-test"))
    assert msg.content == '{"a": 1}'
    body = json.loads(seen[0].content)
    assert body["stream"] is True and body["output_config"]["format"]["schema"] == {"type": "object"}

def test_a_cloud_reply_cut_at_its_cap_overruns_with_what_it_wrote(monkeypatch):
    settings.save_provider("anthropic", "sk-ant-test")
    serve(monkeypatch, lambda r: sse(
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": '{"a": 1'}},
        {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}}))
    with pytest.raises(llm.Overrun) as e:
        asyncio.run(llm.chat([{"role": "user", "content": "hi"}], format={"type": "object"},
                             model="anthropic/claude-sonnet-test"))
    assert e.value.text == '{"a": 1'

def test_an_openai_compatible_provider_falls_back_to_json_mode(monkeypatch):
    settings.save_provider("xai", "xai-test")
    def handler(r):
        body = json.loads(r.content)
        if body["response_format"]["type"] == "json_schema":
            return httpx.Response(400, json={"error": {"message": "schema not supported"}})
        return sse({"choices": [{"delta": {"content": '{"ok": true}'}}]},
                   {"choices": [{"delta": {}, "finish_reason": "stop"}]})
    seen = serve(monkeypatch, handler)
    msg = asyncio.run(llm.chat([{"role": "user", "content": "hi"}], format={"type": "object"}, model="xai/grok-test"))
    assert msg.content == '{"ok": true}' and len(seen) == 2
    retry = json.loads(seen[1].content)
    assert retry["messages"][0]["role"] == "system" and "JSON schema" in retry["messages"][0]["content"]
    assert seen[0].headers["authorization"] == "Bearer xai-test" and str(seen[0].url) == "https://api.x.ai/v1/chat/completions"

def test_openai_reasoning_models_get_no_temperature(monkeypatch):
    settings.save_provider("openai", "sk-test")
    seen = serve(monkeypatch, lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}))
    asyncio.run(llm.chat([{"role": "user", "content": "hi"}], model="openai/gpt-test"))
    body = json.loads(seen[0].content)
    assert "temperature" not in body and body["max_completion_tokens"] == llm.CLOUD_REPLY_TOKENS

def test_a_refused_key_is_reported_without_the_key(monkeypatch):
    settings.save_provider("anthropic", "sk-ant-secret")
    serve(monkeypatch, lambda r: httpx.Response(401, json={"error": {"message": "invalid x-api-key"}}))
    with pytest.raises(providers.ProviderError) as e:
        providers.list_models("anthropic")
    assert "invalid x-api-key" in str(e.value) and "sk-ant-secret" not in str(e.value)

def test_cloud_models_list_only_chat_models(monkeypatch):
    settings.save_provider("openai", "sk-test")
    serve(monkeypatch, lambda r: httpx.Response(200, json=OPENAI_MODELS))
    assert providers.list_models("openai") == ["gpt-test"]

# ---------- readiness ----------

def test_a_cloud_only_loop_never_asks_ollama(monkeypatch):
    def no_ollama(*a, **k):
        raise AssertionError("asked Ollama")
    monkeypatch.setattr(ollama, "Client", no_ollama)
    monkeypatch.setenv("MISSION_LOOP_MODEL", "anthropic/claude-sonnet-test")
    monkeypatch.setenv("MISSION_LOOP_SEARCH_MODEL", "anthropic/claude-haiku-test")
    ok, why = llm.ready()
    assert not ok and "Anthropic isn't connected" in why and "ANTHROPIC_API_KEY" in why
    settings.save_provider("anthropic", "sk-ant-test")
    serve(monkeypatch, lambda r: httpx.Response(200, json=ANTHROPIC_MODELS))
    ok, why = llm.ready()
    assert ok and "anthropic/claude-sonnet-test (Principal Investigator)" in why

def test_cloud_contexts_are_bigger_but_bounded():
    assert llm.context_for("qwen3:30b") == llm.CONTEXT
    assert llm.context_for("anthropic/claude-x") == llm.CLOUD_CONTEXT
    big = [{"role": "system", "content": "s"}, {"role": "tool", "content": "x" * 150_000}]
    assert llm.fit(big, llm.TURN_TOKENS)[1]["content"].endswith("[cut to fit]")          # Ollama's 32k
    assert llm.fit(big, llm.TURN_TOKENS, "anthropic/claude-x")[1]["content"] == "x" * 150_000

# ---------- the Settings page ----------

def test_connecting_a_provider_checks_the_key_and_lists_its_models(client, monkeypatch):
    html = client.get("/settings").text
    for p in ("anthropic", "openai", "xai", "deepseek", "gemini", "openrouter", "custom"):
        assert f'name="provider" value="{p}"' in html
    serve(monkeypatch, lambda r: httpx.Response(401, json={"error": {"message": "invalid x-api-key"}}))
    r = client.post("/settings/provider", data={"do": "connect", "provider": "anthropic", "key": "bad"})
    assert "Anthropic was not connected" in r.text and "invalid x-api-key" in r.text
    assert not settings.provider_on("anthropic")
    serve(monkeypatch, lambda r: httpx.Response(200, json=ANTHROPIC_MODELS))
    r = client.post("/settings/provider", data={"do": "connect", "provider": "anthropic", "key": "sk-ant-good"},
                    follow_redirects=False)
    assert "provider_on" in r.headers["location"] and settings.provider_key("anthropic") == "sk-ant-good"
    html = client.get("/settings").text
    assert '<optgroup label="Anthropic">' in html and 'value="anthropic/claude-sonnet-test"' in html
    assert "sk-ant-good" not in html                               # a key is never shown again
    r = client.post("/settings/worker", data={"worker": "pi", "model": "anthropic/claude-sonnet-test", "on": "on"},
                    follow_redirects=False)
    assert "worker_saved" in r.headers["location"] and settings.worker_model("pi") == "anthropic/claude-sonnet-test"
    assert settings.worker_model("assistant") == "small-test:1b"     # the other worker stays on Ollama
    r = client.post("/settings/worker", data={"worker": "pi", "model": "anthropic/not-offered", "on": "on"},
                    follow_redirects=False)
    assert "error=worker_model" in r.headers["location"]
    r = client.post("/settings/provider", data={"do": "forget", "provider": "anthropic"}, follow_redirects=False)
    assert "provider_off" in r.headers["location"] and not settings.provider_on("anthropic")
    assert client.post("/settings/provider", data={"do": "forget", "provider": "openai"},
                       headers={"origin": "https://evil.example"}).status_code == 403

def test_connect_ollama_gives_each_worker_without_a_model_one(client, monkeypatch):
    monkeypatch.delenv("MISSION_LOOP_MODEL"); monkeypatch.delenv("MISSION_LOOP_SEARCH_MODEL")
    assert "Connect Ollama" in client.get("/settings").text
    r = client.post("/settings/ollama-connect", follow_redirects=False)
    assert "ollama_connected" in r.headers["location"]
    assert settings.worker_model("pi") == "big-test:30b" and settings.worker_model("assistant") == "small-test:1b"
    r = client.post("/settings/ollama-connect", follow_redirects=False)
    assert "ollama_ready" in r.headers["location"]                 # nothing left to fill: a choice is never overwritten
    closed = {"host": "http://127.0.0.1:11434", "ok": False, "error": "ConnectError", "models": [], "loaded": {}}
    monkeypatch.setattr(llm, "connection", lambda timeout=2: closed)
    assert "error=ollama" in client.post("/settings/ollama-connect", follow_redirects=False).headers["location"]

def test_researcher_chat_offers_cloud_models_with_ollama_closed(monkeypatch):
    class Down:
        def __init__(self, *a, **k): pass
        def list(self): raise ConnectionError("no ollama")
    monkeypatch.setattr(ollama, "Client", Down)
    with pytest.raises(ConnectionError):
        assistant.catalog()                                        # nothing at all to talk to: say so
    settings.save_provider("anthropic", "sk-ant-test")
    serve(monkeypatch, lambda r: httpx.Response(200, json=ANTHROPIC_MODELS))
    found = {c["name"]: c for c in assistant.catalog()}
    assert set(found) == {"anthropic/claude-haiku-test", "anthropic/claude-sonnet-test"}
    assert found["anthropic/claude-sonnet-test"]["family"] == "claude"
