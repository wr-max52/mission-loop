"""What the loop is working on right now, so the UI can show it.

The loop and the UI are separate processes and the database only changes when a task ends, so each
agent's start is recorded in one small file. The two workers (settings.WORKERS) can each be busy at once,
so the file holds one entry per worker: {"assistant": {...}, "pi": {...}}. Work done outside a worker
(publishing, posting to channels) is filed under "desk". It is display-only: a failure here never stops a
mission. A killed loop leaves the file behind, so only trust it while the loop holds its lock.
"""
import json, os, threading, time
from pathlib import Path
import trail

FILE = Path("data/activity.json")

AGENTS = {                                 # in pipeline order
    "finder": "Mission finder",            # works for the whole desk, so its slug is ""
    "sources": "Source finder",
    "contacts": "Contact finder",
    "brief": "Brief writer",
    "source_check": "Source checker",
    "writer": "Article writer",
    "article_check": "Article checker",
    "publisher": "Publisher",
    "1f916": "1F916 poster",               # posts published articles, one a day, newest first
    "bluesky": "Bluesky poster",           # announces published articles, at most one every 10 minutes, newest first
}

DESK = "desk"
_here = threading.local()                  # which worker the calling thread works for
_lock = threading.Lock()

def work_as(worker: str) -> None:
    """Called at the start of a worker's task, on its own thread."""
    _here.worker = worker

def _worker() -> str:
    return getattr(_here, "worker", DESK)

def _read() -> dict:
    try:
        data = json.loads(FILE.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}

def _write(data: dict) -> None:
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        if not data:
            FILE.unlink(missing_ok=True)
            return
        tmp = FILE.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, FILE)              # atomic, so the UI never reads half a file
    except OSError:
        pass

def begin(slug: str, agent: str) -> None:
    trail.start(slug, agent)               # the chat assistant reads what each agent did from here
    with _lock:
        data = _read()
        data[_worker()] = {"slug": slug, "agent": agent, "since": time.time()}
        _write(data)

def clear() -> None:
    """This worker is done with its task."""
    trail.stop()
    with _lock:
        data = _read()
        if data.pop(_worker(), None) is not None:
            _write(data)

def reset() -> None:
    """Forget everything, e.g. what a killed loop left behind. Called at the start of each pass."""
    with _lock:
        _write({})

def working() -> list[dict]:
    """Every agent at work now, one per worker: [{"worker", "slug", "agent", "since"}], earliest start first."""
    entries = []
    for worker, e in _read().items():
        if isinstance(e, dict) and e.get("agent") in AGENTS:
            entries.append(e | {"worker": worker})
    return sorted(entries, key=lambda e: e.get("since", 0))

def current() -> dict | None:
    """The agent that started most recently, or None."""
    entries = working()
    return entries[-1] if entries else None
