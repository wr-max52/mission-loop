import fcntl, socket, threading, time, webbrowser
import typer, yaml
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()                              # reads API keys from .env before anything else
import activity, agents, clock, doctor, notify, orchestrator, store
from contracts import Mission, SITE_FOLDERS

app = typer.Typer()
LOCK = orchestrator.LOCK_FILE
WAIT_RETRY = 30                            # seconds between checks while the loop can't work (no model, Ollama closed)

@app.callback()
def _start():
    """Every command finds the database ready, so a fresh clone's first command never crashes."""
    store.init()

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

def _ready(quiet_repeat: bool = False) -> bool:
    """Can the models answer? If not, say why (once per reason in the loop) and record it, so the UI shows the loop
    as waiting with the reason instead of as running."""
    ok, why = agents.ready()
    if ok:
        doctor.clear_waiting()
        return True
    if doctor.set_waiting(why) or not quiet_repeat:
        print(f"[{clock.now():%Y-%m-%d %H:%M}] waiting: {why}", flush=True)
    return False

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
    if slug in SITE_FOLDERS:
        raise typer.BadParameter(f"{slug!r} is one of the published site's own folders; pick another slug")
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

def _busy() -> set[str]:
    return {e["slug"] for e in activity.working()} if orchestrator.loop_running() else set()

@app.command()
def combines():
    """List the Theorist's combines: proposed ones wait for you (approve-combine / reject-combine)."""
    store.init()
    for c in store.all_combines():
        print(f"{c.slug:32} {c.status:9} {' + '.join(c.members)} -> {c.topic}")

@app.command("approve-combine")
def approve_combine(slug: str):
    """Combine the missions of a proposed combine into one bigger mission, which is queued (its brief still comes
    to you); the missions it combines stop."""
    store.init()
    try:
        made = orchestrator.approve_combine(slug, clock.now(), busy=_busy())
    except ValueError as err:
        print(err)
        raise typer.Exit(1) from None
    orchestrator.WAKE_FLAG.parent.mkdir(parents=True, exist_ok=True)
    orchestrator.WAKE_FLAG.touch()
    print(f"combined {', '.join(made.combines)} into {made.slug}")

@app.command("reject-combine")
def reject_combine(slug: str):
    """Keep the missions of a proposed combine separate. The Theorist won't propose it again."""
    store.init()
    try:
        orchestrator.reject_combine(slug, clock.now())
    except ValueError as err:
        print(err)
        raise typer.Exit(1) from None
    print(f"{slug} rejected")

@app.command()
def link(a: str, b: str, why: str = typer.Option("", help="The thread they share; research sees it")):
    """Link two missions while keeping them separate (at least one not published yet), so each one's research
    sees the other."""
    store.init()
    try:
        orchestrator.link(a, b, why, clock.now())
    except ValueError as err:
        print(err)
        raise typer.Exit(1) from None
    print(f"linked {a} <-> {b}")

@app.command()
def unlink(a: str, b: str):
    """Remove a link. The Theorist won't draw it again."""
    store.init()
    try:
        orchestrator.unlink(a, b, clock.now())
    except ValueError as err:
        print(err)
        raise typer.Exit(1) from None
    print(f"unlinked {a} <-> {b}")

@app.command()
def inspire(line: str = typer.Argument(None, help='The new line; "" clears it and turns the finder off')):
    """Show or set the inspiration line the mission finder works from each time the loop has caught up on its
    missions. The Theorist evolves it as the desk's work grows."""
    if line is not None:
        store.set_inspiration(line)
    print(store.get_inspiration() or "no inspiration line set; the mission finder and the Theorist are off")
    last = store.last_theory()
    if last.get("why"):
        print(f"Theorist, {last['at'][:16].replace('T', ' ')} UTC: {last['why']}")

@app.command("library")
def library_cmd(query: str = typer.Argument("", help="What to look for; leave out to index and show the status"),
                epic: str = typer.Option("", help="Only missions in this epic (its title)"),
                mission: str = typer.Option("", help="Only this mission (its slug)")):
    """Search the desk's library (what the Theorist and the chat search), or bring its index up to date."""
    import library
    store.init()
    if not query.strip():
        _, _, note = library.sync(time_limit=30 * 60)
        print(library.summary() + (f" ({note})" if note else ""))
        return
    print(library.ask(query, epic=epic, mission=mission))

@app.command()
def theorize():
    """Ask the Theorist to look across the desk next: it runs as the loop's next task, or when the loop starts."""
    if not store.get_inspiration():
        print("no inspiration line set, so the Theorist has nothing to evolve: set one with `inspire`")
        raise typer.Exit(1)
    orchestrator.THINK_FLAG.parent.mkdir(exist_ok=True)
    orchestrator.THINK_FLAG.touch()
    if orchestrator.loop_running():
        orchestrator.WAKE_FLAG.touch()
        print("the Theorist goes next: when the loop starts its next run (now, if it is resting)")
    else:
        print("the Theorist goes first the next time the loop starts")

@app.command()
def run():
    """Work until nothing is left the loop can do on its own (quick code steps first, then the farthest-along
    model work), then exit."""
    held = _lock()
    orchestrator.STOP_FLAG.unlink(missing_ok=True)
    if _ready():
        n = orchestrator.run_until_idle(clock.now)
        print(f"{n} task(s) ran")
    status()
    held.close()

@app.command()
def loop(every: float = typer.Option(15, help="Minutes to rest when there is nothing left to do")):
    """Keep working forever, resting when nothing is left to do. `python run.py stop` ends it after the current task."""
    held = _lock()
    orchestrator.STOP_FLAG.unlink(missing_ok=True)
    seen: set[tuple[str, str]] = set()
    print(f"[{clock.now():%Y-%m-%d %H:%M}] loop started; it rests up to {every:g} min when nothing is left to do", flush=True)

    def announce():                        # after every model task, so a ready brief never waits on the others
        for line in notify.announce(store.all_missions(), seen):
            print(f"[{clock.now():%Y-%m-%d %H:%M}] notice: {line}", flush=True)

    while not orchestrator.STOP_FLAG.exists():
        ready = _ready(quiet_repeat=True)
        if ready:
            n = orchestrator.run_until_idle(clock.now, after_task=announce)
            if n:
                print(f"[{clock.now():%Y-%m-%d %H:%M}] {n} task(s) ran", flush=True)
                status()
        announce()
        deadline = time.monotonic() + (every * 60 if ready else WAIT_RETRY)   # not ready: look again soon
        while time.monotonic() < deadline and not orchestrator.STOP_FLAG.exists():
            if orchestrator.WAKE_FLAG.exists():  # a human acted in the UI: look for work now
                orchestrator.WAKE_FLAG.unlink(missing_ok=True)
                break
            time.sleep(2)
    doctor.clear_waiting()
    print(f"[{clock.now():%Y-%m-%d %H:%M}] loop stopped", flush=True)
    held.close()

@app.command()
def ui(port: int = typer.Option(8770, help="Port on 127.0.0.1"),
       open_browser: bool = typer.Option(False, "--open", help="Open the UI in your browser once it is up")):
    """The local web UI: missions, briefs, articles, approve/reject/publish, start/stop the loop."""
    import uvicorn
    import ui as web
    from agents import publisher
    if not _port_free(port):
        print(f"Port {port} is already in use: the UI may already be open at http://127.0.0.1:{port}, or another "
              f"app has the port. Pick another: python run.py ui --port {port + 1}", flush=True)
        raise typer.Exit(1)
    Path("site").mkdir(exist_ok=True)      # /site errors if the folder is missing; publishing creates it later
    if not (publisher.SITE / "index.html").exists():
        publisher.rebuild_index()          # so the site and its About page open before anything is published
    url = f"http://127.0.0.1:{port}"
    print(f"Mission Loop UI: {url}  (Ctrl-C to quit)", flush=True)
    if open_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    uvicorn.run(web.app, host="127.0.0.1", port=port, log_level="warning")

def _port_free(port: int) -> bool:
    """Like uvicorn, reuse an address a just-closed UI left in TIME_WAIT; only a live listener counts as taken."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False

@app.command("doctor")
def doctor_cmd():
    """Check this machine's setup: models, their connection, web search, the tools research needs."""
    found = doctor.checks(deep=True)
    print(doctor.report(found))
    if any(c.blocks_loop and not c.ok for c in found):
        raise typer.Exit(1)

@app.command()
def stop():
    import stopping
    stopping.press(orchestrator.STOP_FLAG)
    print(f"stop requested; the current agent writes out what it has (at most {stopping.GRACE // 60} min), then the loop stops")

@app.command()
def auto(switch: str = typer.Argument("", help="on or off; leave out to see whether it is on")):
    """Auto mode approves every brief, proposed epic and combine without you. Publishing still waits for `review`."""
    import settings
    if switch not in ("", "on", "off"):
        raise typer.BadParameter("say on or off")
    if switch:
        settings.set_auto_mode(switch == "on")
        approved = orchestrator.auto_approve(clock.now(), _busy())
        orchestrator.WAKE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        orchestrator.WAKE_FLAG.touch()
        if approved:
            print(f"approved {approved} waiting brief(s)")
    print("auto mode is " + ("on: briefs and new epics are approved for you; articles still wait for review"
                             if settings.auto_mode() else "off: every brief and new epic waits for you"))

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
    if m.combined_into:
        print(f"\nCombined into {m.combined_into}")
    if m.combines:
        print(f"\nCombines {', '.join(m.combines)}")
    for k in store.links_of(slug):
        print(f"Linked to {k.other(slug)} by the {k.by}" + (f": {k.why}" if k.why else ""))

@app.command(name="join-1f916")
def join_1f916(handle: str = typer.Argument(..., help="2-32 letters, digits, _ or -; permanent and public"),
               model: str = typer.Option(None, help="Self-declared model id; defaults to the writing model"),
               server: str = typer.Option(None, help="1F916 server to join; defaults to the configured one")):
    """Register our citizen on 1F916 once and save its secret key, with its server, to data/1f916.json (stays on
    this machine). The server comes from config: --server (saved), the Settings page, MISSION_LOOP_1F916_URL, or 1f916.ai."""
    import settings
    from agents import f916
    if server:
        settings.set_f916_url(server)
    if not model:
        model = settings.model_for("pi") or "unknown"
    try:
        me = f916.register(handle, model)
    except (FileExistsError, RuntimeError) as e:
        print(e)
        raise typer.Exit(1) from None
    print(f"joined 1F916 as {me.get('handle', handle)}; key saved to {f916.KEY_FILE} and checked with the site")

def _channel_summary(m) -> str:
    if m.state.value != "DONE":
        return ""
    names = [orchestrator.channel_name(k) for k in orchestrator.channels(m)]
    if agents.f916_enabled() and "1f916" not in m.channels:
        names.append("1F916 queued")
    if agents.github_enabled() and "github" not in m.channels:
        names.append("GitHub Pages queued")
    if agents.bluesky_enabled() and "bluesky" not in m.channels:
        names.append("Bluesky queued")
    return " published: " + ", ".join(names)

@app.command()
def status():
    for m in store.all_missions():
        extra = f" (wait until {m.wait_until:%m-%d %H:%M})" if m.wait_until and m.state.value == "AWAITING_COMMENTS" else ""
        tries = f"attempts={m.attempts}" + (f" write_attempts={m.write_attempts}" if m.write_attempts else "")
        print(f"{m.slug:20} {m.state.value:18} {tries}{extra}{_channel_summary(m)}{'  [' + m.epic + ']' if m.epic else ''}")
    from agents import f916
    if agents.f916_enabled():
        queued, until = len(orchestrator.queue(f916)), f916.spent_until()
        when = f"next post after {until:%m-%d %H:%M} UTC" if until and until > clock.now() else "can post now"
        print(f"1F916 as {f916.key()['handle']}: {queued} queued, {when}")
    if agents.github_enabled():
        from agents import github
        note = github.load_state().get("note", "")
        print(f"GitHub Pages: {github.repo()} -> {github.site_address()}, {len(orchestrator.queue(github))} queued"
              + (f" (last push failed: {note})" if note else ""))
    if agents.bluesky_enabled():
        from agents import bluesky
        note = bluesky.load_state().get("note", "")
        print(f"Bluesky as {bluesky.handle()}: {len(orchestrator.queue(bluesky))} queued"
              + (f" (last try failed: {note})" if note else ""))

@app.command()
def ask(question: str, model: str = typer.Option("", help="An Ollama model, or provider/model for a connected cloud provider; default: the small search model")):
    """Ask Researcher Chat what the loop is doing, or to look something up on the web. It only reads the loop, so it
    is safe while the loop runs, and works without it."""
    store.init()
    print(agents.chat(question, model=model or None))

@app.command(name="check-bluesky")
def check_bluesky():
    """Log in to Bluesky with BLUESKY_HANDLE and BLUESKY_APP_PASSWORD from .env, without posting anything."""
    from agents import bluesky
    if not bluesky.enabled():
        print("set BLUESKY_HANDLE and BLUESKY_APP_PASSWORD in .env first (an app password, not your account password)")
        raise typer.Exit(1)
    try:
        who = bluesky.check_login()
    except Exception as e:
        print(f"Bluesky login failed: {type(e).__name__}: {e}")
        raise typer.Exit(1) from None
    print(f"logged in to Bluesky as {who}; published articles will be announced there, at most one every 10 minutes")

if __name__ == "__main__":
    app()
