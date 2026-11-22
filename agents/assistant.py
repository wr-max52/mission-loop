"""Chat assistant: answers a human's questions about what the loop is doing. It is not one of the workers.

Small on purpose: a one-paragraph prompt, a few read-only tools, a handful of tool calls, short tool results
and only the last couple of turns the page sends back. It keeps no memory and saves nothing.

Every tool comes from MCP servers in mcp.json ("loop" = status_tools.py, plus "search" when a Brave key is set),
so nothing here can change a mission. It runs on the loop's small model with the loop's options, so Ollama
uses the copy already loaded instead of reloading the model under a running agent. The human may pick another
installed model; that one is loaded beside it.

It never needs the loop: the tools read the files the loop leaves behind, so it answers whether the loop is
running, stopped or was never started. It needs only Ollama.
"""
import asyncio, os
from pathlib import Path
import ollama
from agents import llm
from agents.toolbox import Toolbox

MODEL = os.environ.get("MISSION_LOOP_CHAT_MODEL", llm.SEARCH_MODEL)
CONFIG = Path(__file__).resolve().parent.parent / "mcp.json"
MAX_TOOL_CALLS = 5
TIME_LIMIT = 120                           # seconds for the whole answer
MAX_TOOL_CHARS = 2500                      # the context stays small even if a tool says a lot
MAX_TURNS = 4                              # earlier messages the page may send back for follow-ups
MAX_MESSAGE_CHARS = 600

SYSTEM = """You are the research desk's assistant. The real agents (mission finder, researchers, writers,
checkers, publisher) do the work; you only answer the human's questions about it. Use your tools to look
before you answer: what is running, where a mission is, what its agent did last and why it may be slow.
Your tools only read, so you cannot change, start or stop anything; say so if asked. Answer in a few
plain sentences, use mission topics rather than slugs, and say when you don't know."""

OUT_OF_TIME = "I ran out of time looking that up. Try a narrower question, like one mission by name."

def _can_use_tools(name: str) -> bool:
    try:
        return "tools" in (ollama.show(name).capabilities or [])
    except Exception:
        return False

def models() -> list[str]:
    """Installed Ollama models that can call tools (the assistant can't work without them), default first.
    Raises if Ollama is unreachable."""
    names = sorted(m.model for m in ollama.list().models)
    return sorted([n for n in names if n == MODEL or _can_use_tools(n)], key=lambda n: n != MODEL)

def pick(model: str | None) -> str:
    """The human's choice if it is an installed chat model, else the default."""
    if not model or model == MODEL:
        return MODEL
    try:
        return model if model in models() else MODEL
    except Exception:
        return MODEL

def servers() -> list[str]:
    return ["loop", "search"] if os.environ.get("BRAVE_API_KEY") else ["loop"]

def _history(history: list[dict] | None) -> list[dict]:
    turns = [h for h in history or [] if h.get("role") in ("user", "assistant") and str(h.get("content", "")).strip()]
    return [{"role": h["role"], "content": str(h["content"])[:MAX_MESSAGE_CHARS]} for h in turns[-MAX_TURNS:]]

async def run_chat(question: str, history: list[dict] | None = None, model: str | None = None,
                   toolbox_factory=None) -> str:
    question = " ".join(question.split())[:MAX_MESSAGE_CHARS]
    if not question:
        return "Ask me what the loop is doing, or where a mission is."
    messages = [{"role": "system", "content": SYSTEM}, *_history(history), {"role": "user", "content": question}]
    model = model or MODEL
    factory = toolbox_factory or (lambda names: Toolbox(names, config_path=CONFIG))
    try:
        async with asyncio.timeout(TIME_LIMIT), factory(servers()) as tb:
            calls = 0
            while calls < MAX_TOOL_CALLS:
                msg = await llm.chat(messages, tools=tb.tools, model=model, stoppable=False)
                messages.append(msg)
                if not msg.tool_calls:
                    return (msg.content or "").strip() or "I couldn't put an answer together. Try asking again."
                for tc in msg.tool_calls[:MAX_TOOL_CALLS - calls]:
                    calls += 1
                    result = await tb.call(tc.function.name, dict(tc.function.arguments or {}))
                    messages.append({"role": "tool", "tool_name": tc.function.name, "content": result[:MAX_TOOL_CHARS]})
            messages.append({"role": "user", "content": "Answer now from what you found, without tools."})
            msg = await llm.chat(messages, model=model, stoppable=False)
            return (msg.content or "").strip() or OUT_OF_TIME
    except TimeoutError:
        return OUT_OF_TIME

def chat(question: str, history: list[dict] | None = None, model: str | None = None) -> str:
    """Sync entry point for the UI and CLI. An unknown model falls back to the default."""
    return asyncio.run(run_chat(question, history, pick(model)))
