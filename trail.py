"""The last few things each agent did, so the chat assistant can say where a mission is and why it is slow.

The loop writes; nothing else does. Each mission has one small file of recent steps (model replies, tool
calls, tool results), cut short and capped, so reading it never touches the running agent. Like activity.py,
it is display-only: a failure here never stops a mission.
"""
import json, re, time
from pathlib import Path

DIR = Path("data/trail")
KEEP = 40                                  # steps kept per mission
MAX_TEXT = 400                             # characters kept per step
FINDER = "_finder"                         # the mission finder works for the whole desk, not one mission

_current: dict = {}                        # set by activity.begin in the loop process: {"slug", "agent"}

def _file(slug: str) -> Path:
    name = re.sub(r"[^a-z0-9-]", "", slug.lower())[:60] or FINDER
    return DIR / f"{name}.jsonl"

def start(slug: str, agent: str) -> None:
    _current.clear()
    _current.update(slug=slug, agent=agent)
    record("start", f"{agent} started")

def stop() -> None:
    _current.clear()

def record(kind: str, text: str) -> None:
    """Add one step for the agent working now. Outside the loop (no agent started) it does nothing."""
    if not _current:
        return
    text = " ".join(str(text).split())
    step = {"at": time.time(), "agent": _current["agent"], "kind": kind,
            "text": text[:MAX_TEXT] + ("…" if len(text) > MAX_TEXT else "")}
    path = _file(_current["slug"])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(step) + "\n")
        lines = path.read_text().splitlines()
        if len(lines) > 2 * KEEP:          # trim now and then, not on every write
            path.write_text("\n".join(lines[-KEEP:]) + "\n")
    except OSError:
        pass

def model_reply(msg) -> None:
    """A model message: its thinking, what it said, and the tools it asked for."""
    if not _current:
        return
    thinking = getattr(msg, "thinking", None)
    if thinking:
        record("thinking", thinking)
    content = (getattr(msg, "content", None) or "").strip()
    think = re.match(r"<think>(.*?)(</think>|$)", content, re.S)   # some models think inline
    if think:
        record("thinking", think.group(1))
        content = content[think.end():].strip()
    for tc in getattr(msg, "tool_calls", None) or []:
        args = dict(tc.function.arguments or {})
        record("tool_call", f"{tc.function.name}({json.dumps(args, ensure_ascii=False)})")
    if content:
        record("said", content)

def recent(slug: str, n: int = 10) -> list[dict]:
    """The last n steps for a mission ("" for the mission finder), oldest first."""
    try:
        lines = _file(slug).read_text().splitlines()
    except OSError:
        return []
    steps = []
    for line in lines[-n:]:
        try:
            steps.append(json.loads(line))
        except ValueError:
            continue
    return steps
