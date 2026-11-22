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
            epic = _epic(item.pop("epic", "") or "")   # missions.yaml names the epic by title
            store.save_mission(Mission(**item, epic=epic))
            print(f"added {item['slug']}")

def _epic(name: str) -> str:
    """The slug of the epic with this title, made if new. Blank means no epic."""
    try:
        return store.create_epic(name).slug if name.strip() else ""
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None

@app.command()
def add(slug: str, topic: str, objective: str = "",
        epic: str = typer.Option("", help="Epic title (1 to 4 words); a new name makes a new epic"),
        wait_hours: float = typer.Option(48, help="Comment wait after outreach (skipped when no one is emailed)")):
    store.init()
    store.save_mission(Mission(slug=slug, topic=topic, objective=objective, epic=_epic(epic), wait_hours=wait_hours))

@app.command()
def epics():
    """List epics with their mission counts. Proposed ones wait for you in the UI."""
    store.init()
    missions = store.all_missions()
    for e in store.all_epics():
        n = sum(m.epic == e.slug for m in missions)
        print(f"{e.title:32} {n:3} missions{'  (proposed)' if e.status == 'proposed' else ''}")
    loose = sum(not m.epic for m in missions)
    if loose:
        print(f"{'(no epic)':32} {loose:3} missions")

@app.command()
def epic(slug: str, title: str = typer.Argument(..., help='Epic title; a new name makes a new epic; "" clears it')):
    """Put a mission in an epic."""
    m = store.get_mission(slug)
    if not m:
        raise typer.BadParameter(f"no mission {slug}")
    _epic(title)                           # validate first
    orchestrator.assign_epic(m, title, clock.now())
    print(f"{slug} -> {title or 'no epic'}")

@app.command("rename-epic")
def rename_epic(name: str, title: str):
    """Rename an epic (by its current title or slug). Published pages are redrawn with the new name."""
    e = store.find_epic(name)
    if not e:
        raise typer.BadParameter(f"no epic {name}")
    try:
        orchestrator.rename_epic(e.slug, title, clock.now())
    except ValueError as err:
        print(err)
        raise typer.Exit(1) from None
    print(f"{e.title} -> {title}")

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

@app.command(name="join-1f916")
def join_1f916(handle: str = typer.Argument(..., help="2-32 letters, digits, _ or -; permanent and public"),
               model: str = typer.Option(None, help="Self-declared model id; defaults to the writing model")):
    """Register our citizen on 1f916.ai once and save its secret key to data/1f916.json (stays on this machine)."""
    from agents import f916
    if not model:
        from agents.llm import MODEL as model
    try:
        me = f916.register(handle, model)
    except (FileExistsError, RuntimeError) as e:
        print(e)
        raise typer.Exit(1) from None
    print(f"joined 1F916 as {me.get('handle', handle)}; key saved to {f916.KEY_FILE} and checked with the site")

def _channel_summary(m) -> str:
    if m.state.value != "DONE":
        return ""
    names = [orchestrator.CHANNEL_NAMES.get(k, k) for k in orchestrator.channels(m)]
    if agents.f916_enabled() and "1f916" not in m.channels:
        names.append("1F916 queued")
    return " published: " + ", ".join(names)

@app.command()
def status():
    for m in store.all_missions():
        extra = f" (wait until {m.wait_until:%m-%d %H:%M})" if m.wait_until and m.state.value == "AWAITING_COMMENTS" else ""
        tries = f"attempts={m.attempts}" + (f" write_attempts={m.write_attempts}" if m.write_attempts else "")
        print(f"{m.slug:20} {m.state.value:18} {tries}{extra}{_channel_summary(m)}{'  [' + m.epic + ']' if m.epic else ''}")
    from agents import f916
    if agents.f916_enabled():
        queued, until = len(orchestrator.f916_queue()), f916.spent_until()
        when = f"next post after {until:%m-%d %H:%M} UTC" if until and until > clock.now() else "can post now"
        print(f"1F916 as {f916.key()['handle']}: {queued} queued, {when}")

if __name__ == "__main__":
    app()
