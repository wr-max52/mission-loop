"""Researcher Chat: answers a human's questions about what the loop is doing, and looks things up on the web.
It is not one of the workers.

Small on purpose: a short prompt, a few tools, a handful of tool calls, short tool results and only the last couple
of turns the page sends back. It keeps no memory and saves nothing.

Every tool comes from MCP servers in mcp.json: "loop" (status_tools.py), "library" (library_tools.py: search_library,
the desk's own work by meaning and words, the same library the Theorist searches), and the researcher's own web
tools, "fetch" and "pages" (page_tools.py: find_in_page, page_links, wayback_versions), plus "search" (Brave web and
news search) when a Brave key is set. It can do everything the Theorist can: read the desk as the Theorist does
(desk_overview), search the library, change the inspiration line, propose epics and combines (they wait for the
human), link missions, queue a mission and ask for the Theorist's turn, through the Theorist's own checks. Nothing
here can change an existing mission's work: add_mission only queues a new one and set_mission_epic only moves a
mission between epics. By default it runs on the Research
Assistant's model with the loop's options (settings.chat_model; config, never a name in code), so Ollama uses
the copy already loaded instead of reloading the model under a running agent. The human may pick another
installed model; that one is loaded beside it.

It never needs the loop: the loop tools read the files the loop leaves behind, so it answers whether the loop
is running, stopped or was never started. It needs only a model: Ollama's, or a connected cloud provider's (and the
network for the web tools).
"""
import asyncio, os, re
from pathlib import Path
import ollama
import settings
from agents import llm, providers
from agents.toolbox import Toolbox, server_status

def default_model() -> str:
    """The configured chat model; on a fresh clone with no model chosen anywhere yet, the first installed model that
    can call tools (else a connected cloud provider's first), so the chat can help with setup before the workers
    have models."""
    return settings.chat_model() or _first_usable()

def _first_usable() -> str:
    try:
        for m in sorted(_ollama().list().models, key=lambda m: m.model):
            if _can_use_tools(m.model):
                return m.model
    except Exception:
        pass
    for pid in settings.cloud_providers():
        names = providers.models(pid)[0]
        if names:
            return settings.model_ref(pid, names[0])
    return ""

def _ollama() -> ollama.Client:
    return ollama.Client(host=settings.ollama_host(), timeout=10)

CONFIG = Path(__file__).resolve().parent.parent / "mcp.json"
MAX_TOOL_CALLS = 8                         # a web question takes a search, a page and a look inside it
TIME_LIMIT = 180                           # seconds for the whole answer
MAX_TOOL_CHARS = 2500                      # the context stays small even if a tool says a lot
MAX_WEB_CHARS = 4000                       # a web page or search gets a little more: its start is mostly menus
MAX_DESK_CHARS = 6000                      # the Theorist's view of the whole desk
LOOP_SERVER, LIBRARY_SERVER, WEB_SERVERS = "loop", "library", ["fetch", "pages"]
ROOMY = {"desk_overview": MAX_DESK_CHARS}  # loop tools that may say more than MAX_TOOL_CHARS
BIG_MODEL = 20                             # billions of parameters: from here a model is a tenured researcher
MAX_TURNS = 4                              # earlier messages the page may send back for follow-ups
MAX_MESSAGE_CHARS = 600

SYSTEM = """You are Researcher Chat, the research desk's assistant in the Mission Loop app. You answer the human's
questions about the loop and the desk's findings, look things up on the web, and can do whatever the Theorist does.
A mission is a question to research: queued -> researching (the Research Assistant, a small model, reads sources;
the Principal Investigator, a big model, writes a brief; a validator checks it) -> brief ready (the human approves)
-> writing (a check) -> article ready (the human approves) -> published on the local site and channels. A mission
failing every attempt is parked; the human can requeue it or push its draft through. Claims are facts (what the
record shows) or possibilities (labelled, not verified). The mission finder adds missions from the human's inspiration line; the Theorist
looks across the desk, evolves the line, proposes epics (themes), missions and combines, and links missions.
Loop tools: loop_status, list_missions, mission_status, agent_steps, search_missions, publication_status,
list_epics, theorist_notes, desk_overview, setup_status (setup questions, or when
nothing happens). search_library finds what the desk's briefs and articles say (claims,
quotes, sections). Web tools: search, fetch, find_in_page, wayback_versions. Link every web page you rely on as
[title](url); never invent a URL.
Change things only when asked: add_mission (search_missions first), set_mission_epic, set_inspiration,
propose_epic, propose_combine, link_missions, ask_theorist. Proposals and new briefs wait for the human.
Approve, publish, start and stop are UI buttons, not your tools.
Answer briefly; Markdown renders. Use topics, not slugs; say when you don't know."""

OUT_OF_TIME = "I ran out of time looking that up. Try a narrower question, like one mission by name."
NO_MODEL = ("I need a model to answer. Open Ollama with a model installed (ollama.com/download), or add a cloud "
            "provider's API key on the Settings page, then ask again.")

def _can_use_tools(name: str) -> bool:
    if settings.split_model(name)[0] != settings.LOCAL:
        return True                        # every connected cloud provider's chat models call tools
    try:
        return "tools" in (_ollama().show(name).capabilities or [])
    except Exception:
        return False

def _billions(text: str | None) -> float | None:
    """'30.5B', 'qwen3:30b', 'phi3:3.8b' -> billions of parameters; None when it doesn't say."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*([bm])\b", (text or "").lower())
    return None if not m else float(m.group(1)) / (1000 if m.group(2) == "m" else 1)

def crew(name: str, size: str | None = None) -> str:
    """Which crew of pixel researchers stands for a model: a worker's own model is that worker's ("assistant",
    young; "pi", tenured), any other is tenured from BIG_MODEL billion parameters up, else young."""
    for worker in settings.WORKERS:
        if name == settings.worker_model(worker):
            return worker
    billions = _billions(size) or _billions(name.rsplit(":", 1)[-1])
    return "pi" if billions and billions >= BIG_MODEL else "assistant"

def catalog() -> list[dict]:
    """Models that can call tools (the assistant can't work without them), default first, each with its family and
    crew so the chat panel can send in the right researcher: Ollama's installed ones, then each connected cloud
    provider's. Raises if Ollama is unreachable and no cloud provider is connected."""
    default = default_model()
    found = []
    try:
        for m in sorted(_ollama().list().models, key=lambda m: m.model):
            if m.model != default and not _can_use_tools(m.model):
                continue
            details = getattr(m, "details", None)
            size, family = getattr(details, "parameter_size", None), getattr(details, "family", None)
            found.append({"name": m.model, "family": family or m.model.split(":")[0], "crew": crew(m.model, size)})
    except Exception:
        if not settings.cloud_providers():
            raise
    for pid in settings.cloud_providers():
        for name in providers.models(pid)[0]:
            ref = settings.model_ref(pid, name)
            found.append({"name": ref, "family": providers.family(name), "crew": crew(ref)})
    return sorted(found, key=lambda c: c["name"] != default)

def models() -> list[str]:
    """Installed models the assistant can use, default first. Raises if Ollama is unreachable."""
    return [c["name"] for c in catalog()]

def pick(model: str | None) -> str:
    """The human's choice if it is an installed chat model, else the default."""
    default = default_model()
    if not model or model == default:
        return default
    try:
        return model if model in models() else default
    except Exception:
        return default

def web_search() -> bool:
    """Brave's search needs a key (saved on the Settings page or in .env) and npx; reading pages works without."""
    return not server_status(["search"], CONFIG)["search"]

def servers() -> list[str]:
    return [LOOP_SERVER, LIBRARY_SERVER, *WEB_SERVERS, *(["search"] if web_search() else [])]

def _limit(tb, name: str) -> int:
    """Loop answers stay short; a library search, web page or search result gets a little more room, and the
    whole desk more still."""
    if name in ROOMY:
        return ROOMY[name]
    server = getattr(tb, "server", {}).get(name)
    return MAX_WEB_CHARS if server and server != LOOP_SERVER else MAX_TOOL_CHARS

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
    if not model:
        return NO_MODEL
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
                    messages.append({"role": "tool", "tool_name": tc.function.name,
                                     "content": result[:_limit(tb, tc.function.name)]})
            messages.append({"role": "user", "content": "Answer now from what you found, without tools."})
            msg = await llm.chat(messages, model=model, stoppable=False)
            return (msg.content or "").strip() or OUT_OF_TIME
    except TimeoutError:
        return OUT_OF_TIME

def chat(question: str, history: list[dict] | None = None, model: str | None = None) -> str:
    """Sync entry point for the UI and CLI. An unknown model falls back to the default."""
    return asyncio.run(run_chat(question, history, pick(model)))
