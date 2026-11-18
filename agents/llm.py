"""Thin wrapper over the Ollama client so tests can replace one function."""
import os
import ollama

MODEL = os.environ.get("MISSION_LOOP_MODEL", "qwen3.8:27b")
OPTIONS = {"num_ctx": 32768, "temperature": 0.2}

async def chat(messages, tools=None, format=None) -> ollama.Message:
    client = ollama.AsyncClient()          # new client per call: safe across asyncio.run() loops
    resp = await client.chat(model=MODEL, messages=messages, tools=tools,
                             format=format, options=OPTIONS, keep_alive="24h")
    return resp.message

def ready() -> tuple[bool, str]:
    """Is Ollama up with the model pulled? Checked before each pass so a stopped Ollama app
    doesn't burn every queued mission's attempts and park them all."""
    try:
        names = {m.model for m in ollama.list().models}
    except Exception as e:
        return False, f"Ollama is not reachable ({type(e).__name__}); start the Ollama app"
    if MODEL not in names:
        return False, f"model {MODEL} is not pulled; run: ollama pull {MODEL}"
    return True, MODEL
