import fcntl, time
import typer, yaml
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()                              # reads API keys from .env before anything else
import activity, agents, clock, notify, orchestrator, store
from contracts import Mission

app = typer.Typer()
LOCK = orchestrator.LOCK_FILE

def _lock():
    """Only one run/loop at a time, so a manual `run` can't race the background loop."""
    LOCK.parent.mkdir(exist_ok=True)
    f = LOCK.open("w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"another run or loop is already working (lock: {LOCK})")
        raise typer.Exit(1) from None
    activity.clear()                       # a killed loop may have left its last task behind
    return f                               # keep the handle open to hold the lock

def _set(slug: str, **fields):
    m = store.get_mission(slug)
    if not m:
        raise typer.BadParameter(f"no mission {slug}")
    for k, v in fields.items():
        setattr(m, k, v)
    store.save_mission(m)

def _ready() -> bool:
    ok, why = agents.ready()
    if not ok:
        print(f"not running: {why}", flush=True)
    return ok

@app.command()
def init(path: str = "missions.yaml"):
    store.init()
    for item in yaml.safe_load(Path(path).read_text()) or []:
        if not store.get_mission(item["slug"]):
            store.save_mission(Mission(**item))
            print(f"added {item['slug']}")

@app.command()
def add(slug: str, topic: str, objective: str = "",
        wait_hours: float = typer.Option(48, help="Comment wait after outreach (skipped when no one is emailed)")):
    store.init()
    store.save_mission(Mission(slug=slug, topic=topic, objective=objective, wait_hours=wait_hours))

@app.command()
def inspire(line: str = typer.Argument(None, help='The new line; "" clears it and turns the finder off')):
    """Show or set the inspiration line the mission finder works from on every pass."""
    if line is not None:
        store.set_inspiration(line)
    print(store.get_inspiration() or "no inspiration line set; the mission finder is off")

@app.command()
def run():
    """One pass: run every mission until each is blocked or finished, then exit."""
    held = _lock()
    orchestrator.STOP_FLAG.unlink(missing_ok=True)
    if _ready():
        n = orchestrator.run_until_idle(clock.now)
        print(f"{n} task(s) ran")
    status()
    held.close()

@app.command()
def loop(every: float = typer.Option(15, help="Minutes to sleep between passes")):
    """Keep running passes forever. `python run.py stop` ends it after the current task."""
    held = _lock()
    orchestrator.STOP_FLAG.unlink(missing_ok=True)
    seen: set[tuple[str, str]] = set()
    print(f"[{clock.now():%Y-%m-%d %H:%M}] loop started; a pass every {every:g} min", flush=True)
    while not orchestrator.STOP_FLAG.exists():
        if _ready():
            n = orchestrator.run_until_idle(clock.now)
            if n:
                print(f"[{clock.now():%Y-%m-%d %H:%M}] {n} task(s) ran", flush=True)
                status()
        for line in notify.announce(store.all_missions(), seen):
            print(f"[{clock.now():%Y-%m-%d %H:%M}] notice: {line}", flush=True)
        deadline = time.monotonic() + every * 60
        while time.monotonic() < deadline and not orchestrator.STOP_FLAG.exists():
            if orchestrator.WAKE_FLAG.exists():  # a human acted in the UI: run a pass now
                orchestrator.WAKE_FLAG.unlink(missing_ok=True)
                break
            time.sleep(2)
    print(f"[{clock.now():%Y-%m-%d %H:%M}] loop stopped", flush=True)
    held.close()

@app.command()
def ui(port: int = typer.Option(8770, help="Port on 127.0.0.1")):
    """The local web UI: missions, briefs, articles, approve/reject/publish, start/stop the loop."""
    import uvicorn
    import ui as web
    store.init()
    Path("site").mkdir(exist_ok=True)      # /site errors if the folder is missing; publishing creates it later
    print(f"Mission Loop UI: http://127.0.0.1:{port}  (Ctrl-C to quit)", flush=True)
    uvicorn.run(web.app, host="127.0.0.1", port=port, log_level="warning")

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
def reject(slug: str, notes: str = typer.Option(..., help="What the agent must fix")):
    m = store.get_mission(slug)
    orchestrator.reject(m, notes, clock.now())
    print(f"{slug} sent back to {m.state.value.lower()}")

@app.command()
def requeue(slug: str):
    m = store.get_mission(slug)
    orchestrator.requeue(m, clock.now())
    print(f"{slug} requeued to {m.state.value}")

@app.command()
def push(slug: str):
    """Move a parked mission to its next step using its last failed draft, overriding the check."""
    m = store.get_mission(slug)
    if not m:
        raise typer.BadParameter(f"no mission {slug}")
    try:
        orchestrator.push_through(m, clock.now())
    except ValueError as e:
        print(e)
        raise typer.Exit(1) from None
    print(f"{slug} pushed through to {m.state.value}; it still needs your review")

@app.command()
def show(slug: str):
    m = store.get_mission(slug)
    folder = Path("missions") / slug
    name = "article.md" if m.state.value in ("ARTICLE_READY", "DONE") else "brief.md"
    if m.state.value == "PARKED":          # the draft `push` would move on
        name = "article.rejected.md" if orchestrator.parked_while_writing(m) else "brief.rejected.md"
    doc = folder / name
    print(doc.read_text() if doc.exists() else f"nothing to show yet ({m.state.value})")
    if m.state.value == "ARTICLE_READY":
        print(f"\nPreview in a browser: open {folder / 'preview.html'}")
    if m.validator_notes:
        print("\nLast validator / reviewer notes:\n" + m.validator_notes)
    if m.unchecked_brief:
        print("\nBrief pushed through despite these source check problems:\n" + m.unchecked_brief)
    if m.unchecked_article:
        print("\nArticle pushed through despite these check problems:\n" + m.unchecked_article)

@app.command()
def status():
    for m in store.all_missions():
        extra = f" (wait until {m.wait_until:%m-%d %H:%M})" if m.wait_until and m.state.value == "AWAITING_COMMENTS" else ""
        tries = f"attempts={m.attempts}" + (f" write_attempts={m.write_attempts}" if m.write_attempts else "")
        print(f"{m.slug:20} {m.state.value:18} {tries}{extra}")

if __name__ == "__main__":
    app()
