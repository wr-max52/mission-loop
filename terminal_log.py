"""The step log in the terminal (#173): while `python run.py ui` runs, its terminal prints each step as it ends and
each mission that comes to need a human, one line each, cut to the terminal's width and ending in a link to the
UI's missions page. So the terminal alone can be watched and the browser window closed.

  17:42:05  Source finder · Texas grid outage · 4m 12s · done        http://127.0.0.1:8770/
  17:44:31  Brief writer · Texas grid outage · 1m 3s · failed        http://127.0.0.1:8770/
  17:44:32  Texas grid outage · brief needs review                  http://127.0.0.1:8770/

done is green, failed red, stopped yellow and needs review purple; colours only on a terminal that shows them (not
when the output is piped, or NO_COLOR is set). The link is an OSC 8 hyperlink whose text is the address itself, so a
terminal that doesn't know OSC 8 still shows a plain address it can open. Steps come from activity.LOG (written by
the loop, a separate process); needs review comes from the missions. Display only: nothing here can stop the UI.
"""
import json, os, shutil, sys, threading, time
from datetime import datetime
import activity, settings, store
from contracts import Mission, State

EVERY = 2.0                                # seconds between looks
COLORS = {"done": "32", "failed": "31", "stopped": "33", "review": "35"}   # green, red, yellow, purple (ANSI)
LABELS = {"done": "done", "failed": "failed", "stopped": "stopped"}
PAST = {"theorist": "all missions", "finder": "new missions"}             # what a desk-wide step worked on

def color_on(stream=None) -> bool:
    stream = stream or sys.stdout
    return "NO_COLOR" not in os.environ and hasattr(stream, "isatty") and stream.isatty()

def took(seconds: float) -> str:
    """How long a step took: "0.4s", "12s", "4m 12s", "1h 3m" (as the UI says it)."""
    seconds = max(0.0, float(seconds))
    if seconds < 10:
        return f"{seconds:.1f}s"
    s = int(seconds)
    return f"{s // 3600}h {s % 3600 // 60}m" if s >= 3600 else f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"

def _fit(text: str, room: int) -> str:
    return text if len(text) <= room else text[:max(1, room - 1)] + "…"

def line(when: float, text: str, kind: str, url: str, width: int, color: bool) -> str:
    """One terminal line: the time, the text cut to fit, the outcome word in its colour, then the link."""
    stamp = datetime.fromtimestamp(when).strftime("%H:%M:%S")
    word = "needs review" if kind == "review" else LABELS.get(kind, kind)
    room = width - len(stamp) - 2 - len(url) - 2 - len(word) - 3
    body = _fit(text, max(10, room))
    shown = f"\033[{COLORS[kind]}m{word}\033[0m" if color and kind in COLORS else word
    link = f"\033]8;;{url}\033\\{url}\033]8;;\033\\" if color else url
    return f"{stamp}  {body} · {shown}  {link}"

def step_text(e: dict, topics: dict[str, str]) -> str:
    """"Source finder · Texas grid outage · 4m 12s"."""
    task = topics.get(e.get("slug", ""), e.get("slug", "")) if e.get("slug") else PAST.get(e["agent"], "all missions")
    return f"{activity.AGENTS.get(e['agent'], e['agent'])} · {task} · {took(float(e['ended']) - float(e['since']))}"

def needs_review(m: Mission) -> str | None:
    """What a mission waits on a human for, or None: a brief to approve or an article to review."""
    if m.state == State.BRIEF_READY and not m.approved and not settings.auto_mode():
        return "brief"
    if m.state == State.ARTICLE_READY and not m.reviewed:
        return "article"
    return None

class Watcher:
    """Reads the step log from where it stood when the watcher began, and the missions' states, and prints what's new."""

    def __init__(self, url: str, out=None, width: int | None = None):
        self.url, self.out, self.width = url, out or sys.stdout, width
        try:
            self.pos = activity.LOG.stat().st_size      # only steps that end from now on
        except OSError:
            self.pos = 0
        self.seen: set[tuple[str, str]] = set()         # (slug, "brief"/"article") already printed

    def _say(self, when: float, text: str, kind: str) -> None:
        width = self.width or shutil.get_terminal_size((100, 24)).columns
        print(line(when, text, kind, self.url, width, color_on(self.out)), file=self.out, flush=True)

    def steps(self) -> list[dict]:
        """The steps appended since the last look (a half-written last line waits for the next look)."""
        try:
            with activity.LOG.open("rb") as f:
                if f.seek(0, os.SEEK_END) < self.pos:   # the log was cleared or replaced: start at its top
                    self.pos = 0
                f.seek(self.pos)
                chunk = f.read()
        except OSError:
            return []
        done, _, rest = chunk.rpartition(b"\n")
        self.pos += len(chunk) - len(rest)
        out = []
        for raw in done.decode("utf-8", "replace").splitlines():
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if isinstance(e, dict) and e.get("agent") in activity.AGENTS and "ended" in e and "since" in e:
                out.append(e)
        return out

    def look(self) -> None:
        missions = store.all_missions()
        topics = {m.slug: m.topic for m in missions}
        for e in self.steps():
            self._say(float(e["ended"]), step_text(e, topics), e.get("outcome", "done"))
        waiting = set()
        for m in missions:
            what = needs_review(m)
            if what:
                waiting.add((m.slug, what))
                if (m.slug, what) not in self.seen:
                    self._say(time.time(), f"{m.topic} · {what}", "review")
        self.seen = waiting                              # one sent back and ready again is printed again

    def run(self) -> None:
        while True:
            try:
                self.look()
            except Exception as e:                       # display only: a bad line or a locked database waits a turn
                try:
                    print(f"terminal log: {type(e).__name__}: {e}", file=self.out, flush=True)
                except Exception:                        # the terminal itself is gone
                    return
            time.sleep(EVERY)

def start(url: str) -> threading.Thread:
    t = threading.Thread(target=Watcher(url).run, name="terminal-log", daemon=True)
    t.start()
    return t
