"""What the loop is working on right now, so the UI can show it.

The loop and the UI are separate processes and the database only changes when a task ends, so each
agent's start is recorded in one small file. The two workers (settings.WORKERS) can each be busy at once, each
as several copies (settings.seats()), so the file holds one entry per seat: {"assistant": {...}, "assistant-2":
{...}, "pi": {...}}. Work done outside a worker
(publishing, posting to channels) is filed under "desk". It is display-only: a failure here never stops a
mission. A killed loop leaves the file behind, so only trust it while the loop holds its lock.

Every step that ends (the seat's next begin, or clear) is appended to the step log, data/steps.jsonl, one JSON line
each: {"worker", "slug", "agent", "since", "ended", "outcome"} (outcome "done", "failed" or "stopped", with "why" for
the last two). It is the whole history, kept on this machine; the UI shows the latest under the pipeline strip and
all of it on /log. Like the rest of this file it is display-only.
"""
import json, os, threading, time
from pathlib import Path
import trail

FILE = Path("data/activity.json")
LOG = Path("data/steps.jsonl")
TAIL = 16384                               # bytes read from the end of the log for the latest steps

AGENTS = {                                 # in pipeline order
    "theorist": "Theorist",                # looks across the whole desk and evolves the inspiration line; slug ""
    "finder": "Mission finder",            # works for the whole desk, so its slug is ""
    "sources": "Source finder",
    "contacts": "Contact finder",
    "brief": "Brief writer",
    "source_check": "Source checker",
    "writer": "Article writer",
    "article_check": "Article checker",
    "publisher": "Publisher",
    "1f916": "1F916 poster",               # posts published articles, one a day, newest first
    "github": "Site pusher",               # pushes site/ to the linked GitHub Pages repo
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

def _log(worker: str, e: dict | None) -> None:
    """Append a finished step to the step log. Called under _lock."""
    if not isinstance(e, dict) or e.get("agent") not in AGENTS:
        return
    line = {"worker": worker, "slug": e.get("slug", ""), "agent": e["agent"], "since": e.get("since", 0),
            "ended": time.time(), "outcome": e.get("outcome", "done")}
    if e.get("why"):
        line["why"] = e["why"]
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(json.dumps(line) + "\n")
    except OSError:
        pass

def begin(slug: str, agent: str) -> None:
    trail.start(slug, agent)               # the chat assistant reads what each agent did from here
    with _lock:
        data = _read()
        _log(_worker(), data.get(_worker()))   # this seat's last step ends here
        data[_worker()] = {"slug": slug, "agent": agent, "since": time.time()}
        _write(data)

def outcome(kind: str, why: str) -> None:
    """How the step at work on this seat ended, if not "done": "failed" or "stopped", and why. The step log records
    it when the step ends. The first outcome given stands, so a crash isn't renamed by the check that follows it."""
    with _lock:
        data = _read()
        e = data.get(_worker())
        if isinstance(e, dict) and "outcome" not in e:
            e |= {"outcome": kind, "why": " ".join(str(why).split())[:300]}
            _write(data)

def fail(e: BaseException) -> None:
    """The step at work on this seat raised: failed, or stopped for a stop."""
    if type(e).__name__ == "Interrupted":
        outcome("stopped", "stopped by the stop button")
    else:
        outcome("failed", f"{type(e).__name__}: {e}")

def clear() -> None:
    """This worker is done with its task."""
    trail.stop()
    with _lock:
        data = _read()
        e = data.pop(_worker(), None)
        if e is not None:
            _log(_worker(), e)
            _write(data)

def reset() -> None:
    """Forget everything, e.g. what a killed loop left behind. Called at the start of each pass."""
    with _lock:
        _write({})

def working() -> list[dict]:
    """Every agent at work now, one per seat: [{"worker" (the seat), "slug", "agent", "since"}], earliest start first."""
    entries = []
    for worker, e in _read().items():
        if isinstance(e, dict) and e.get("agent") in AGENTS:
            entries.append(e | {"worker": worker})
    return sorted(entries, key=lambda e: e.get("since", 0))

def current() -> dict | None:
    """The agent that started most recently, or None."""
    entries = working()
    return entries[-1] if entries else None

def _lines(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except ValueError:                 # a line still being written, or the cut start of the tail
            continue
        if isinstance(e, dict) and e.get("agent") in AGENTS:
            out.append(e)
    return out

def recent(n: int = 3) -> list[dict]:
    """The last n finished steps, newest first. Reads only the end of the log."""
    try:
        with LOG.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - TAIL))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    return _lines(text)[::-1][:n]

def history() -> list[dict]:
    """Every finished step in the log, newest first."""
    try:
        return _lines(LOG.read_text(errors="replace"))[::-1]
    except OSError:
        return []
