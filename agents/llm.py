"""Thin wrapper over the Ollama client so tests can replace one function."""
import os
import ollama

MODEL = os.environ.get("MISSION_LOOP_MODEL", "qwen3.8:27b")
SEARCH_MODEL = os.environ.get("MISSION_LOOP_SEARCH_MODEL", "bonsai-2:4b")   # research tool loops only
OPTIONS = {"num_ctx": 32768, "temperature": 0.2}

async def chat(messages, tools=None, format=None, model=None) -> ollama.Message:
    client = ollama.AsyncClient()          # new client per call: safe across asyncio.run() loops
    resp = await client.chat(model=model or MODEL, messages=messages, tools=tools,
                             format=format, options=OPTIONS, keep_alive="24h")
    return resp.message

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
