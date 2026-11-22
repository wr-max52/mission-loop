"""Chat assistant: answers a human's questions about what the loop is doing. It is not one of the workers.

Small on purpose: a short prompt, a few tools (all read-only but add_mission and set_mission_epic), a handful of tool calls, short tool results
and only the last couple of turns the page sends back. It keeps no memory and saves nothing.

Every tool comes from MCP servers in mcp.json ("loop" = status_tools.py, plus "search" when a Brave key is set),
so nothing here can change an existing mission's work: add_mission only queues a new one and set_mission_epic
only moves a mission between epics. By default it runs on the Research Assistant's model with the loop's
options (settings.chat_model; config, never a name in code), so Ollama uses the copy already loaded instead of
reloading the model under a running agent. The human may pick another
installed model; that one is loaded beside it.

It never needs the loop: the tools read the files the loop leaves behind, so it answers whether the loop is
running, stopped or was never started. It needs only Ollama.
"""
import asyncio, os
from pathlib import Path
import ollama
import settings
from agents import llm
from agents.toolbox import Toolbox

def default_model() -> str:
    return settings.chat_model()

def _ollama() -> ollama.Client:
    return ollama.Client(host=settings.ollama_host(), timeout=10)

CONFIG = Path(__file__).resolve().parent.parent / "mcp.json"
MAX_TOOL_CALLS = 5
TIME_LIMIT = 120                           # seconds for the whole answer
MAX_TOOL_CHARS = 2500                      # the context stays small even if a tool says a lot
MAX_TURNS = 4                              # earlier messages the page may send back for follow-ups
MAX_MESSAGE_CHARS = 600

SYSTEM = """You are the research desk's assistant, in the Mission Loop app. You answer the human's questions
about the loop and can queue new missions for it. How it works: a mission is a question or claim to research.
It goes queued -> researching (the Research Assistant, a small model, searches and reads sources; the Principal
Investigator, a big model, writes a brief; a validator checks it, 3 attempts) -> brief ready (the human approves
or sends it back) -> writing (the PI writes the article; a check runs, 3 attempts) -> article ready (the human
approves) -> published on the local site and its feeds, then posted to 1F916 (one a day) and Bluesky when they
are set up. A mission that fails all attempts is parked; the human can requeue it or push its draft through.
The mission finder adds up to 3 missions a pass from the human's inspiration line. Missions sit in epics,
short themes. Claims are facts (what the record shows) or possibilities (labelled, not verified). The human
works in the UI: approve, send back, publish, requeue and settings are buttons there, not your tools.
Use your tools before you answer: loop_status, list_missions (open ones by default), mission_status,
agent_steps, search_missions, publication_status, list_epics (all epics, or the missions in one). Call
add_mission only when the human asks for a new mission; search first so you don't add a duplicate, then say
its brief still needs their approval. Call set_mission_epic only when the human asks to move a mission
(any stage) to an epic; prefer an existing epic. You cannot approve, publish, start or stop anything; say so
if asked. Answer briefly; Markdown (**bold**, lists) renders. Use topics, not slugs; say when you don't know."""

OUT_OF_TIME = "I ran out of time looking that up. Try a narrower question, like one mission by name."

def _can_use_tools(name: str) -> bool:
    try:
        return "tools" in (_ollama().show(name).capabilities or [])
    except Exception:
        return False

def models() -> list[str]:
    """Installed Ollama models that can call tools (the assistant can't work without them), default first.
    Raises if Ollama is unreachable."""
    default, names = default_model(), sorted(m.model for m in _ollama().list().models)
    return sorted([n for n in names if n == default or _can_use_tools(n)], key=lambda n: n != default)

def pick(model: str | None) -> str:
    """The human's choice if it is an installed chat model, else the default."""
    default = default_model()
    if not model or model == default:
        return default
    try:
        return model if model in models() else default
    except Exception:
        return default

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
    model = model or default_model()
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
