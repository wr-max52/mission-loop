# Mission Loop: Milestone 2 (Research Agent + Source Validator)

Milestone 1 proved the loop with stubs. Milestone 2 replaces two stubs with real implementations:

- **Research agent**: a bounded tool loop on Ollama (`qwen3.8:27b`) that uses MCP tools (search, fetch) and returns a `ResearchBrief`.
- **Source validator**: plain Python that fetches every cited page and checks quotes, domains, a contradicting source, and POC emails. The model never decides whether its own sources are valid.

It also adds three things milestone 1 was missing: a **switchboard** so the orchestrator never changes when agents are swapped, a way to **reject** a brief or **requeue** a parked mission, and a readable **brief.md** for the approval gate.

Read this whole file before writing code. The reference implementation below was run against mcp 2.3.0 and ollama-python 0.6.3: all 23 tests pass, including a real MCP round trip against a local test server. The live Ollama and live web paths could not be tested in the authoring environment; section 8 is how you test them.

---

## 1. Rules (milestone 1 rules still apply)

1. **The orchestrator stays deterministic.** No LLM decides what runs next.
2. **`agents/__init__.py` is the only switchboard.** The orchestrator does `import agents` and calls `agents.research`, `agents.validate`, `agents.interview`, `agents.check_inbox`, `agents.write`. Swapping implementations means editing `agents/__init__.py` only. `MISSION_LOOP_STUBS=1` routes everything to stubs.
3. **Validation is code.** `checks/validator.py` must never call the LLM.
4. **Research is bounded.** Max 40 tool calls per attempt, one JSON repair attempt, max 3 attempts per mission before PARKED.
5. **A crash is a failed attempt, not a dead loop.** If research or validation raises (Ollama down, MCP server missing, network error), the orchestrator records the error as validator notes and counts the attempt.
6. **Tests never touch Ollama or the internet.** LLM calls, MCP toolboxes and web fetches are replaced with fakes in tests. The only subprocess allowed in tests is the local echo MCP server.
7. **Pin `mcp>=2,<3`.** mcp 2.x renamed APIs: `Tool.input_schema` (not `inputSchema`), `CallToolResult.is_error` (not `isError`), and `FastMCP` became `MCPServer` in `mcp.server.mcpserver`. Do not write 1.x code.
8. **Name modules so they don't collide with switchboard exports.** The research module is `agents/researcher.py` because `agents.research` is the exported function.

## 2. Prerequisites (on the Mac)

```bash
brew install uv node                 # uvx runs the fetch server; npx runs the search server
ollama list | grep qwen3.8:27b     # already downloaded; confirm the exact tag
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                 # then paste your search API key into .env
```

### `requirements.txt`

```text
pydantic>=2.7
pyyaml
typer
python-dotenv
ollama>=0.6
mcp>=2,<3
httpx
trafilatura
rapidfuzz
pytest
pytest-asyncio
```

### `.env.example`

```bash
# Search API key for the MCP search server in mcp.json
BRAVE_API_KEY=
# TAVILY_API_KEY=
# Optional: override the model
# MISSION_LOOP_MODEL=qwen3.8:27b
```


Add `.env` to `.gitignore`. Never commit API keys.

## 3. Task order

Do these in order and run `pytest -q` after each group.

1. **Contracts:** add `Claim`; `ResearchBrief.claims` becomes `list[Claim]`. Update `agents/stubs.py` to match.
2. **Switchboard:** rewrite `agents/__init__.py`.
3. **Orchestrator:** `import agents`; wrap research and validate in try/except; write `missions/<slug>/brief.md` on BRIEF_READY; clear notes on success; add `reject()` and `requeue()`.
4. **CLI:** load `.env`; add `reject`, `requeue`, `show`.
5. **Test infrastructure:** move fixtures to `tests/conftest.py`, add the `stub_agents` fixture, update `tests/test_loop.py`, set `asyncio_mode = auto`.
6. **Feedback tests:** `tests/test_feedback.py`. Milestone 1 tests must still pass.
7. **Research stack:** `agents/llm.py`, `agents/toolbox.py`, `agents/researcher.py`, `mcp.json`, plus their tests.
8. **Validator:** `checks/validator.py` and its tests.
9. **Render:** `render.py`.
10. **Live smoke test** (section 8).

## 4. State machine changes

Same states as milestone 1. New transitions:

| From | To | Trigger |
|---|---|---|
| BRIEF_READY | RESEARCHING | `run.py reject <slug> --notes "..."`; attempts reset to 0; notes passed to the next research attempt |
| PARKED | QUEUED | `run.py requeue <slug>`; attempts reset to 0; last notes kept |
| RESEARCHING | RESEARCHING / PARKED | research or validate raised an exception; counts as a failed attempt |

## 5. Files

### 5.1 Changed files

### `contracts.py`

```python
from datetime import datetime
from enum import Enum
from typing import Literal
from pydantic import BaseModel, Field

class State(str, Enum):
    QUEUED = "QUEUED"
    RESEARCHING = "RESEARCHING"
    BRIEF_READY = "BRIEF_READY"            # waits for human approval
    AWAITING_COMMENTS = "AWAITING_COMMENTS"
    WRITING = "WRITING"
    ARTICLE_READY = "ARTICLE_READY"        # waits for human review
    DONE = "DONE"
    PARKED = "PARKED"

class Mission(BaseModel):
    slug: str
    topic: str
    objective: str = ""
    tools: list[str] = ["search", "fetch"]
    state: State = State.QUEUED
    attempts: int = 0
    max_attempts: int = 3
    wait_hours: float = 48                 # fixed; comments never end it early
    wait_until: datetime | None = None
    approved: bool = False
    reviewed: bool = False
    validator_notes: str = ""              # validator failures or human feedback

class Claim(BaseModel):
    text: str
    source_ids: list[str]

class Source(BaseModel):
    id: str
    url: str
    title: str
    quote: str                             # exact sentence copied from the page
    stance: Literal["supports", "contradicts"]

class POC(BaseModel):
    name: str
    role: str
    email: str
    contact_source_url: str                # public page where the email appears
    why_them: str

class ResearchBrief(BaseModel):
    thesis: str
    claims: list[Claim]
    counterpoint: str
    sources: list[Source]
    pocs: list[POC] = Field(min_length=3)  # 3 or more POCs
    question_for_pocs: str

class OutreachRecord(BaseModel):
    poc_email: str
    gmail_thread_id: str
    sent_at: datetime
    replies: list[dict] = []
```

### `agents/__init__.py`

```python
"""Switchboard: the ONLY place that decides which implementation each agent uses.

The orchestrator calls agents.research / validate / interview / check_inbox / write.
Swapping a stub for a real agent means editing this file and nothing else.
Set MISSION_LOOP_STUBS=1 to run the whole loop on stubs (no Ollama, no network).
"""
import os

from agents.stubs import interview, check_inbox, write

if os.environ.get("MISSION_LOOP_STUBS") == "1":
    from agents.stubs import research, validate
else:
    from agents.researcher import research
    from checks.validator import validate

__all__ = ["research", "validate", "interview", "check_inbox", "write"]
```

### `agents/stubs.py`

```python
from pathlib import Path
from contracts import Mission, ResearchBrief, Source, POC, Claim, OutreachRecord

def research(m: Mission) -> ResearchBrief:
    return ResearchBrief(
        thesis=f"Stub thesis about {m.topic}",
        claims=[Claim(text="Stub claim", source_ids=["s1", "s2"])],
        counterpoint="Stub counterpoint answering s4",
        sources=[
            Source(id="s1", url="https://a.example.com", title="A", quote="stub", stance="supports"),
            Source(id="s2", url="https://b.example.com", title="B", quote="stub", stance="supports"),
            Source(id="s3", url="https://c.example.com", title="C", quote="stub", stance="supports"),
            Source(id="s4", url="https://d.example.com", title="D", quote="stub", stance="contradicts"),
        ],
        pocs=[POC(name=f"Person {i}", role="Analyst", email=f"poc{i}@example.com",
                  contact_source_url="https://a.example.com/team", why_them="stub")
              for i in range(1, 4)],
        question_for_pocs="What is your take on this thesis?",
    )

def validate(m: Mission, brief: ResearchBrief) -> tuple[bool, str]:
    if "FAIL" in m.topic:
        return False, "stub: forced failure"
    return True, ""

def interview(m: Mission, brief: ResearchBrief, now) -> list[OutreachRecord]:
    return [OutreachRecord(poc_email=p.email, gmail_thread_id=f"stub-{m.slug}-{i}", sent_at=now)
            for i, p in enumerate(brief.pocs)]

def check_inbox(m: Mission, records: list[OutreachRecord], now) -> list[OutreachRecord]:
    return records                         # milestone 4: poll Gmail threads here

def write(m: Mission, brief: ResearchBrief, records: list[OutreachRecord]) -> Path:
    comments = [r for rec in records for r in rec.replies if r.get("is_comment")]
    out = Path("missions") / m.slug / "article.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        f"---\ntitle: {brief.thesis}\nsources: {[s.id for s in brief.sources]}\n---\n\n"
        f"# {brief.thesis}\n\n{brief.claims[0].text}\n\nComments received: {len(comments)}\n")
    return out
```

### `orchestrator.py`

```python
from datetime import timedelta
from pathlib import Path
import agents                              # switchboard: see agents/__init__.py
import store
from contracts import Mission, State, ResearchBrief, OutreachRecord
from render import brief_markdown

STOP_FLAG = Path("data/STOP")

def _brief(m): return ResearchBrief(**store.load_artifact(m.slug, "brief"))
def _outreach(m): return [OutreachRecord(**r) for r in store.load_artifact(m.slug, "outreach") or []]

def _write_brief_md(m: Mission, brief: ResearchBrief) -> Path:
    path = Path("missions") / m.slug / "brief.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(brief_markdown(m.slug, brief))
    return path

def step(m: Mission, now) -> str | None:
    """Run at most one task. Return a note if the mission changed, None if blocked."""
    if m.state == State.QUEUED:
        m.state = State.RESEARCHING
        return "started"

    if m.state == State.RESEARCHING:
        m.attempts += 1
        try:
            brief = agents.research(m)
            ok, notes = agents.validate(m, brief)
        except Exception as e:             # model, tool or network failure counts as a failed attempt
            brief, ok, notes = None, False, f"research crashed: {type(e).__name__}: {e}"
        if ok:
            store.save_artifact(m.slug, "brief", brief)
            path = _write_brief_md(m, brief)
            m.validator_notes = ""
            m.state = State.BRIEF_READY
            return f"brief passed on attempt {m.attempts}; review {path}"
        m.validator_notes = notes
        if m.attempts >= m.max_attempts:
            m.state = State.PARKED
            return f"parked after {m.attempts} attempts"
        return f"attempt {m.attempts} failed validation"

    if m.state == State.BRIEF_READY:
        if not m.approved:
            return None                    # blocked on human
        records = agents.interview(m, _brief(m), now)
        store.save_artifact(m.slug, "outreach", records)
        m.wait_until = now + timedelta(hours=m.wait_hours)
        m.state = State.AWAITING_COMMENTS
        return f"asked {len(records)} POCs; waiting until {m.wait_until:%Y-%m-%d %H:%M} UTC"

    if m.state == State.AWAITING_COMMENTS:
        store.save_artifact(m.slug, "outreach", agents.check_inbox(m, _outreach(m), now))
        if now < m.wait_until:
            return None                    # blocked on clock
        m.state = State.WRITING
        return "wait over"

    if m.state == State.WRITING:
        path = agents.write(m, _brief(m), _outreach(m))
        m.state = State.ARTICLE_READY
        return f"article saved to {path}"

    if m.state == State.ARTICLE_READY:
        if not m.reviewed:
            return None                    # blocked on human
        m.state = State.DONE
        return "done"

    return None

def reject(m: Mission, notes: str, now) -> None:
    """Human says no to a brief: back to research with the feedback, fresh attempt budget."""
    if m.state != State.BRIEF_READY:
        raise ValueError(f"{m.slug} is {m.state.value}; only BRIEF_READY can be rejected")
    before = m.state
    m.state, m.attempts, m.approved = State.RESEARCHING, 0, False
    m.validator_notes = f"Human reviewer feedback: {notes}"
    store.save_mission(m)
    store.log_event(m.slug, now, before.value, m.state.value, "rejected by human")

def requeue(m: Mission, now) -> None:
    """Revive a parked mission. Keeps the last validator notes so research can learn from them."""
    if m.state != State.PARKED:
        raise ValueError(f"{m.slug} is {m.state.value}; only PARKED can be requeued")
    before = m.state
    m.state, m.attempts = State.QUEUED, 0
    store.save_mission(m)
    store.log_event(m.slug, now, before.value, m.state.value, "requeued by human")

def tick(now) -> int:
    changed = 0
    for m in store.active_missions():
        if STOP_FLAG.exists():
            break                          # stop between tasks, never mid-task
        before = m.state
        note = step(m, now)
        if note is not None:
            store.save_mission(m)          # saved after every task = resumable
            store.log_event(m.slug, now, before.value, m.state.value, note)
            changed += 1
    return changed

def run_until_idle(now_fn, max_ticks: int = 100) -> int:
    total = 0
    for _ in range(max_ticks):
        if STOP_FLAG.exists():
            break
        n = tick(now_fn())
        total += n
        if n == 0:
            break
    return total
```

### `run.py`

```python
import typer, yaml
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()                              # reads API keys from .env before anything else
import clock, orchestrator, store
from contracts import Mission

app = typer.Typer()

def _set(slug: str, **fields):
    m = store.get_mission(slug)
    if not m:
        raise typer.BadParameter(f"no mission {slug}")
    for k, v in fields.items():
        setattr(m, k, v)
    store.save_mission(m)

@app.command()
def init(path: str = "missions.yaml"):
    store.init()
    for item in yaml.safe_load(Path(path).read_text()) or []:
        if not store.get_mission(item["slug"]):
            store.save_mission(Mission(**item))
            print(f"added {item['slug']}")

@app.command()
def add(slug: str, topic: str, objective: str = ""):
    store.init()
    store.save_mission(Mission(slug=slug, topic=topic, objective=objective))

@app.command()
def run():
    orchestrator.STOP_FLAG.unlink(missing_ok=True)
    n = orchestrator.run_until_idle(clock.now)
    print(f"{n} task(s) ran")
    status()

@app.command()
def stop():
    orchestrator.STOP_FLAG.parent.mkdir(exist_ok=True)
    orchestrator.STOP_FLAG.touch()
    print("stop requested; the current task will finish first")

@app.command()
def approve(slug: str):
    _set(slug, approved=True)

@app.command()
def review(slug: str):
    _set(slug, reviewed=True)

@app.command()
def reject(slug: str, notes: str = typer.Option(..., help="What the research agent must fix")):
    orchestrator.reject(store.get_mission(slug), notes, clock.now())
    print(f"{slug} sent back to research")

@app.command()
def requeue(slug: str):
    orchestrator.requeue(store.get_mission(slug), clock.now())
    print(f"{slug} requeued")

@app.command()
def show(slug: str):
    m = store.get_mission(slug)
    brief = Path("missions") / slug / "brief.md"
    print(brief.read_text() if brief.exists() else f"no brief yet ({m.state.value})")
    if m.validator_notes:
        print("\nLast validator / reviewer notes:\n" + m.validator_notes)

@app.command()
def status():
    for m in store.all_missions():
        extra = f" (wait until {m.wait_until:%m-%d %H:%M})" if m.wait_until and m.state.value == "AWAITING_COMMENTS" else ""
        print(f"{m.slug:20} {m.state.value:18} attempts={m.attempts}{extra}")

if __name__ == "__main__":
    app()
```

### `pytest.ini`

```ini
[pytest]
pythonpath = .
asyncio_mode = auto
```


### 5.2 New files

### `agents/llm.py`

```python
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
```

### `agents/toolbox.py`

```python
"""Starts the MCP servers a mission enables and exposes their tools to the LLM.

Adding a research capability = adding an entry to mcp.json. No code change.
"""
import json, os, re
from contextlib import AsyncExitStack
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CONFIG = Path("mcp.json")
MAX_TOOL_CHARS = 12_000                    # keeps one big page from flooding the context

def _expand(value: str) -> str:
    """Replace ${VAR} with the environment variable's value."""
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)

class Toolbox:
    def __init__(self, enabled: list[str], config_path: Path | None = None):
        self.enabled = enabled
        self.config_path = config_path or CONFIG
        self.tools: list[dict] = []        # Ollama tool schemas
        self._owner: dict[str, ClientSession] = {}

    async def __aenter__(self):
        self._stack = AsyncExitStack()
        cfg = json.loads(Path(self.config_path).read_text())
        for name in self.enabled:
            if name not in cfg:
                raise KeyError(f"tool server '{name}' is not defined in {self.config_path}")
            spec = cfg[name]
            env = {**os.environ, **{k: _expand(v) for k, v in spec.get("env", {}).items()}}
            params = StdioServerParameters(command=spec["command"],
                                           args=[_expand(a) for a in spec.get("args", [])], env=env)
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            for tool in (await session.list_tools()).tools:
                self._owner[tool.name] = session
                self.tools.append({"type": "function", "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.input_schema,
                }})
        return self

    async def __aexit__(self, *exc):
        await self._stack.aclose()

    async def call(self, name: str, arguments: dict | None) -> str:
        session = self._owner.get(name)
        if session is None:
            return f"ERROR: unknown tool '{name}'. Available: {sorted(self._owner)}"
        try:
            result = await session.call_tool(name, arguments or {})
        except Exception as e:                # a bad tool call should not kill the mission
            return f"ERROR: {name} failed: {e}"
        text = "\n".join(c.text for c in getattr(result, "content", []) if getattr(c, "type", "") == "text")
        prefix = "ERROR: " if getattr(result, "is_error", False) else ""
        return prefix + text[:MAX_TOOL_CHARS]
```

### `agents/researcher.py`

```python
"""Research agent: a bounded tool loop on Ollama that ends in a ResearchBrief."""
import asyncio
from pydantic import ValidationError
from contracts import Mission, ResearchBrief
from agents import llm
from agents.toolbox import Toolbox

MAX_TOOL_CALLS = 40

SYSTEM = """You are a research analyst.
Topic: {topic}
Mission: {objective}

Find one specific, defensible thesis and prove it with primary sources.
Rules:
- Use the tools to search, then fetch and read full pages. Only cite pages you actually fetched.
- For every source, copy one exact sentence from that page as its quote. Never paraphrase a quote.
- Find at least 3 supporting sources on 3 different websites, and at least 1 credible source that disagrees.
- Find at least 3 points of contact (experts, authors, spokespeople) whose email address appears
  on a public page you fetched. Record that page as contact_source_url.
- When you have enough evidence, stop calling tools and reply with the word DONE.
{notes}"""

FINAL = ("Now output the research brief as JSON matching the schema. "
         "Use only sources you fetched, with quotes copied exactly from those pages.")

class ResearchFailed(Exception):
    pass

def _notes(m: Mission) -> str:
    if not m.validator_notes:
        return ""
    return f"\nYour previous brief was rejected. Fix every problem below:\n{m.validator_notes}\n"

async def run_research(m: Mission, toolbox_factory=Toolbox) -> ResearchBrief:
    messages = [
        {"role": "system", "content": SYSTEM.format(topic=m.topic, objective=m.objective or m.topic,
                                                    notes=_notes(m))},
        {"role": "user", "content": "Begin."},
    ]
    async with toolbox_factory(m.tools) as tb:
        calls = 0
        while calls < MAX_TOOL_CALLS:
            msg = await llm.chat(messages, tools=tb.tools)
            messages.append(msg)
            if not msg.tool_calls:
                break
            for tc in msg.tool_calls:
                if calls >= MAX_TOOL_CALLS:
                    break
                calls += 1
                result = await tb.call(tc.function.name, dict(tc.function.arguments or {}))
                messages.append({"role": "tool", "tool_name": tc.function.name, "content": result})

    messages.append({"role": "user", "content": FINAL})
    schema = ResearchBrief.model_json_schema()
    error = None
    for _ in range(2):                     # one repair attempt for malformed JSON
        msg = await llm.chat(messages, format=schema)
        try:
            return ResearchBrief.model_validate_json(msg.content)
        except ValidationError as e:
            error = e
            messages.append({"role": "assistant", "content": msg.content})
            messages.append({"role": "user", "content": f"That JSON was invalid:\n{e}\nReturn corrected JSON only."})
    raise ResearchFailed(f"model could not produce a valid brief: {error}")

def research(m: Mission) -> ResearchBrief:
    """Sync entry point the orchestrator calls."""
    return asyncio.run(run_research(m))
```

### `checks/validator.py`

```python
"""Source gate. Code decides whether sources are valid; the model never does."""
import re
from urllib.parse import urlparse
import httpx, trafilatura
from rapidfuzz import fuzz
from contracts import Mission, ResearchBrief

MIN_SUPPORT_DOMAINS = 3
QUOTE_MATCH = 90                           # rapidfuzz partial_ratio threshold
MIN_QUOTE_CHARS = 20
MAX_TEXT_CHARS = 300_000
_cache: dict[str, tuple[str, str] | None] = {}

def fetch(url: str) -> tuple[str, str] | None:
    """Return (raw_html, readable_text) or None if the page can't be fetched."""
    if url in _cache:
        return _cache[url]
    page = None
    try:
        r = httpx.get(url, follow_redirects=True, timeout=20,
                      headers={"User-Agent": "Mozilla/5.0 (MissionLoop source validator)"})
        if r.status_code == 200:
            page = (r.text, trafilatura.extract(r.text) or "")
    except httpx.HTTPError:
        page = None
    _cache[url] = page
    return page

def _norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip()

def _domain(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")

def validate(m: Mission, brief: ResearchBrief) -> tuple[bool, str]:
    problems: list[str] = []
    ids = {s.id for s in brief.sources}

    # 1. Every claim cites at least one real source id
    for c in brief.claims:
        if not c.source_ids:
            problems.append(f"Claim cites no sources: '{c.text[:80]}'")
        for sid in c.source_ids:
            if sid not in ids:
                problems.append(f"Claim cites unknown source id '{sid}'")

    # 2 + 3. Every source fetches, and its quote appears on the page
    verified: set[str] = set()
    for s in brief.sources:
        page = fetch(s.url)
        if page is None:
            problems.append(f"{s.id}: could not fetch {s.url}")
            continue
        text = _norm(page[1])[:MAX_TEXT_CHARS]
        quote = _norm(s.quote)
        if not text:
            problems.append(f"{s.id}: no readable text at {s.url}")
        elif len(quote) < MIN_QUOTE_CHARS:
            problems.append(f"{s.id}: quote is too short to verify; copy a full sentence")
        else:
            score = fuzz.partial_ratio(quote, text)
            if score < QUOTE_MATCH:
                problems.append(f"{s.id}: quote not found on {s.url} (best match {score:.0f}%)")
            else:
                verified.add(s.id)

    # 4. At least 3 distinct domains among verified supporting sources
    domains = {_domain(s.url) for s in brief.sources if s.stance == "supports" and s.id in verified}
    if len(domains) < MIN_SUPPORT_DOMAINS:
        problems.append(f"Only {len(domains)} verified supporting domain(s); need {MIN_SUPPORT_DOMAINS}")

    # 5. At least 1 verified contradicting source, and a counterpoint
    if not any(s.stance == "contradicts" and s.id in verified for s in brief.sources):
        problems.append("No verified source contradicts the thesis")
    if not brief.counterpoint.strip():
        problems.append("Counterpoint is empty")

    # 6. Every POC's email appears on their contact page
    for p in brief.pocs:
        page = fetch(p.contact_source_url)
        if page is None or p.email.lower() not in page[0].lower():
            problems.append(f"POC {p.name}: {p.email} not found on {p.contact_source_url}")

    return (not problems, "\n".join(problems))
```


`checks/__init__.py` is an empty file.

### `render.py`

```python
"""Human-readable brief, written next to the data so you can review before approving."""
from contracts import ResearchBrief

def brief_markdown(slug: str, b: ResearchBrief) -> str:
    lines = [f"# Brief: {slug}", "", f"**Thesis:** {b.thesis}", "", "## Claims"]
    lines += [f"- {c.text} [{', '.join(c.source_ids)}]" for c in b.claims]
    lines += ["", f"## Counterpoint", b.counterpoint, "", "## Sources"]
    lines += [f"- **{s.id}** ({s.stance}) [{s.title}]({s.url})\n  > {s.quote}" for s in b.sources]
    lines += ["", "## Points of contact"]
    lines += [f"- {p.name}, {p.role}: {p.email} ([where found]({p.contact_source_url})). {p.why_them}"
              for p in b.pocs]
    lines += ["", f"## Question for POCs", b.question_for_pocs, ""]
    return "\n".join(lines)
```


### 5.3 `mcp.json` (research capabilities)

Each key is a capability name that a mission can list in its `tools` field. `${VAR}` values are read from the environment (loaded from `.env`).

```json
{
  "search": {
    "command": "npx",
    "args": ["-y", "@brave/brave-search-mcp-server"],
    "env": {"BRAVE_API_KEY": "${BRAVE_API_KEY}"}
  },
  "fetch": {
    "command": "uvx",
    "args": ["mcp-server-fetch"]
  }
}
```

**Before relying on this file, verify two things from the official docs, and adjust if needed:**

1. The current official npm package name for the Brave Search MCP server, and that it runs over **stdio** by default (add a transport flag if it defaults to HTTP).
2. If using Tavily instead, replace the `search` entry with the official Tavily MCP server (commonly `npx -y tavily-mcp@latest` with `TAVILY_API_KEY`) and confirm its name the same way.

Report which package and version you used.

## 6. Tests

### `tests/conftest.py`

```python
import pytest
import agents, orchestrator, store
from agents import stubs

@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    """Every test gets its own database, stop flag and working folder."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(store, "DB", tmp_path / "data" / "loop.db")
    monkeypatch.setattr(orchestrator, "STOP_FLAG", tmp_path / "data" / "STOP")
    store.init()

@pytest.fixture
def stub_agents(monkeypatch):
    """Route research and validate to stubs so loop tests never touch Ollama or the network."""
    monkeypatch.setattr(agents, "research", stubs.research)
    monkeypatch.setattr(agents, "validate", stubs.validate)
```


In `tests/test_loop.py`, delete the old `fresh` fixture (it now lives in conftest) and add this line right after the imports so loop tests use stubs:

```python
pytestmark = pytest.mark.usefixtures("stub_agents")
```

### `tests/test_feedback.py`

```python
from datetime import datetime, timezone
import pytest
import orchestrator, store
from contracts import Mission, State

pytestmark = pytest.mark.usefixtures("stub_agents")
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)

def test_reject_sends_brief_back_with_notes():
    store.save_mission(Mission(slug="m", topic="x"))
    orchestrator.run_until_idle(lambda: T0)
    orchestrator.reject(store.get_mission("m"), "thesis too vague", T0)
    m = store.get_mission("m")
    assert m.state == State.RESEARCHING and m.attempts == 0
    assert "thesis too vague" in m.validator_notes

def test_reject_only_from_brief_ready():
    store.save_mission(Mission(slug="m", topic="x"))
    with pytest.raises(ValueError):
        orchestrator.reject(store.get_mission("m"), "no", T0)

def test_requeue_revives_parked_mission():
    store.save_mission(Mission(slug="p", topic="FAIL"))
    orchestrator.run_until_idle(lambda: T0)
    assert store.get_mission("p").state == State.PARKED
    orchestrator.requeue(store.get_mission("p"), T0)
    m = store.get_mission("p")
    assert m.state == State.QUEUED and m.attempts == 0

def test_research_crash_counts_as_failed_attempt(monkeypatch):
    import agents
    def boom(m): raise RuntimeError("ollama is not running")
    monkeypatch.setattr(agents, "research", boom)
    store.save_mission(Mission(slug="c", topic="x"))
    orchestrator.run_until_idle(lambda: T0)
    m = store.get_mission("c")
    assert m.state == State.PARKED and "ollama is not running" in m.validator_notes

def test_brief_md_written_for_review():
    from pathlib import Path
    store.save_mission(Mission(slug="b", topic="x"))
    orchestrator.run_until_idle(lambda: T0)
    assert "**Thesis:**" in Path("missions/b/brief.md").read_text()
```

### `tests/test_validator.py`

```python
import pytest
from checks import validator
from contracts import Mission, ResearchBrief, Source, POC, Claim

M = Mission(slug="v", topic="t")
Q = "Heat pumps kept homes above 20C at minus 25C during the trial."

PAGES = {
    "https://a.com/1": ("<p>x</p>", f"Intro. {Q} More text."),
    "https://b.org/2": ("<p>x</p>", f"Other words. {Q}"),
    "https://c.net/3": ("<p>x</p>", f"{Q} Closing."),
    "https://d.io/4":  ("<p>x</p>", "Critics say efficiency collapses below minus 20C in older homes."),
    "https://a.com/team": ("<a href='mailto:ana@a.com'>Ana</a> bo@b.org cy@c.net", "team"),
}

def brief(**changes) -> ResearchBrief:
    data = dict(
        thesis="Cold climate heat pumps work",
        claims=[Claim(text="They hold temperature", source_ids=["s1", "s2"])],
        counterpoint="Older homes need insulation first",
        sources=[
            Source(id="s1", url="https://a.com/1", title="A", quote=Q, stance="supports"),
            Source(id="s2", url="https://b.org/2", title="B", quote=Q, stance="supports"),
            Source(id="s3", url="https://c.net/3", title="C", quote=Q, stance="supports"),
            Source(id="s4", url="https://d.io/4", title="D",
                   quote="Critics say efficiency collapses below minus 20C in older homes.", stance="contradicts"),
        ],
        pocs=[POC(name=n, role="Engineer", email=e, contact_source_url="https://a.com/team", why_them="x")
              for n, e in [("Ana", "ana@a.com"), ("Bo", "bo@b.org"), ("Cy", "cy@c.net")]],
        question_for_pocs="Does this match what you see?",
    )
    data.update(changes)
    return ResearchBrief(**data)

@pytest.fixture(autouse=True)
def fake_web(monkeypatch):
    monkeypatch.setattr(validator, "fetch", lambda url: PAGES.get(url))

def test_good_brief_passes():
    ok, notes = validator.validate(M, brief())
    assert ok, notes

def test_invented_quote_fails():
    b = brief()
    b.sources[0].quote = "Heat pumps are the best technology ever invented for every home."
    ok, notes = validator.validate(M, b)
    assert not ok and "s1: quote not found" in notes

def test_unfetchable_source_fails():
    b = brief()
    b.sources[1].url = "https://dead.example/404"
    ok, notes = validator.validate(M, b)
    assert not ok and "could not fetch" in notes

def test_too_few_domains_fails():
    b = brief()
    b.sources[2].url = "https://a.com/1"          # now only a.com and b.org support
    ok, notes = validator.validate(M, b)
    assert not ok and "supporting domain" in notes

def test_missing_contradiction_fails():
    b = brief()
    b.sources[3].stance = "supports"
    ok, notes = validator.validate(M, b)
    assert not ok and "contradicts" in notes

def test_unknown_source_id_fails():
    ok, notes = validator.validate(M, brief(claims=[Claim(text="x", source_ids=["s9"])]))
    assert not ok and "unknown source id 's9'" in notes

def test_poc_email_not_on_page_fails():
    b = brief()
    b.pocs[2].email = "nobody@nowhere.com"
    ok, notes = validator.validate(M, b)
    assert not ok and "nobody@nowhere.com not found" in notes
```

### `tests/test_research.py`

```python
import json
import pytest
from ollama import Message
from agents import researcher as research_mod
from agents import llm
from agents.stubs import research as stub_brief
from contracts import Mission

M = Mission(slug="r", topic="Heat pumps", tools=["search"])
BRIEF_JSON = stub_brief(M).model_dump_json()

def tool_call(name, **args):
    return Message(role="assistant", content="", tool_calls=[
        Message.ToolCall(function=Message.ToolCall.Function(name=name, arguments=args))])

def text(content):
    return Message(role="assistant", content=content)

class FakeToolbox:
    calls: list = []
    def __init__(self, enabled): self.tools = [{"type": "function", "function": {"name": "search"}}]
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): pass
    async def call(self, name, args):
        FakeToolbox.calls.append((name, args))
        return "result"

@pytest.fixture(autouse=True)
def reset():
    FakeToolbox.calls = []

def script(monkeypatch, replies):
    """Make llm.chat return the given replies in order; record what it was sent."""
    seen = []
    it = iter(replies)
    async def fake_chat(messages, tools=None, format=None):
        seen.append({"tools": tools, "format": format})
        return next(it)
    monkeypatch.setattr(llm, "chat", fake_chat)
    return seen

async def test_tool_loop_then_brief(monkeypatch):
    seen = script(monkeypatch, [tool_call("search", query="heat pumps"), text("DONE"), text(BRIEF_JSON)])
    brief = await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert brief.thesis.startswith("Stub thesis")
    assert FakeToolbox.calls == [("search", {"query": "heat pumps"})]
    assert seen[-1]["format"] is not None         # final call asks for the JSON schema

async def test_tool_budget_is_enforced(monkeypatch):
    replies = [tool_call("search", query=str(i)) for i in range(100)] 
    seen = script(monkeypatch, replies[:research_mod.MAX_TOOL_CALLS] + [text(BRIEF_JSON)])
    await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert len(FakeToolbox.calls) == research_mod.MAX_TOOL_CALLS

async def test_bad_json_gets_one_repair(monkeypatch):
    script(monkeypatch, [text("DONE"), text("{not json"), text(BRIEF_JSON)])
    brief = await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert len(brief.pocs) == 3

async def test_bad_json_twice_raises(monkeypatch):
    script(monkeypatch, [text("DONE"), text("{nope"), text("{still nope")])
    with pytest.raises(research_mod.ResearchFailed):
        await research_mod.run_research(M, toolbox_factory=FakeToolbox)

async def test_validator_notes_reach_the_prompt(monkeypatch):
    captured = {}
    async def fake_chat(messages, tools=None, format=None):
        captured.setdefault("system", messages[0]["content"])
        return text("DONE") if format is None else text(BRIEF_JSON)
    monkeypatch.setattr(llm, "chat", fake_chat)
    m = M.model_copy(update={"validator_notes": "s1: quote not found"})
    await research_mod.run_research(m, toolbox_factory=FakeToolbox)
    assert "s1: quote not found" in captured["system"]
```

### `tests/fixtures/echo_server.py`

```python
"""Tiny local MCP server used to test the Toolbox plumbing without any network."""
from mcp.server.mcpserver import MCPServer

server = MCPServer("echo")

@server.tool()
def echo(text: str) -> str:
    """Return the text you send."""
    return f"echo: {text}"

if __name__ == "__main__":
    server.run()
```

### `tests/test_toolbox.py`

```python
import json, sys
from pathlib import Path
from agents.toolbox import Toolbox

FIXTURE = Path(__file__).parent / "fixtures" / "echo_server.py"

async def test_toolbox_round_trip(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"echo": {"command": sys.executable, "args": [str(FIXTURE)]}}))
    async with Toolbox(["echo"], config_path=cfg) as tb:
        assert [t["function"]["name"] for t in tb.tools] == ["echo"]
        assert "echo: hi" in await tb.call("echo", {"text": "hi"})
        assert (await tb.call("nope", {})).startswith("ERROR: unknown tool")

async def test_unknown_server_name_is_a_clear_error(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{}")
    try:
        async with Toolbox(["search"], config_path=cfg):
            pass
    except KeyError as e:
        assert "search" in str(e)
    else:
        raise AssertionError("expected KeyError")
```


Expected: `pytest -q` shows **23 passed** (4 loop, 5 feedback, 7 validator, 5 research, 2 toolbox).

## 7. Stub walkthrough (no Ollama, no network)

```bash
rm -rf data missions                                   # fresh start
MISSION_LOOP_STUBS=1 python run.py init
MISSION_LOOP_STUBS=1 python run.py run                 # heat-pumps BRIEF_READY, fail-test PARKED
MISSION_LOOP_STUBS=1 python run.py show heat-pumps     # prints brief.md
MISSION_LOOP_STUBS=1 python run.py reject heat-pumps --notes "too vague"
MISSION_LOOP_STUBS=1 python run.py requeue fail-test
MISSION_LOOP_STUBS=1 python run.py run                 # heat-pumps BRIEF_READY again, fail-test PARKED again
```

## 8. Live smoke test (real Ollama + real web)

1. Confirm Ollama is running: `ollama ps` (start the app if not).
2. Confirm each MCP server starts on its own: run the `command` + `args` from `mcp.json` in a terminal; it should wait silently for input. Ctrl+C to exit.
3. Add one real mission you know well:
   ```bash
   python run.py add pt-cats "Indoor vs outdoor cats" --objective "Find a defensible thesis about whether indoor cats live longer"
   python run.py run
   python run.py show pt-cats
   ```
4. Expect the first runs to **park**. That is the validator doing its job. Read the notes from `show`, then tune `SYSTEM` in `agents/researcher.py` (most common fixes: quotes paraphrased instead of copied, too few domains, POC emails that aren't on the cited page).
5. `python run.py requeue pt-cats` and rerun after each prompt change.

A good result: a brief.md where you click 3 random sources and the quotes are really there.

## 9. Out of scope (do NOT build)

- Gmail, outreach sending, reply classification (milestone 4)
- Streamlit or any UI (milestone 3)
- A real writer (milestone 5)
- LangGraph, CrewAI or any agent framework; the tool loop in `researcher.py` is intentionally hand-written
- Parallel missions, background threads, or daemons
- Caching search results to disk, vector stores, RAG

## 10. Definition of done

- [ ] `pytest -q` shows `23 passed` with no network access needed
- [ ] Stub walkthrough (section 7) produces the expected states
- [ ] `grep -n "agents.stubs" orchestrator.py` returns nothing (orchestrator only imports `agents`)
- [ ] Live smoke test: at least one real mission reaches BRIEF_READY, or parks with notes that clearly say why
- [ ] Report the MCP search package and version used
- [ ] Commit and tag `m2`

When finished, report: files created or changed, test output, stub walkthrough output, live smoke test result (state, attempts, and the validator notes if it parked), and any deviations with reasons.
