"""What the loop is working on right now, so the UI can show it.

The loop and the UI are separate processes and the database only changes when a task ends, so each
agent's start is recorded in one small file. The two workers (settings.WORKERS) can each be busy at once, each
as several copies (settings.seats()), so the file holds one entry per seat: {"assistant": {...}, "assistant-2":
{...}, "pi": {...}}. Work done outside a worker
(publishing, posting to channels) is filed under "desk". It is display-only: a failure here never stops a
mission. A killed loop leaves the file behind, so only trust it while the loop holds its lock.

Every step that ends (the seat's next begin, or clear) is appended to the step log, data/steps.jsonl, one JSON line
each: {"worker", "slug", "agent", "since", "ended", "outcome"} (outcome "done", "failed" or "stopped", with "why" for
the last two), and "model": the model the seat's worker was set to when the step began (none for the desk, or a worker
with no model; lines from before #121 have none either). It is the whole history, kept on this machine; the UI shows
the latest under the pipeline strip, all of it on /log, and pass rates and times per step and per model on /stats
(stats()). Like the rest of this file it is display-only.
"""
import json, math, os, re, statistics, threading, time
from pathlib import Path
import settings, trail

FILE = Path("data/activity.json")
LOG = Path("data/steps.jsonl")
TAIL = 16384                               # bytes read from the end of the log for the latest steps

AGENTS = {                                 # in pipeline order
    "theorist": "Theorist",                # looks across the whole desk and evolves the inspiration line; slug ""
    "finder": "Mission finder",            # works for the whole desk, so its slug is ""
    "sources": "Source finder",
    "brief": "Brief writer",
    "source_check": "Source checker",
    "outreach": "Outreach agent",          # asks people a question about an approved brief (only while outreach is on)
    "outreach_check": "Outreach checker",  # contacts.send's checks on each message, before it goes
    "replies": "Reply checker",            # reads contacts' answers while a mission waits for them
    "writer": "Article writer",
    "article_check": "Article checker",
    "article_rating": "Article rater",     # the Research Assistant rates a checked article; 20/30 or less goes back
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
    if e.get("model"):
        line["model"] = e["model"]
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(json.dumps(line) + "\n")
    except OSError:
        pass

def seat_model(seat: str) -> str:
    """The model a seat runs on: its worker's ("assistant-2" -> the Research Assistant's); "" for the desk."""
    worker = settings.seat_worker(seat)
    return settings.worker_model(worker) if worker in settings.WORKERS else ""

def begin(slug: str, agent: str) -> None:
    trail.start(slug, agent)               # the chat assistant reads what each agent did from here
    with _lock:
        data = _read()
        _log(_worker(), data.get(_worker()))   # this seat's last step ends here
        data[_worker()] = {"slug": slug, "agent": agent, "since": time.time()} | (
            {"model": model} if (model := seat_model(_worker())) else {})
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
    """The step at work on this seat raised: failed, or stopped for a stop. The step log gets a short reason;
    the raw error goes to the loop log."""
    if type(e).__name__ == "Interrupted":
        outcome("stopped", "stopped by the stop button")
    else:
        print(f"step failed: {type(e).__name__}: {e}", flush=True)
        outcome("failed", reason(e))

def reason(e: BaseException) -> str:
    """A short, plain reason for a step that raised, for History and the step log; the raw text stays in the
    loop log. Network trouble says what couldn't be reached instead of a library's exception dump."""
    name, text = type(e).__name__, " ".join(str(e).split())
    low = f"{name} {text}".lower()
    host = re.search(r"https?://([^/\s'\"]+)", text)
    where = host.group(1) if host else ""
    if any(k in low for k in ("connecterror", "connectionerror", "connection refused", "connection reset",
                              "enotfound", "getaddrinfo", "name or service not known", "network is unreachable")):
        return f"couldn't reach {where}" if where else "couldn't reach a server it needs (network or server down)"
    if "timeout" in low or "timed out" in low:
        return f"{where} took too long to answer" if where else "a server took too long to answer"
    if not text:
        return name
    if getattr(e, "plain", False):         # its message is already a plain sentence
        return text
    first = text if len(text) <= 160 else text[:157].rstrip() + "..."
    return first if name in ("Exception", "RuntimeError") or name.endswith("Failed") else f"{name}: {first}"

SHORT = 160                                # the most of a reason History shows on one line
SOURCE_LINE = re.compile(r"s\d+: ")        # a check's problem with one source ("s2: quote not found ...")

def short_notes(notes: str) -> str:
    """A check's notes as one line for History: its first problem, cut to SHORT characters, and how many more.
    A problem with the whole thing goes ahead of one source's; a line in parentheses is an aside, not a problem."""
    lines = [l.strip() for l in notes.splitlines() if l.strip() and not l.strip().startswith("(")]
    if not lines:
        return ""
    first = next((l for l in lines if not SOURCE_LINE.match(l)), lines[0])
    first = first if len(first) <= SHORT else first[:SHORT - 3].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."
    return first + (f" (+{len(lines) - 1} more)" if len(lines) > 1 else "")

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

# ---------- adding the log up (/stats) ----------

STATS_AGENTS = ("sources", "brief", "source_check", "writer", "article_check", "article_rating", "outreach",
                "theorist", "finder")      # the steps a model runs, whose pass rate says whether a model swap helped
WEEK = 7 * 86400

def percentile(values: list[float], p: float) -> float:
    """The nearest-rank percentile: the smallest value with at least p of them at or below it (0.9: the slowest tenth
    starts here)."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]

def stats(steps: list[dict], by_model: bool = False, since: float = 0) -> list[dict]:
    """Each model step's runs in the log, one row per step in pipeline order (STATS_AGENTS, every one even with no
    runs), or with by_model one per step and model that ran it, most runs first ("" for a line with no model).
    A row: {"agent", "model", "runs", "passed", "failed", "stopped", "rate", "median", "p90"}. Passed is outcome
    "done", so a source check that found a dead lead passed: the check did its job. The rate is passed out of passed
    and failed (None with neither); a stop cuts a step short, so stopped runs count as runs but in neither the rate
    nor the times, which are the passed and failed runs' seconds (None with none). since keeps steps that ended then
    or later."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for e in steps:
        if e.get("agent") in STATS_AGENTS and float(e.get("ended", 0)) >= since:
            key = (e["agent"], (e.get("model") or "") if by_model else "")
            groups.setdefault(key, []).append(e)
    if not by_model:
        for agent in STATS_AGENTS:
            groups.setdefault((agent, ""), [])
    rows = []
    for (agent, model), runs in groups.items():
        kinds = [e.get("outcome", "done") for e in runs]
        passed, failed, stopped = kinds.count("done"), kinds.count("failed"), kinds.count("stopped")
        times = [max(0.0, float(e["ended"]) - float(e.get("since", e["ended"])))
                 for e in runs if e.get("outcome", "done") != "stopped"]
        rows.append({"agent": agent, "model": model, "runs": len(runs), "passed": passed, "failed": failed,
                     "stopped": stopped, "rate": passed / (passed + failed) if passed + failed else None,
                     "median": statistics.median(times) if times else None,
                     "p90": percentile(times, 0.9) if times else None})
    return sorted(rows, key=lambda r: (STATS_AGENTS.index(r["agent"]), -r["runs"], r["model"]))
