# Mission Loop: Milestone 1 (Skeleton Loop)

You are building milestone 1 of Mission Loop, a local, start/stop pipeline that turns topics into sourced Markdown articles. This milestone proves the **state machine** works using **stub agents**. No LLMs, no network, no email, no UI.

Read this whole file before writing code. Build exactly what is specified. When done, every item in "Definition of done" must pass.

---

## 1. Architecture rules (do not violate)

1. **The orchestrator is plain Python.** No LLM decides what runs next. The loop is a fixed state machine in `orchestrator.py`.
2. **Agents are swappable functions.** The orchestrator calls exactly five agent functions (`research`, `validate`, `interview`, `check_inbox`, `write`) from `agents/stubs.py`. Later milestones replace the stubs behind the same signatures; no other file should need to change.
3. **Save after every task.** Mission state is persisted to SQLite after each transition so a stopped or killed run resumes exactly where it left off.
4. **Stop between tasks, never mid-task.** A `data/STOP` file is checked before each task.
5. **Time is injected.** Every function that cares about time takes `now` as an argument. Only `run.py` calls `clock.now()`. Tests pass fixed datetimes.
6. **The 48 hour wait is fixed.** Comments never end the wait early. A mission in `AWAITING_COMMENTS` advances only when `now >= wait_until`.
7. **Two human gates.** `BRIEF_READY` is blocked until `approved=True`. `ARTICLE_READY` is blocked until `reviewed=True`. In this milestone, the CLI commands `approve` and `review` set these flags.

## 2. State machine

```
QUEUED -> RESEARCHING -> BRIEF_READY -> AWAITING_COMMENTS -> WRITING -> ARTICLE_READY -> DONE
               |  ^            (needs approved)   (needs now >= wait_until)   (needs reviewed)
               |  | retry (attempts < 3)
               v
            PARKED (after 3 failed validations)
```

| From | To | Condition / action |
|---|---|---|
| QUEUED | RESEARCHING | Always, on next tick |
| RESEARCHING | BRIEF_READY | Stub research + validate pass; save brief artifact |
| RESEARCHING | RESEARCHING | Validate fails and `attempts < max_attempts` (3); store notes |
| RESEARCHING | PARKED | Validate fails and `attempts >= max_attempts` |
| BRIEF_READY | AWAITING_COMMENTS | `approved` is true; run interview stub, save outreach, set `wait_until = now + wait_hours` |
| AWAITING_COMMENTS | WRITING | Always run `check_inbox` and save outreach; advance only if `now >= wait_until` |
| WRITING | ARTICLE_READY | Writer stub saves `missions/<slug>/article.md` |
| ARTICLE_READY | DONE | `reviewed` is true |

`DONE` and `PARKED` are terminal and excluded from `active_missions()`.

## 3. Setup

```bash
mkdir mission-loop && cd mission-loop
python3.12 -m venv .venv && source .venv/bin/activate
pip install pydantic pyyaml typer pytest
mkdir agents tests data missions
touch agents/__init__.py
printf ".venv/\ndata/\nmissions/\n__pycache__/\n" > .gitignore
git init
```

## 4. Files to create

| File | Purpose |
|---|---|
| `contracts.py` | `State` enum and Pydantic models |
| `store.py` | SQLite reads/writes and event log |
| `clock.py` | `now()` with an offset override for manual testing |
| `agents/__init__.py` | Empty |
| `agents/stubs.py` | Fake research, validate, interview, check_inbox, write |
| `orchestrator.py` | `step()`, `tick()`, `run_until_idle()`, `STOP_FLAG` |
| `run.py` | Typer CLI: `init`, `add`, `run`, `stop`, `approve`, `review`, `status` |
| `missions.yaml` | Topic list |
| `pytest.ini` | Makes root modules importable in tests |
| `tests/test_loop.py` | The four acceptance tests |

The reference implementation below has been run and passes all tests. Use it as written unless you find a real bug; if you change behavior, explain why in your summary.

### 4.1 `contracts.py`

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
    validator_notes: str = ""

class Source(BaseModel):
    id: str
    url: str
    title: str
    quote: str
    stance: Literal["supports", "contradicts"]

class POC(BaseModel):
    name: str
    role: str
    email: str
    contact_source_url: str
    why_them: str

class ResearchBrief(BaseModel):
    thesis: str
    claims: list[dict]                     # {"text": ..., "source_ids": [...]}
    counterpoint: str
    sources: list[Source]
    pocs: list[POC] = Field(min_length=3)  # 3 or more POCs
    question_for_pocs: str

class OutreachRecord(BaseModel):
    poc_email: str
    gmail_thread_id: str
    sent_at: datetime
    replies: list[dict] = []               # {"text", "received_at", "is_comment", "ok_to_quote"}
```

### 4.2 `store.py`

```python
import json, sqlite3
from datetime import datetime
from pathlib import Path
from contracts import Mission

DB = Path("data/loop.db")

def conn():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS missions(
            slug TEXT PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS artifacts(
            slug TEXT, kind TEXT, data TEXT, PRIMARY KEY (slug, kind));
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY, slug TEXT, at TEXT,
            from_state TEXT, to_state TEXT, note TEXT);
        """)

def save_mission(m: Mission):
    with conn() as c:
        c.execute(
            "INSERT INTO missions VALUES (?, ?, ?) "
            "ON CONFLICT(slug) DO UPDATE SET state = excluded.state, data = excluded.data",
            (m.slug, m.state.value, m.model_dump_json()))

def get_mission(slug: str) -> Mission | None:
    row = conn().execute("SELECT data FROM missions WHERE slug = ?", (slug,)).fetchone()
    return Mission.model_validate_json(row["data"]) if row else None

def all_missions() -> list[Mission]:
    rows = conn().execute("SELECT data FROM missions ORDER BY rowid").fetchall()
    return [Mission.model_validate_json(r["data"]) for r in rows]

def active_missions() -> list[Mission]:
    return [m for m in all_missions() if m.state.value not in ("DONE", "PARKED")]

def save_artifact(slug: str, kind: str, obj):
    if isinstance(obj, list):
        data = "[" + ",".join(o.model_dump_json() for o in obj) + "]"
    else:
        data = obj.model_dump_json()
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO artifacts VALUES (?, ?, ?)", (slug, kind, data))

def load_artifact(slug: str, kind: str):
    row = conn().execute(
        "SELECT data FROM artifacts WHERE slug = ? AND kind = ?", (slug, kind)).fetchone()
    return json.loads(row["data"]) if row else None

def log_event(slug: str, at: datetime, frm: str, to: str, note: str = ""):
    with conn() as c:
        c.execute("INSERT INTO events (slug, at, from_state, to_state, note) VALUES (?, ?, ?, ?, ?)",
                  (slug, at.isoformat(), frm, to, note))

def events(slug: str) -> list[sqlite3.Row]:
    return conn().execute("SELECT * FROM events WHERE slug = ? ORDER BY id", (slug,)).fetchall()
```

### 4.3 `clock.py`

```python
import os
from datetime import datetime, timedelta, timezone

def now() -> datetime:
    offset = float(os.environ.get("MISSION_LOOP_CLOCK_OFFSET_HOURS", "0"))
    return datetime.now(timezone.utc) + timedelta(hours=offset)
```

### 4.4 `agents/stubs.py`

Any topic containing `FAIL` fails validation. That is how parking is tested.

```python
from pathlib import Path
from contracts import Mission, ResearchBrief, Source, POC, OutreachRecord

def research(m: Mission) -> ResearchBrief:
    return ResearchBrief(
        thesis=f"Stub thesis about {m.topic}",
        claims=[{"text": "Stub claim", "source_ids": ["s1", "s2"]}],
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
        f"# {brief.thesis}\n\n{brief.claims[0]['text']}\n\nComments received: {len(comments)}\n")
    return out
```

### 4.5 `orchestrator.py`

`step()` returns a note string when the mission changed, or `None` when it is blocked (on a human or the clock). `run_until_idle()` keeps ticking until nothing changes, so validation retries complete in a single Run.

```python
from datetime import timedelta
from pathlib import Path
import store
from agents import stubs as agents
from contracts import Mission, State, ResearchBrief, OutreachRecord

STOP_FLAG = Path("data/STOP")

def _brief(m): return ResearchBrief(**store.load_artifact(m.slug, "brief"))
def _outreach(m): return [OutreachRecord(**r) for r in store.load_artifact(m.slug, "outreach") or []]

def step(m: Mission, now) -> str | None:
    """Run at most one task. Return a note if the mission changed, None if blocked."""
    if m.state == State.QUEUED:
        m.state = State.RESEARCHING
        return "started"

    if m.state == State.RESEARCHING:
        brief = agents.research(m)
        ok, notes = agents.validate(m, brief)
        m.attempts += 1
        if ok:
            store.save_artifact(m.slug, "brief", brief)
            m.state = State.BRIEF_READY
            return f"brief passed on attempt {m.attempts}"
        m.validator_notes = notes
        if m.attempts >= m.max_attempts:
            m.state = State.PARKED
            return f"parked after {m.attempts} attempts: {notes}"
        return f"attempt {m.attempts} failed: {notes}"

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

### 4.6 `run.py`

```python
import typer, yaml
from pathlib import Path
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
def status():
    for m in store.all_missions():
        extra = (f" (wait until {m.wait_until:%m-%d %H:%M})"
                 if m.wait_until and m.state.value == "AWAITING_COMMENTS" else "")
        print(f"{m.slug:20} {m.state.value:18} attempts={m.attempts}{extra}")

if __name__ == "__main__":
    app()
```

### 4.7 `missions.yaml`

The second mission fails on purpose to exercise the parked state.

```yaml
- slug: heat-pumps
  topic: Heat pump adoption in cold climates
  objective: Find a defensible thesis about whether heat pumps work below -15C
- slug: fail-test
  topic: FAIL this mission on purpose
  objective: Exercise the parked state
```

### 4.8 `pytest.ini`

```ini
[pytest]
pythonpath = .
```

### 4.9 `tests/test_loop.py`

```python
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
import orchestrator, store
from contracts import Mission, State

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)

@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(store, "DB", tmp_path / "data" / "loop.db")
    monkeypatch.setattr(orchestrator, "STOP_FLAG", tmp_path / "data" / "STOP")
    store.init()

def run_at(t):
    orchestrator.run_until_idle(lambda: t)

def state(slug):
    return store.get_mission(slug).state

def flag(slug, **fields):
    m = store.get_mission(slug)
    for k, v in fields.items():
        setattr(m, k, v)
    store.save_mission(m)

def test_happy_path_waits_full_48_hours():
    store.save_mission(Mission(slug="m1", topic="Heat pumps"))
    run_at(T0)
    assert state("m1") == State.BRIEF_READY          # blocked on approval
    flag("m1", approved=True)
    run_at(T0)
    assert state("m1") == State.AWAITING_COMMENTS
    run_at(T0 + timedelta(hours=47, minutes=59))
    assert state("m1") == State.AWAITING_COMMENTS    # still waiting
    run_at(T0 + timedelta(hours=48))
    assert state("m1") == State.ARTICLE_READY
    assert Path("missions/m1/article.md").exists()
    flag("m1", reviewed=True)
    run_at(T0 + timedelta(hours=49))
    assert state("m1") == State.DONE

def test_forced_failure_parks_after_3_attempts():
    store.save_mission(Mission(slug="bad", topic="FAIL on purpose"))
    run_at(T0)
    m = store.get_mission("bad")
    assert m.state == State.PARKED and m.attempts == 3

def test_stop_then_resume():
    store.save_mission(Mission(slug="m2", topic="Anything"))
    orchestrator.STOP_FLAG.touch()
    run_at(T0)
    assert state("m2") == State.QUEUED              # nothing ran
    orchestrator.STOP_FLAG.unlink()
    run_at(T0)
    assert state("m2") == State.BRIEF_READY         # resumed where it was

def test_every_transition_is_logged():
    store.save_mission(Mission(slug="m3", topic="Anything"))
    run_at(T0)
    moves = [(e["from_state"], e["to_state"]) for e in store.events("m3")]
    assert moves == [("QUEUED", "RESEARCHING"), ("RESEARCHING", "BRIEF_READY")]
```

## 5. Manual walkthrough

Run from the project root. Expected output is shown in comments.

```bash
python run.py init                                     # added heat-pumps, added fail-test
python run.py run                                      # heat-pumps BRIEF_READY attempts=1; fail-test PARKED attempts=3
python run.py approve heat-pumps
python run.py run                                      # heat-pumps AWAITING_COMMENTS (wait until ...)
python run.py status                                   # same state after a fresh terminal
MISSION_LOOP_CLOCK_OFFSET_HOURS=49 python run.py run   # heat-pumps ARTICLE_READY
cat missions/heat-pumps/article.md
python run.py review heat-pumps
python run.py run                                      # heat-pumps DONE
```

## 6. Out of scope (do NOT build)

- Ollama, LangGraph, MCP, or any LLM call
- Network access of any kind
- Gmail, email, or reply classification
- Streamlit or any UI
- Real source validation
- Background scheduling, daemons, or threads
- Extra abstraction layers (plugin registries, base classes, dependency injection frameworks)

## 7. Definition of done

- [ ] `pytest -q` shows `4 passed`
- [ ] Manual walkthrough ends with `heat-pumps DONE` and `fail-test PARKED`
- [ ] `python run.py status` in a new terminal shows the same states as before (state persists)
- [ ] Only `agents/stubs.py` would need to change to swap in a real agent
- [ ] Code committed and tagged `m1`

When finished, report: files created, test output, walkthrough output, and any deviations from this spec with reasons.


