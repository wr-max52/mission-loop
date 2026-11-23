"""Local web UI. Run it with `python run.py ui`, then open http://127.0.0.1:8770.

It is a thin layer over the same store and orchestrator functions the CLI uses: it reads missions,
shows briefs and articles, and records human decisions (approve, reject, publish, requeue, push). It never
runs agents itself. Work happens in the loop, which the UI can start, stop and wake. Bound to
127.0.0.1 only; nothing here is reachable from another machine.
"""
import asyncio, concurrent.futures, hashlib, json, os, re, subprocess, sys, threading, time
from html import escape
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, quote
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (HTMLResponse, RedirectResponse, PlainTextResponse, Response,
                                 JSONResponse)
from starlette.routing import Route, Mount
from starlette.concurrency import run_in_threadpool
from starlette.staticfiles import StaticFiles
import activity, agents, avatars, board, claim_tags, clock, contacts, doctor, library, mcp_share, orchestrator, search_sources, search_status, settings, stopping, store
from agents import llm, providers
from agents import bluesky, f916, github, publisher
from contracts import (Mission, State, ResearchBrief, Article, ArticleRating, Epic, epic_title, is_open, slugify, SITE_FOLDERS,
                       RATING_FACTORS, RATING_FAIL_AT, RATING_SCALE)
from render import CSS, brief_html, epic_pill
from icons import icon, PATHS as ICON_PATHS, CSS as ICON_CSS
import flowchart, i18n
from flowchart import flow_steps
from i18n import _, _n

LOG = Path("data/loop.log")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")

LABELS = {
    State.QUEUED: "Queued", State.RESEARCHING: "Researching", State.BRIEF_READY: "Brief ready",
    State.OUTREACH: "Outreach", State.AWAITING_COMMENTS: "Waiting for replies", State.WRITING: "Writing",
    State.ARTICLE_READY: "Article ready", State.DONE: "Published", State.PARKED: "Parked",
    State.COMBINED: "Combined", State.DEAD_LEAD: "Dead lead",
}
MESSAGES = {
    "approved": "Brief approved.",
    "published": "Article approved for publishing.",
    "rejected": "Sent back with notes.",
    "requeued": "Requeued.",
    "pushed": "Pushed through to review.",
    "added": "Mission added.",
    "added_next": "Mission added. Research Assistant takes it next.",
    "added_started": "Mission added. Loop started.",
    "inspired": "Inspiration saved.",
    "uninspired": "Inspiration cleared. Mission finder and Theorist off.",
    "thinking": "Theorist runs next.",
    "started": "Loop started.",
    "stopping": "Stopping after the current task.",
    "woken": "Checking for work.",
    "epic_set": "Epic saved.",
    "epic_approved": "Epic approved.",
    "epic_moved": "Missions moved.",
    "epic_rejected": "Epic rejected. Missions moved back.",
    "epic_renamed": "Epic renamed.",
    "epic_merged": "Epic merged.",
    "epic_deleted": "Epic deleted.",
    "combined": "Combine approved. Combined mission queued.",
    "combine_rejected": "Combine rejected.",
    "dead_lead": "Marked as a dead lead.",
    "dead_approved": "Dead lead approved. Mission stopped.",
    "dead_rejected": "Kept going.",
    "revived": "Dead lead revived. Queued again.",
    "claim_set": "Claim tag saved.",
    "linked": "Missions linked.",
    "unlinked": "Link removed.",
    "site_url_saved": "Site address saved.",
    "site_url_cleared": "Site address cleared.",
    "bluesky_on": "Bluesky connected.",
    "bluesky_off": "Bluesky disconnected.",
    "every_saved": "Rest time saved. Applies when the loop next starts.",
    "auto_on": "Auto mode on.",
    "auto_off": "Auto mode off.",
    "animations_on": "Animations on.",
    "animations_off": "Animations off.",
    "library_saved": "Library settings saved.",
    "library_indexing": "Indexing the library. Refresh in a minute.",
    "theme_saved": "Appearance saved.",
    "language_saved": "Language saved.",
    "chat_color_saved": "Chat colour saved.",
    "board_style_saved": "Board look saved.",
    "ollama_saved": "Ollama address saved.",
    "ollama_connected": "Ollama connected. Models assigned.",
    "ollama_ready": "Ollama connected. Press play to start.",
    "provider_on": "Provider connected.",
    "provider_off": "Provider disconnected. Key deleted.",
    "worker_saved": "Model saved.",
    "worker_on": "Worker on.",
    "worker_off": "Worker off.",
    "worker_count": "Worker count saved.",
    "joined": "Joined 1F916.",
    "github_connected": "GitHub connected. Choose a repo.",
    "github_linked": "Repo linked and site pushed. Pages can take a few minutes.",
    "github_unlinked": "Repo unlinked.",
    "github_off": "GitHub disconnected.",
    "github_app_saved": "OAuth client ID saved.",
    "custom_added": "Channel added.",
    "custom_removed": "Channel removed.",
    "republish": "Publishing queued.",
    "republish_off": "Publishing now.",
    "email_saved": "Email saved.",
    "email_off": "Email removed.",
    "outreach_saved": "Outreach saved.",
    "search_saved": "Search key saved.",
    "search_unchecked": "Search key saved, but the source didn't answer. If search fails, check the key.",
    "search_off": "Search key removed.",
    "search_order": "Web search sources saved.",
    "mcp_on": "Sharing on. Copy a setup below into your app.",
    "mcp_off": "Sharing off. Nothing on the network can reach the tools.",
    "mcp_token": "New token made. Apps with the old one are refused.",
    "mcp_port": "Port saved.",
    "setup_hidden": "Setup hidden. Settings → Setup brings it back.",
    "setup_shown": "Setup shown on the Missions page.",
    "publication_saved": "Publication saved.",
}
ERRORS = {
    "outreach": "Wait is 0 to 336 hours; 1 to 6 people per mission.",
    "mcp_start": "Sharing didn't start. The MCP server part of Settings says why.",
    "mcp_port": "A port is a number from 1024 to 65535.",
    "combine": "Can't combine now: a mission is in use or has moved on.",
    "dead_lead": "Can't mark it a dead lead now: it is in use or being written.",
    "claim": "Pick confirmed or unconfirmed.",
    "link": "Can't link those two: at least one must be unpublished.",
    "topic": "A mission needs a topic.",
    "theme": "Pick System, Light or Dark.",
    "language": "Pick a language from the list.",
    "board_style": "Pick a surface, cards and string.",
    "chat_color": "That colour wasn't a #rrggbb colour.",
    "slug": "Short name: lowercase letters, numbers and dashes only.",
    "exists": "A mission with that short name already exists.",
    "notes": "Add notes on what to fix.",
    "state": "That action no longer applies.",
    "running": "The loop is already running.",
    "every": "Enter whole minutes.",
    "top_n": "Pick a number from the list.",
    "epic": "An epic title is 1 to 4 words.",
    "epic_exists": "There is already an epic with that name.",
    "epic_target": "Pick a different approved epic.",
    "epic_busy": "A worker is busy with one of its missions. Try again once that task ends.",
    "epic_not_empty": "That epic still has missions. Merge it into another instead.",
    "site_url": "Enter a full http(s):// address, or leave it empty.",
    "bluesky": "Bluesky needs both a handle and an app password.",
    "worker_model": "Pick a model from the list.",
    "provider": "Unknown provider.",
    "ollama_empty": "Ollama has no models. Pull one first (ollama pull <model>).",
    "ollama_host": "Enter a full http(s):// address, or leave it empty.",
    "server": "Enter a full http(s):// address, or leave it empty.",
    "worker_last": "At least one worker must stay on.",
    "worker_count": "Enter a whole number from 1 to {max}.",
    "ollama": "Can't reach Ollama. Start it and try again.",
    "handle": "A 1F916 handle is 2 to 32 letters, digits, _ or -.",
    "github_app": "That isn't a valid OAuth client ID.",
    "places": "Pick at least one place.",
    "notready": "The loop can't start yet",
    "search_key": "That source refused the key.",
    "search_format": "That isn't an API key. Paste it without spaces.",
    "publication": "Too long. Limits: name {name}, tagline {tagline}, About {about} characters.",
}

# ---------- loop status ----------

def loop_running() -> bool:
    return orchestrator.loop_running()

def running_now() -> list[dict]:
    """Every agent working this moment, at most one per seat (a copy of a worker). The record only counts while the
    loop is alive."""
    return activity.working() if loop_running() else []

def start_loop(every: float) -> None:
    """Start `run.py loop` in the background, detached from the UI, logging to data/loop.log."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    orchestrator.STOP_FLAG.unlink(missing_ok=True)
    with LOG.open("a") as log:
        subprocess.Popen([sys.executable, "-u", "run.py", "loop", "--every", f"{every:g}"],
                         stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    for _ in range(25):                    # give it a moment to take the lock so the page shows it running
        if loop_running():
            break
        time.sleep(0.2)

IDLE_CHECK = 30                            # seconds between the idle unloader's looks

def idle_check(off_since: float | None, now: float) -> float | None:
    """One look of the idle unloader (#107): once the loop has been off llm.IDLE_UNLOAD, unload Mission Loop's
    models Ollama still holds for longer than that (a loop that died, or an old 24-hour call). Returns when the loop
    was first seen off, None while it runs. Never raises."""
    try:
        if loop_running():
            return None
        off_since = now if off_since is None else off_since
        if now - off_since >= llm.IDLE_UNLOAD and (gone := llm.unload_pinned()):
            print(f"loop off {int(now - off_since) // 60} min: unloaded {', '.join(gone)}", flush=True)
        return off_since
    except Exception:
        return off_since

def idle_unloader() -> None:
    """Runs on a thread beside the UI (run.py ui) for as long as it is open."""
    off_since = None
    while True:
        off_since = idle_check(off_since, time.monotonic())
        time.sleep(IDLE_CHECK)

def publish_now() -> threading.Thread:
    """The loop is off, so publish what a human just asked for from here, in the background: no model is needed,
    and the page shows each place as it is reached. A request that fails stays for the next try or the loop."""
    def go():
        try:
            orchestrator.publish_requested(clock.now())
        except Exception as e:             # the request stays; the loop or the next press tries again
            print(f"publishing failed: {type(e).__name__}: {e}", flush=True)
    t = threading.Thread(target=go, name="publish", daemon=True)
    t.start()
    return t

def wake() -> None:
    orchestrator.WAKE_FLAG.parent.mkdir(parents=True, exist_ok=True)
    orchestrator.WAKE_FLAG.touch()

def log_tail(n: int = 40) -> list[str]:
    if not LOG.exists():
        return []
    lines = [l for l in LOG.read_text(errors="replace").splitlines() if "HTTP Request" not in l]
    return lines[-n:]

# ---------- mission helpers ----------

def needs_you(m: Mission) -> bool:
    return ((m.state == State.BRIEF_READY and not m.approved)
            or (m.state == State.ARTICLE_READY and not m.reviewed)
            or m.state == State.PARKED)

def tone(m: Mission) -> str:
    """The highlight color class: blue brief to review, green article to publish, red parked."""
    if m.state == State.BRIEF_READY and not m.approved:
        return "review"
    if m.state == State.ARTICLE_READY:
        return "publish"
    return "parked" if m.state == State.PARKED else ""

def label(m: Mission) -> str:
    if m.state == State.DONE and m.publish_requests:
        return _("Publishing again")
    if m.state == State.BRIEF_READY and m.approved:
        return _("Approved")
    if m.state == State.ARTICLE_READY and m.reviewed:
        return _("Publishing next")
    return _(LABELS[m.state])

def next_step(m: Mission) -> str:
    if m.state == State.BRIEF_READY and not m.approved:
        return _("Brief needs review")
    if m.state == State.ARTICLE_READY and not m.reviewed:
        rated = f"{rating_summary(m.article_rating)} · " if m.article_rating else ""
        return rated + _("Article needs review")
    if m.state == State.PARKED:
        stopped = (_("Stopped after {n} writing attempts") if orchestrator.parked_while_writing(m)
                   else _("Stopped after {n} research attempts"))
        return stopped.format(n=m.max_attempts)
    if m.state == State.RESEARCHING:
        if m.brief_sources:
            doing = _("Writing claims")
        elif m.brief_pick:
            doing = _("Checking sources")
        elif m.gathered or m.interrupted_brief:
            doing = _("Writing brief")
        else:
            doing = _("Resuming search") if m.search_paused else _("Finding sources")
        same = m.gathered or m.interrupted_brief or m.search_paused   # the attempt already counted carries on
        try_ = m.attempts if same else m.attempts + 1
        attempt = _("attempt {n} of {total}").format(n=try_, total=m.max_attempts)
        return f"{doing} · {attempt}" if m.attempts and try_ > 1 else doing
    if m.state == State.OUTREACH:
        asked = len(contacts.records(m.slug))
        return _("Finding people to ask") + (" · " + _("{n} asked").format(n=asked) if asked else "")
    if m.state == State.AWAITING_COMMENTS:
        found = contacts.records(m.slug)
        if found and m.wait_until:
            return _("Waiting for replies until {when} UTC · {n} of {total} answered").format(
                when=f"{m.wait_until:%b %d, %H:%M}", n=sum(1 for r in found if r.replies), total=len(found))
    if m.state == State.WRITING:
        if m.checked_article:
            return _("Rating the article") + " · " + _("attempt {n} of {total}").format(n=m.write_attempts, total=m.max_attempts)
        attempt = _("attempt {n} of {total}").format(n=m.write_attempts + 1, total=m.max_attempts)
        done = (" · " + _("{n} of {total} sections").format(n=len(m.article_draft.sections),
                                                           total=len(m.article_draft.plan.sections))
                if m.article_draft else "")
        return f'{_("Writing")} · {attempt}{done}'
    if m.state == State.DONE and m.publish_requests:
        return _("To {places}").format(places=", ".join(_(orchestrator.channel_name(k)) for k in m.publish_requests))
    return ""

def in_progress(m: Mission) -> bool:
    """On the Missions page: everything not published yet, and published articles a human asked to publish again.
    A mission combined into another is not: the bigger one carries its work on."""
    return not needs_you(m) and m.state not in (State.COMBINED, State.DEAD_LEAD) \
        and (m.state != State.DONE or bool(m.publish_requests))

def flow_rank(m: Mission) -> int:
    """Top to bottom on the Missions page, the flow chart read from its end: closest to published first."""
    order = {State.DONE: 0, State.ARTICLE_READY: 1, State.WRITING: 2, State.AWAITING_COMMENTS: 3, State.OUTREACH: 3,
             State.BRIEF_READY: 4, State.RESEARCHING: 5, State.QUEUED: 7, State.PARKED: 8, State.COMBINED: 9,
             State.DEAD_LEAD: 9}
    return order[m.state] + (m.state == State.RESEARCHING and orchestrator.role(m) != "pi")   # a brief before a search

def _brief(slug: str) -> ResearchBrief | None:
    data = store.load_artifact(slug, "brief")
    return ResearchBrief(**data) if data else None

def _article(slug: str) -> Article | None:
    data = store.load_artifact(slug, "article")
    return Article(**data) if data else None

def working_on(m: Mission, now: list[dict]) -> str:
    """A line saying which agent is on this mission right now, or "" if none is."""
    e = next((e for e in now or [] if e["slug"] == m.slug), None)
    return _("{agent} working now").format(agent=agent_name(e["agent"])) if e else ""

def agent_name(agent: str) -> str:
    return _(activity.AGENTS[agent])

def worker_name(worker: str) -> str:
    return _(settings.WORKERS[worker])

def seat_name(seat: str) -> str:
    """A worker copy's name in the UI's language ("Research Assistant 2"); "" for a seat that is no worker's."""
    worker, _sep, n = seat.partition("-")
    return f"{_(settings.WORKERS[worker])} {n}".strip() if worker in settings.WORKERS else ""

def elapsed(since: float) -> str:
    seconds = max(0, int(time.time() - since))
    return f"{seconds // 60}m {seconds % 60}s" if seconds >= 60 else f"{seconds}s"

def signature() -> str:
    """Changes whenever anything the dashboard shows changes; the page polls it to refresh itself."""
    with store.conn() as c:
        rows = c.execute("SELECT data FROM missions ORDER BY rowid").fetchall()
        rows += c.execute("SELECT slug || title || status AS data FROM epics ORDER BY rowid").fetchall()
        last = c.execute("SELECT max(id) FROM events").fetchone()[0]
    working = ",".join(f'{e["worker"]}:{e["slug"]}:{e["agent"]}' for e in running_now())
    raw = ("".join(r["data"] for r in rows) + f"|{last}|{loop_running()}|{orchestrator.STOP_FLAG.exists()}|{working}"
           f"|{settings.auto_mode()}|{(doctor.waiting() or {}).get('reason', '')}|{settings.setup_dismissed()}"
           f"|{','.join(settings.search_sources_on('web'))}")
    return hashlib.sha1(raw.encode()).hexdigest()[:12]

# ---------- page shell ----------

UI_CSS = """
main { max-width: 56rem; }
.onboard { display:inline-flex; align-items:center; gap:.25rem; margin-left:.6rem; font-size:.82em; color:var(--muted);
           text-decoration:none; cursor:pointer; white-space:nowrap; }
.onboard:hover { color:var(--accent); }
.onboard svg { width:.95em; height:.95em; fill:none; stroke:currentColor; stroke-width:2; stroke-linecap:round; }
body { font-family: -apple-system, system-ui, sans-serif; font-size: 16px; }
nav.top { display:flex; justify-content:space-between; align-items:center; gap:1rem; flex-wrap:wrap;
          border-bottom:1px solid var(--rule); padding-bottom:1rem; margin-bottom:1.5rem; }
nav.top a.brand { font:400 1.25rem "American Typewriter", "Courier Prime", "Courier New", Courier, monospace;
                  letter-spacing:.04em; color:var(--fg); text-decoration:none;
                  text-shadow:.5px .5px 0 currentColor, -.3px 0 .4px rgba(0,0,0,.25); }   /* old typewriter ink */
nav.top .links { display:flex; align-items:center; gap:.5rem; flex-wrap:wrap; }
nav.top .links a { display:inline-flex; align-items:center; gap:.4rem; padding:.45rem .8rem; border-radius:8px;
                   border:1px solid var(--muted); background:var(--quote); color:var(--fg);
                   font-size:.9rem; font-weight:600; text-decoration:none; }
nav.top .links a:hover { border-color:var(--fg); }
nav.top .links svg { width:1rem; height:1rem; fill:none; stroke:currentColor; stroke-width:2;
                     stroke-linecap:round; stroke-linejoin:round; }
form.auto { margin:0 .5rem 0 0; }
form.auto .switch { font-size:.9rem; font-weight:600; gap:.45rem; }
form.auto .switch input { width:2.2rem; height:1.3rem; }
form.auto .switch input::after { width:calc(1.3rem - 6px); height:calc(1.3rem - 6px); }
form.auto .switch input:checked::after { transform:translateX(.9rem); }
form.loop-ctl { display:inline-flex; align-items:center; gap:.45rem; margin:0 .5rem 0 0; }
form.loop-ctl button.play { display:inline-grid; place-items:center; width:2.1rem; height:2.1rem; padding:0;
                            border-radius:50%; border:1px solid var(--muted); background:var(--quote); color:var(--fg); }
form.loop-ctl button.play:hover:not(:disabled) { border-color:var(--fg); }
form.loop-ctl button.play.on { background:#237a35; border-color:#237a35; color:#fff; }
form.loop-ctl button.play:disabled { opacity:.55; cursor:default; }
form.loop-ctl button.play .ico { width:1rem; height:1rem; }
form.loop-ctl button.play .ico svg { stroke-width:2.6; }
form.loop-ctl button.play:not(.on):not(.stopping) .ico svg { fill:currentColor; }   /* a solid play triangle */
form.loop-ctl button.play.off { background:#237a35; border-color:#237a35; color:#fff; animation:go-pulse 2s ease-out infinite; }
form.loop-ctl button.play.off:hover { background:#2b8a3e; border-color:#2b8a3e; animation:none; }
@keyframes go-pulse { 0% { box-shadow:0 0 0 0 rgba(47,158,68,.6); } 70%, 100% { box-shadow:0 0 0 .55rem rgba(47,158,68,0); } }
@media (prefers-reduced-motion: reduce) { form.loop-ctl button.play.off { animation:none; } }
.status { display:inline-flex; align-items:center; gap:.4rem;
          color:var(--muted); font-size:.9rem; font-weight:600; }
.status.on { color:var(--fg); }
a.status.search-down { color:#b26b00; text-decoration:none; margin-left:.35rem; }
[data-theme="dark"] a.status.search-down { color:#e8a33d; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) a.status.search-down { color:#e8a33d; } }
.status .dot.on { animation:blink 1.4s ease-in-out infinite; }
.loop { display:flex; align-items:center; gap:.6rem; flex-wrap:wrap; padding:.8rem 1rem;
        border:1px solid var(--rule); border-radius:10px; margin-bottom:1.5rem; }
.loop form { display:flex; gap:.4rem; align-items:center; margin:0; }
.dot { width:.6rem; height:.6rem; border-radius:50%; background:var(--muted); display:inline-block; }
.dot.on { background:#2f9e44; } .dot.stopping { background:#e8a33d; }
.loop .grow { flex:1; }
.now { flex-basis:100%; color:var(--muted); font-size:.9rem; display:flex; align-items:flex-end; gap:.4rem 1rem; flex-wrap:wrap; }
.now strong, .now a { color:var(--fg); }
.now .steps { list-style:none; margin:0; padding:0; flex:1; min-width:16rem; }
.now .steps li { padding:.1rem 0; }
.now .steps li.past { font-size:.82rem; opacity:.8; }
.now .steps li.past strong { font-weight:600; }
.now .steps li.past + li.live, .now .steps li.live + li.past { margin-top:.25rem; }
.now .steps li.live + li.past { border-top:1px solid var(--rule); padding-top:.35rem; }
.now a.steps-all, a.steps-all { font-size:.82rem; color:var(--muted); white-space:nowrap; }
.out.failed { color:#c92a2a; font-weight:600; cursor:help; }
.out.stopped { color:#b26a00; font-weight:600; cursor:help; }
.out.live { color:#2f9e44; font-weight:600; }
table.steps-log .out { cursor:auto; }
table.steps-log td strong { white-space:nowrap; } table.steps-log .out.done { color:var(--muted); }
table.steps-log td.took { white-space:nowrap; font-variant-numeric:tabular-nums; }
@keyframes blink { 50% { opacity:.2; } }
h1 { font-family: Georgia, serif; }
h2.group { display:flex; align-items:center; gap:.5rem; font-size:.8rem; text-transform:uppercase; letter-spacing:.08em;
           color:var(--muted); margin:2.2rem 0 .6rem; }
h2.group .ico { width:1.05rem; height:1.05rem; color:var(--fg); }
h2.sec, .doc h2 { display:flex; align-items:center; gap:.5rem; }
h2.sec .ico, .doc h2 .ico { color:var(--muted); }
summary .ico { width:.95rem; height:.95rem; margin-right:.4rem; vertical-align:-.15rem; }
h2 .n, h3.stage .n { font-size:.72rem; font-weight:700; letter-spacing:0; padding:.05rem .45rem; border-radius:999px;
                     background:var(--quote); color:var(--muted); }
h2#needs-you .n:not([data-n="0"]) { background:#c92a2a; color:#fff; }
h3.stage { display:flex; align-items:center; gap:.45rem; font-size:.92rem; margin:1.1rem 0 .45rem; }
h3.stage .ico { color:var(--muted); }
.empty { margin:.2rem 0 0; }
.card { display:block; border:1px solid var(--rule); border-radius:10px; padding:.9rem 1rem; margin-bottom:.6rem;
        text-decoration:none; color:var(--fg); }
.card:hover { border-color:var(--muted); }
button.card.add-mission { display:flex; align-items:center; justify-content:center; gap:.55rem; width:100%;
  margin:-.6rem 0 1.5rem; padding:.7rem 1rem; font:inherit; font-size:1.05rem; font-weight:700; cursor:pointer;
  color:var(--muted); background:var(--quote); border:2px dashed var(--rule); border-radius:10px; }
button.card.add-mission:hover { color:var(--fg); border-color:var(--muted); border-style:solid; }
button.card.add-mission .ico { width:1.25rem; height:1.25rem; }
button.card.add-mission .ico:first-child svg { stroke-width:3; }
dialog.wizard { --tone:#237a35; width:min(36rem, calc(100vw - 2rem)); box-sizing:border-box; padding:1.2rem 1.4rem;
  border:1px solid var(--rule); border-radius:12px; background:var(--bg); color:var(--fg); }
dialog.wizard input:focus-visible, dialog.wizard textarea:focus-visible { outline:2px solid var(--tone); outline-offset:-1px;
  border-color:var(--tone); }
dialog.wizard button:focus-visible { outline:2px solid var(--tone); outline-offset:2px; }
dialog.wizard::backdrop { background:rgba(0,0,0,.45); }
dialog.wizard:focus { outline:none; }
dialog.wizard [hidden] { display:none; }
dialog.wizard header { display:flex; justify-content:space-between; align-items:center; }
dialog.wizard header strong { display:flex; align-items:center; gap:.45rem; font:700 1.2rem Georgia, serif; }
dialog.wizard .wiz-close { padding:.2rem .6rem; font-size:1.1rem; line-height:1; }
dialog.wizard fieldset { border:none; padding:0; margin:0; min-width:0; }
dialog.wizard legend { font-weight:700; margin-bottom:.4rem; padding:0; }
dialog.wizard input, dialog.wizard textarea { width:100%; box-sizing:border-box; }
.wiz-dots { display:flex; gap:.4rem; list-style:none; padding:0; margin:.8rem 0 1rem; }
.wiz-dots li { flex:1; height:4px; border-radius:2px; background:var(--rule); }
.wiz-dots li.on, .wiz-dots li.done { background:var(--tone); }
.wiz-nav { display:flex; justify-content:flex-end; gap:.5rem; margin:1.2rem 0 0; }
.wiz-nav .wiz-back { margin-right:auto; }
.wiz-nav .wiz-add:not([hidden]) { display:contents; }
.wiz-nav .wiz-go { display:inline-flex; align-items:center; gap:.35rem; color:var(--tone); border-color:var(--tone); }
.wiz-nav .wiz-go:hover { background:var(--tone); color:#fff; }
.wiz-nav .wiz-go .ico { width:.85rem; height:.85rem; }
.wiz-nav .wiz-go .ico svg { fill:currentColor; }
.wiz-sum { overflow-wrap:anywhere; }
.card.active, .desk.active { border-color:#2f9e44; box-shadow:inset 3px 0 0 #2f9e44; }
.card.active .sub { color:var(--fg); }
.desk.active .hint { color:var(--fg); }
.card .row { display:flex; justify-content:space-between; gap:1rem; align-items:baseline; }
.card .topic { font-weight:600; }
.card .sub { color:var(--muted); font-size:.9rem; margin-top:.25rem; }
.badge { font-size:.72rem; font-weight:600; text-transform:uppercase; letter-spacing:.05em; white-space:nowrap;
         padding:.2rem .5rem; border-radius:999px; background:var(--quote); color:var(--muted); }
.review { --tone:#1f6feb; --tint:rgba(31,111,235,.12); }
.publish { --tone:#237a35; --tint:rgba(47,158,68,.14); }
.parked { --tone:#c92a2a; --tint:rgba(201,42,42,.12); }
.card.review, .card.publish, .card.parked { border-color:var(--tone); background:var(--tint);
                                            box-shadow:inset 3px 0 0 var(--tone); }
.badge.review, .badge.publish, .badge.parked { background:var(--tone); color:#fff; }
.claim-tag { display:inline-block; font-size:.7rem; line-height:1.3; padding:0 .4rem; margin-right:.4rem; border-radius:999px;
             border:1px solid var(--rule); color:var(--muted); white-space:nowrap; vertical-align:1px; }
.claim-tag.confirmed { border-color:#2f9e44; color:#237a35; }
[data-theme="dark"] .claim-tag.confirmed { color:#69db7c; }
.claim-row { display:flex; flex-wrap:wrap; align-items:center; gap:.4rem .6rem; }
.claim-row form { display:inline-flex; gap:.4rem; align-items:center; margin:0; }
button, .btn { font:600 .9rem -apple-system, system-ui, sans-serif; padding:.5rem .9rem; border-radius:8px;
               border:1px solid var(--rule); background:var(--bg); color:var(--fg); cursor:pointer;
               text-decoration:none; display:inline-block; }
button.primary, .btn.primary { background:var(--tone, #8a2b2b); border-color:var(--tone, #8a2b2b); color:#fff; }
button:hover, .btn:hover { border-color:var(--muted); }
input, textarea { font:inherit; padding:.5rem .6rem; border:1px solid var(--rule); border-radius:8px;
                  background:var(--bg); color:var(--fg); width:100%; }
input[type=number] { width:4.5rem; }
label { display:block; font-size:.85rem; font-weight:600; margin:.8rem 0 .3rem; }
.hint { color:var(--muted); font-size:.85rem; font-weight:400; }
/* A toast over the page, so it shows wherever the page was scrolled to (KEEP_SCROLL_JS brings you back there).
   Click to dismiss; a "done" fades on its own, an error stays. */
.flash { position:fixed; top:1rem; left:50%; transform:translateX(-50%); z-index:60; box-sizing:border-box;
         width:max-content; max-width:calc(100vw - 2rem); padding:.7rem 1rem; border-radius:8px;
         background:var(--quote); border:1px solid var(--rule); box-shadow:0 6px 24px rgba(0,0,0,.18);
         cursor:pointer; animation:flash-out .4s ease 6s forwards; }
.flash.err { border-left:3px solid var(--accent); animation:none; }
@keyframes flash-out { to { opacity:0; visibility:hidden; } }
.code-big code { font-size:1.6rem; letter-spacing:.15em; padding:.3rem .6rem; }
.inline-buttons button { margin-right:.4rem; }
.actions { border:1px solid var(--rule); border-radius:10px; padding:1rem; margin:1rem 0 1.5rem; }
.setup { border-color:#2f9e44; box-shadow:inset 3px 0 0 #2f9e44; }
.setup-top { display:flex; justify-content:space-between; align-items:center; gap:1rem; }
.setup-top h2 { display:flex; align-items:center; gap:.5rem; margin:0; font-size:1.15rem; }
.setup-top form { margin:0; }
.setup-steps { list-style:none; padding:0; margin:.75rem 0 0; display:grid; gap:.6rem; }
.setup-step { border:1px solid var(--rule); border-radius:8px; padding:.7rem .85rem; }
.setup-step.done { opacity:.75; }
.setup-head { display:flex; align-items:center; gap:.55rem; }
.setup-body { margin-left:1.85rem; }
.setup-body p { margin:.35rem 0; }
.setup-form { display:flex; gap:.4rem; flex-wrap:wrap; margin:.4rem 0; }
.setup-form input { flex:1 1 14rem; min-width:0; }
.mark { width:1.3rem; height:1.3rem; border-radius:50%; display:inline-flex; align-items:center; justify-content:center;
        flex:none; border:2px solid var(--muted); }
.mark.ok { background:#2f9e44; border-color:#2f9e44; color:#fff; }
.mark svg { width:.85rem; height:.85rem; }
.chips { display:flex; flex-wrap:wrap; gap:.35rem; margin:.3rem 0; }
.chip { font:500 .82rem -apple-system, system-ui, sans-serif; padding:.3rem .65rem; border-radius:999px;
        border:1px solid var(--rule); background:var(--quote); color:var(--fg); cursor:pointer; }
.chip:hover { border-color:var(--muted); }
button.link { background:none; border:0; padding:0; color:var(--accent); cursor:pointer; font:inherit;
              display:inline-flex; align-items:center; gap:.3rem; text-decoration:underline; }
.setup-more { margin:.8rem 0 0; color:var(--muted); font-size:.9rem; }
ul.checks { list-style:none; padding:0; margin:.5rem 0; display:grid; gap:.45rem; }
ul.checks li { display:flex; gap:.55rem; align-items:flex-start; }
ol.sources { list-style:none; padding:0; margin:.6rem 0; display:grid; gap:.5rem; }
ol.sources li.src { border:1px solid var(--rule); border-radius:10px; padding:.55rem .75rem; }
ol.sources .src-head { display:flex; align-items:center; gap:.45rem; flex-wrap:wrap; }
ol.sources .src-n { color:var(--muted); font-variant-numeric:tabular-nums; min-width:1.1rem; }
ol.sources .src-tag { font-size:.75rem; color:var(--muted); border:1px solid var(--rule); border-radius:999px; padding:0 .45rem; }
ol.sources .src-state { font-size:.85rem; color:var(--muted); font-weight:600; }
ol.sources .src-state.on { color:#2f9e44; } ol.sources .src-state.rest { color:#b26b00; }
ol.sources .src-ctl { margin-left:auto; display:inline-flex; gap:.3rem; }
ol.sources .src-ctl form, ol.sources .src-key form { margin:0; }
ol.sources .src-ctl button { padding:.15rem .5rem; }
ol.sources p.hint { margin:.3rem 0; }
ol.sources .src-key { display:flex; gap:.5rem; align-items:center; flex-wrap:wrap; }
ol.sources .src-key input { width:14rem; max-width:100%; }
.actions .buttons { display:flex; gap:.5rem; flex-wrap:wrap; align-items:flex-start; }
.actions details { margin-top:.8rem; }
.rating { margin:.4rem 0 1rem; } .rating ul { list-style:none; padding:0; margin:.4rem 0 0; }
.rating li { margin:.35rem 0; } .rating .score { font-variant-numeric:tabular-nums; font-weight:600; margin-right:.3rem; }
pre.notes { white-space:pre-wrap; background:var(--quote); padding:.8rem 1rem; border-radius:8px;
            font-size:.85rem; line-height:1.5; }
.doc { font: 17px/1.6 Georgia, "Iowan Old Style", serif; }
.thesis { font-size:1.1rem; padding:.8rem 1rem; background:var(--quote); border-radius:8px; }
iframe.article { width:100%; height:75vh; border:1px solid var(--rule); border-radius:10px; background:var(--bg); }
table.events { width:100%; border-collapse:collapse; font-size:.85rem; }
table.events td { border-bottom:1px solid var(--rule); padding:.4rem .5rem .4rem 0; vertical-align:top; }
table.events td.when { color:var(--muted); white-space:nowrap; }
details summary { cursor:pointer; color:var(--muted); font-size:.9rem; }
.muted { color:var(--muted); }
.card .sub .epic { margin-right:.35rem; }
.propose { --tone:#7048e8; --tint:rgba(112,72,232,.12); }
.badge.propose { background:var(--tone); color:#fff; }
/* a decision waiting on the human: brief, article, parked mission or proposed epic all look alike */
.ask { border:1px solid var(--tone); background:var(--tint); box-shadow:inset 3px 0 0 var(--tone);
       border-radius:10px; padding:.85rem 1rem; margin:0 0 .6rem; }
.ask-head { display:flex; align-items:center; gap:.6rem; }
.ask-head .ico { width:1.2rem; height:1.2rem; color:var(--tone); }
.ask-head .topic { flex:1; font-weight:600; color:var(--fg); text-decoration:none; }
.ask-head a.topic:hover { text-decoration:underline; }
.ask .sub, .ask > .hint, .ask > ul, .ask-actions { margin-left:1.8rem; }
.ask .sub { color:var(--muted); font-size:.9rem; margin-top:.25rem; }
.ask > .hint { margin-top:.35rem; margin-bottom:0; }
.ask > ul { margin-top:.5rem; margin-bottom:0; padding-left:1.1rem; font-size:.92rem; }
.ask-actions { display:flex; flex-wrap:wrap; gap:.5rem; align-items:center; margin-top:.75rem; }
.ask-actions form { display:flex; gap:.4rem; align-items:center; margin:0; }
.ask-actions input[type=text], .ask-actions select { width:auto; min-width:9rem; }
.ask-actions details.send-back { flex-basis:100%; margin:.2rem 0 0; }
@media (max-width: 560px) { .ask .sub, .ask > .hint, .ask > ul, .ask-actions { margin-left:0; } }
.epics form.inline { margin:.3rem 0; }
.epics .epic-row { display:flex; gap:.4rem 1rem; align-items:center; flex-wrap:wrap; }
.epics .epic-row select { width:auto; min-width:9rem; }
select { font:inherit; padding:.45rem .5rem; border:1px solid var(--rule); border-radius:8px;
         background:var(--bg); color:var(--fg); }
form.inline { display:flex; gap:.4rem; align-items:center; flex-wrap:wrap; margin:.4rem 0; }
form.inline input[type=text] { width:auto; flex:1; min-width:10rem; max-width:20rem; }

@media (prefers-reduced-motion: reduce) { .status .dot.on { animation:none; } }
.card[data-lab], .card.lab-host, .desk[data-lab], .desk.lab-host { position:relative; padding-bottom:4rem;
                  background-image:linear-gradient(var(--rule), var(--rule));
                  background-size:calc(100% - 2rem) 2px; background-position:1rem calc(100% - 8px);
                  background-repeat:no-repeat; }
.card canvas.lab-stage, .desk canvas.lab-stage { bottom:0; height:160px; }   /* tall enough for a bubble above the tallest researcher */
.switch { display:flex; align-items:center; gap:.6rem; flex-wrap:wrap; margin:0; font-size:1rem; cursor:pointer; }
.switch input { appearance:none; -webkit-appearance:none; flex:none; width:2.6rem; height:1.5rem; padding:0; margin:0;
                border-radius:999px; border:1px solid var(--muted); background:var(--rule); position:relative;
                cursor:pointer; transition:background .15s, border-color .15s; }
.switch input::after { content:""; position:absolute; top:2px; left:2px; width:calc(1.5rem - 6px); height:calc(1.5rem - 6px);
                       border-radius:50%; background:#fff; box-shadow:0 1px 2px rgba(0,0,0,.35); transition:transform .15s; }
.switch input:checked { background:#237a35; border-color:#237a35; }
.switch input:checked::after { transform:translateX(1.1rem); }
.switch input:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
@media (prefers-reduced-motion: reduce) { .switch input, .switch input::after { transition:none; } }
.chat-bubble { position:fixed; right:1.25rem; bottom:1.25rem; z-index:50; width:3.5rem; height:3.5rem; padding:0;
               border-radius:50%; border:none; background:var(--chat, var(--accent)); color:var(--chat-ink, var(--bg));
               display:grid; place-items:center; box-shadow:0 4px 14px rgba(0,0,0,.25), inset 0 0 0 1px rgba(0,0,0,.08); }
.chat-bubble:hover { border:none; transform:scale(1.05); }
.chat-bubble svg { width:1.6rem; height:1.6rem; fill:none; stroke:currentColor; stroke-width:2;
                   stroke-linecap:round; stroke-linejoin:round; }
.chat-panel { position:fixed; top:0; left:0; bottom:0; z-index:40; width:max(33.333vw, 20rem); box-sizing:border-box;
              display:flex; flex-direction:column; gap:.6rem; padding:1rem; background:var(--bg);
              border-right:1px solid var(--rule); box-shadow:4px 0 18px rgba(0,0,0,.12); }
.chat-panel[hidden] { display:none; }
.chat-panel header { display:flex; justify-content:space-between; align-items:center; gap:.5rem; }
.chat-panel header strong { font:700 1.2rem Georgia, serif; white-space:nowrap; }
.chat-panel header span { display:flex; gap:.3rem; flex:none; }
.chat-panel header button { padding:.2rem .5rem; font-size:.85rem; line-height:1.3; }
.chat-panel header #chat-close { padding:.2rem .6rem; font-size:1.1rem; line-height:1; }
.chat-panel .model { display:flex; gap:.5rem; align-items:center; margin:0; }
.chat-panel .model select { flex:1; min-width:0; }
.chat-panel .hint { margin:0; }
.chat-guide { position:relative; flex:none; height:104px; margin:-1rem -1rem -.6rem; overflow:hidden; }
.chat-guide canvas.lab-stage { top:0; height:100%; }
.chat-guide + header { padding-top:0; }
.chat-log { flex:1; overflow-y:auto; display:flex; flex-direction:column; gap:.6rem; padding:.2rem 0; }
.chat-log .msg { max-width:88%; padding:.6rem .9rem; border-radius:12px; white-space:pre-wrap; line-height:1.5; }
.chat-log .user { align-self:flex-end; background:var(--chat, var(--accent)); color:var(--chat-ink, var(--bg)); }
.chat-log .user .md a { color:inherit; }
.chat-form button.primary { background:var(--chat, var(--accent)); border-color:var(--chat, var(--accent));
                            color:var(--chat-ink, var(--bg)); }
.chat-log .assistant { align-self:flex-start; background:var(--quote); }
.chat-log .assistant.wait { color:var(--muted); font-style:italic; }
.chat-log .md { white-space:normal; }
.chat-log .md > :first-child { margin-top:0; }
.chat-log .md > :last-child { margin-bottom:0; }
.chat-log .md p, .chat-log .md ul, .chat-log .md ol { margin:.45rem 0; }
.chat-log .md ul, .chat-log .md ol { padding-left:1.3rem; }
.chat-log .md li + li { margin-top:.2rem; }
.chat-log .md code { font-size:.88em; padding:0 .25em; border-radius:4px; background:var(--bg); }
.chat-log .md a { color:var(--accent); }
.chat-log .model-tag { display:block; margin-top:.3rem; font-size:.75rem; color:var(--muted); }
.suggest { display:flex; gap:.4rem; flex-wrap:wrap; }
.suggest[hidden] { display:none; }
.suggest button { font-size:.82rem; padding:.3rem .7rem; border-radius:999px; }
.chat-form { display:flex; gap:.5rem; align-items:flex-end; }
.chat-form textarea { resize:none; min-height:2.6rem; }
@media (min-width: 761px) { body.chat-open main { margin-left:calc(max(33.333vw, 20rem) + 1.5rem); } }
@media (max-width: 760px) { .chat-panel { width:100vw; }
                            body.chat-open .chat-bubble { display:none; } }
.workers { display:grid; grid-template-columns:repeat(auto-fit, minmax(16rem, 1fr)); gap:1rem; margin:1rem 0 1.5rem; }
.worker { border:1px solid var(--rule); border-radius:10px; padding:1rem 1rem 0; margin:0; display:flex;
          flex-direction:column; overflow:hidden; }
.worker.off { border-style:dashed; }
.worker.off .crew { opacity:.45; filter:grayscale(1); }
.worker .head { display:flex; justify-content:space-between; align-items:center; gap:.6rem; }
.worker h3 { margin:0; font-family:Georgia, serif; font-size:1.15rem; }
.worker .tagline { margin:.3rem 0 0; }
.worker select { width:100%; }
.worker ul { margin:.3rem 0 .6rem; padding-left:1.1rem; font-size:.9rem; }
.worker ul li { margin:.15rem 0; }
.worker .note { font-size:.85rem; margin:.2rem 0 .6rem; }
.worker .grow { flex:1; }
.stepper { display:flex; align-items:center; flex-wrap:wrap; gap:.3rem .7rem; }
.stepper .steps { display:inline-flex; align-items:stretch; border:1px solid var(--rule); border-radius:8px;
                  overflow:hidden; background:var(--bg); }
.stepper .steps:focus-within { border-color:var(--muted); }
.stepper button { border:0; border-radius:0; padding:.3rem .8rem; font-size:1.1rem; line-height:1; min-width:2.4rem; }
.stepper button:hover:not(:disabled) { background:var(--quote); }
.stepper button:disabled { color:var(--muted); opacity:.45; cursor:default; }
.stepper input[type=number] { width:3.2rem; padding:.3rem 0; border:0; border-radius:0; text-align:center;
                              font-weight:700; border-left:1px solid var(--rule); border-right:1px solid var(--rule);
                              -moz-appearance:textfield; appearance:textfield; }
.stepper input::-webkit-inner-spin-button, .stepper input::-webkit-outer-spin-button { -webkit-appearance:none; margin:0; }
.stepper input:focus-visible { outline:2px solid var(--accent); outline-offset:-2px; }
.stepper .hint { font-size:.85rem; }
.crew { position:relative; height:116px; margin:0 -1rem; border-top:1px solid var(--rule);
        background:linear-gradient(var(--quote), var(--quote)) bottom / 100% 9px no-repeat; }
.crew canvas.lab-stage { bottom:0; height:116px; }
.crew-wrap { position:relative; }
.crew-edit { position:absolute; top:.45rem; right:.2rem; display:inline-flex; align-items:center; gap:.25rem; font-size:.75rem;
             color:var(--muted); text-decoration:none; opacity:.75; }
.crew-edit:hover { color:var(--accent); opacity:1; }
.sr { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap; }
.who { font-size:.75rem; font-weight:700; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); }
table.models td.size { white-space:nowrap; color:var(--muted); }
a.back { font-weight:600; text-decoration:none; }
.card.site-link { margin-top:1.5rem; border-style:dashed; }
.card.site-link .topic { display:inline-flex; align-items:center; gap:.4rem; }
.card.site-link svg, .card.site-link .topic svg { width:1rem; height:1rem; fill:none; stroke:currentColor; stroke-width:2;
                       stroke-linecap:round; stroke-linejoin:round; }
.card.pub:hover { border-color:var(--rule); }
.card.pub.publish:hover { border-color:var(--tone); }
.card.pub .topic { color:var(--fg); }
ul.places { margin:.5rem 0; padding-left:1.1rem; font-size:.9rem; }
ul.places li { margin:.2rem 0; overflow-wrap:anywhere; }
ul.outreach { margin:.5rem 0; padding-left:1.1rem; } ul.outreach li { margin:.6rem 0; overflow-wrap:anywhere; }
ul.outreach .sent { margin:.25rem 0; font-size:.9rem; } ul.outreach blockquote.reply { margin:.3rem 0 .3rem .4rem; }
fieldset.channels { border:1px solid var(--rule); border-radius:8px; margin:.8rem 0; padding:.4rem .8rem .6rem; }
fieldset.channels legend { font-size:.85rem; font-weight:600; padding:0 .3rem; }
label.tick.out-channel { display:flex; align-items:flex-start; margin:.45rem 0; font-weight:400; }
label.tick.out-channel input { margin-top:.2rem; }
li.link-row form.inline { display:inline-flex; margin:0 0 0 .4rem; vertical-align:middle; }
p.objective { white-space:pre-line; }
form.republish { display:flex; flex-wrap:wrap; gap:.3rem 1rem; align-items:center; margin:.6rem 0 0;
                 padding-top:.6rem; border-top:1px solid var(--rule); }
form.republish button { margin-left:auto; }
label.tick { display:inline-flex; align-items:center; gap:.35rem; margin:0; font-weight:600; cursor:pointer; }
label.tick input { width:auto; margin:0; }
label.tick .hint { margin-left:-.15rem; }
.pubbox { border:1px solid var(--rule); border-radius:10px; padding:.9rem 1rem; margin:.6rem 0; }
.pubbox .head { display:flex; justify-content:space-between; align-items:center; gap:.6rem; flex-wrap:wrap; }
.pubbox h3 { margin:0; font-family:Georgia, serif; font-size:1.15rem; }
.pubbox p:last-child, .pubbox form:last-child p:last-child { margin-bottom:0; }
.pubbox form button { margin-top:.2rem; }
.pubbox.add { border-style:dashed; }
.pubbox.add > summary { color:var(--fg); font-size:1rem; }
.pubbox details.formats { margin:.8rem 0; }
h3.sub { font-size:.95rem; margin:1.4rem 0 .3rem; }
/* the Settings page: its sections down the right, a nav down the left that lights the one in view */
main:has(.settings) { max-width: 72rem; }
.settings { display:grid; grid-template-columns:12rem minmax(0, 1fr); gap:2.5rem; align-items:start; }
.set-nav { position:sticky; top:1.25rem; display:flex; flex-direction:column; gap:.15rem; }
.set-nav a { display:flex; align-items:center; gap:.6rem; padding:.5rem .75rem; border-radius:8px; color:var(--muted);
             font-size:.92rem; font-weight:600; text-decoration:none; white-space:nowrap;
             transition:background .15s, color .15s, box-shadow .15s; }
.set-nav a:hover { color:var(--fg); background:var(--quote); }
.set-nav a.on { color:var(--fg); background:var(--quote); box-shadow:inset 3px 0 0 var(--accent); }
.set-nav a.on .ico { color:var(--accent); }
.set-nav .ico { width:1.05rem; height:1.05rem; }
.set { scroll-margin-top:1.25rem; padding-bottom:1rem; margin-bottom:2.5rem; border-bottom:1px solid var(--rule); }
.set:last-child { border-bottom:0; margin-bottom:0; }
.set > h2 { display:flex; align-items:center; gap:.6rem; font-family:Georgia, serif; font-size:1.5rem; margin:0 0 .9rem; }
.set > h2 .ico { width:1.3rem; height:1.3rem; color:var(--accent); }
.set h3.part { display:flex; align-items:center; gap:.5rem; font-size:.8rem; text-transform:uppercase;
               letter-spacing:.08em; color:var(--muted); margin:2rem 0 .6rem; scroll-margin-top:1.25rem; }
.set h3.part .ico { width:1.05rem; height:1.05rem; color:var(--fg); }
.mcp .inline input[readonly] { flex:1 1 16rem; min-width:0; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.85rem; }
.mcp pre.mcp-snippet { white-space:pre-wrap; word-break:break-all; font-size:.8rem; background:var(--quote);
  padding:.6rem .75rem; border-radius:8px; margin:.5rem 0 .3rem; max-width:100%; overflow-x:auto; }
.mcp ul.mcp-tools { padding-left:1.1rem; margin:.4rem 0; display:grid; gap:.3rem; }
.mcp .warn { color:var(--accent); }
.mcp p .ico { width:1rem; height:1rem; vertical-align:-.15rem; margin-right:.35rem; }
.set > h2 + h3.part { margin-top:.4rem; }
.set > h2 + .actions, .set > h2 + form.actions { margin-top:0; }
.set .actions[id], .set form[id] { scroll-margin-top:1.25rem; }
@media (max-width: 820px) {
  .settings { display:block; }       /* not a grid row, so the bar sticks the whole way down */
  .set-nav { top:0; z-index:5; flex-direction:row; overflow-x:auto; scrollbar-width:none; gap:.25rem;
             margin:0 -1rem 1rem; padding:.5rem 1rem; background:var(--bg); border-bottom:1px solid var(--rule); }
  .set-nav::-webkit-scrollbar { display:none; }
  .set-nav a { padding:.4rem .7rem; }
  .set-nav a.on { box-shadow:inset 0 -3px 0 var(--accent); }
  .set, .set h3.part, .set .actions[id], .set form[id] { scroll-margin-top:4rem; }
}
/* Display: the theme as a segmented choice, the chat colour as ink swatches */
:root:not([data-theme]) { color-scheme: light dark; }
.display h3 { display:flex; align-items:center; gap:.45rem; margin:0 0 .2rem; font-family:Georgia, serif; font-size:1.1rem; }
.display h3 .ico { color:var(--muted); }
.display .hint { margin:0 0 .7rem; }
.display + .display { margin-top:-.5rem; }
.choice { display:inline-flex; border:1px solid var(--rule); border-radius:999px; padding:3px; background:var(--quote);
          margin:0; gap:2px; flex-wrap:wrap; }
.choice label { position:relative; display:inline-flex; align-items:center; gap:.4rem; margin:0; padding:.4rem .95rem; border-radius:999px;
                font-size:.9rem; font-weight:600; color:var(--muted); cursor:pointer; }
.choice label:hover { color:var(--fg); }
.choice input { position:absolute; opacity:0; width:1px; height:1px; pointer-events:none; }
.choice label:has(input:checked) { background:var(--bg); color:var(--fg); box-shadow:0 1px 2px rgba(0,0,0,.15), 0 0 0 1px var(--rule); }
.choice label:has(input:focus-visible) { outline:2px solid var(--accent); outline-offset:1px; }
.swatches { display:flex; flex-wrap:wrap; gap:.9rem 1.1rem; margin:0; padding:0; border:0; }
.swatch { position:relative; display:flex; flex-direction:column; align-items:center; gap:.35rem; width:4.6rem; margin:0; cursor:pointer;
          font-size:.78rem; font-weight:600; color:var(--muted); text-align:center; line-height:1.2; }
.swatch input { position:absolute; opacity:0; width:1px; height:1px; pointer-events:none; }
.swatch .dab { position:relative; width:2.4rem; height:2.4rem; border-radius:50%; background:var(--c);
               box-shadow:inset 0 0 0 1px rgba(0,0,0,.12); transition:transform .12s; }
.swatch:hover .dab { transform:scale(1.07); }
.swatch:has(input:checked) { color:var(--fg); }
.swatch:has(input:checked) .dab { box-shadow:inset 0 0 0 1px rgba(0,0,0,.12), 0 0 0 2px var(--bg), 0 0 0 4px var(--fg); }
.swatch:has(input:checked) .dab::after { content:""; position:absolute; left:50%; top:46%; width:.5rem; height:.9rem;
               border:solid var(--ink, #1d1d1f); border-width:0 2px 2px 0; transform:translate(-50%, -50%) rotate(45deg); }
.swatch:has(input:focus-visible) .dab { outline:2px solid var(--accent); outline-offset:5px; }
.swatch.own .dab { background:conic-gradient(#eec5c5, #f3d0b2, #efe2b2, #b9cdb2, #b8d8d6, #bcd0ea, #cbc0e3, #eec5c5); }
.swatch.own.set .dab { background:var(--c); }
.swatch.own input[type=color] { position:absolute; inset:0; width:100%; height:100%; opacity:0; cursor:pointer;
                                pointer-events:auto; padding:0; border:0; }
@media (prefers-reduced-motion: reduce) { .swatch .dab { transition:none; } }
"""

ELAPSED_JS = """<script>
const showElapsed = () => {
  document.querySelectorAll("[data-since]").forEach(e => {
    const s = Math.max(0, Math.floor(Date.now() / 1000 - Number(e.dataset.since)));
    e.textContent = s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
  });
  document.querySelectorAll("[data-ago]").forEach(e => {      // ui.ago, kept current; older dates stay as written
    const s = Date.now() / 1000 - Number(e.dataset.ago);
    const t = (k, n) => ((window.I18N || {})[k] || k).replace("{n}", n);
    if (s < 86400) e.textContent = s < 60 ? t("just now") : s < 3600 ? t("{n}m ago", Math.floor(s / 60)) : t("{n}h ago", Math.floor(s / 3600));
  });
};
setInterval(showElapsed, 1000);
</script>"""

# Every button posts a form and the server redirects back (POST, then 303), which the browser loads as a new page,
# scrolled to the top. Remember where the page was when a form goes off, and scroll back there if it lands on the
# same page. Then take done=/error= out of the address, so the page's own refreshes and a reload don't show the
# flash again.
KEEP_SCROLL_JS = """<script>
(() => {
  const KEY = "scroll-back";
  document.addEventListener("submit", e => {
    if (e.defaultPrevented) return;          // a form handled in the page (the chat) doesn't leave it
    try { sessionStorage.setItem(KEY, JSON.stringify({path: location.pathname, y: scrollY, t: Date.now()})); }
    catch (err) {}
  });
  let saved = null;
  try { saved = JSON.parse(sessionStorage.getItem(KEY)); sessionStorage.removeItem(KEY); } catch (err) {}
  if (saved && saved.path === location.pathname && Date.now() - saved.t < 60000) {
    const back = () => scrollTo(0, saved.y);
    back();
    addEventListener("load", back, {once: true});   // again once the strip has sized itself
  }
  const url = new URL(location.href);
  if (url.searchParams.has("done") || url.searchParams.has("error")) {
    url.searchParams.delete("done"); url.searchParams.delete("error");
    history.replaceState(history.state, "", url.pathname + url.search + url.hash);
  }
  document.addEventListener("click", e => { const f = e.target.closest(".flash"); if (f) f.remove(); });
})();
</script>"""

# Little pixel researchers who wander in-progress cards and the working agent's pill (assets/researchers.js).
ASSETS = Path(__file__).parent / "assets"
TOOLS = Path(__file__).parent / "tools"
_stamps: dict = {}

def asset(name: str) -> str:
    """A script's address with a stamp of its contents (`/assets/researchers.js?v=1a2b3c4d`), so the address changes
    whenever the file does and no browser can go on using a copy from before an update."""
    path = ASSETS / name
    try:
        stat = path.stat()
        seen = _stamps.get(name)
        if not seen or seen[0] != (stat.st_mtime_ns, stat.st_size):
            seen = _stamps[name] = ((stat.st_mtime_ns, stat.st_size), hashlib.sha1(path.read_bytes()).hexdigest()[:8])
        return f"/assets/{name}?v={seen[1]}"
    except OSError:
        return f"/assets/{name}"

def script(name: str) -> str:
    return f'<script src="{asset(name)}" defer></script>'

STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')

def script_strings(name: str) -> list[str]:
    """Every string literal inside a page script's tr(...) calls (assets/board.js, assets/avatars.js), ternaries
    included, in the order written."""
    code, out, i = (ASSETS / name).read_text(), [], 0
    while (i := code.find("tr(", i)) != -1:
        if i and (code[i - 1].isalnum() or code[i - 1] in "_$."):
            i += 3
            continue
        depth, j = 0, i + 2
        while j < len(code):               # to the matching ")", stepping over strings
            c = code[j]
            if c == '"':
                j = code.index('"', j + 1) if code[j + 1] != '"' else j + 1
                while code[j - 1] == "\\" and code[j - 2] != "\\":
                    j = code.index('"', j + 1)
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if not depth:
                    break
            j += 1
        out += [json.loads(f'"{m}"') for m in STRING.findall(code[i + 3:j])]
        i = j
    return list(dict.fromkeys(x for x in out if x.strip()))

def script_i18n(name: str) -> str:
    """The translations one page script needs, added to window.I18N before it runs (English needs none)."""
    if i18n.current() == "en":
        return ""
    table = json.dumps({s: _(s) for s in script_strings(name)}, ensure_ascii=False).replace("</", "<\\/")
    return f"<script>window.I18N = Object.assign(window.I18N || {{}}, {table});</script>"

def lines_script() -> str:
    """The researchers' lines in the UI's language (none for English), loaded before researchers.js."""
    code = i18n.current()
    name = f"lines/{code}.js"
    return f'<script src="{asset(name)}"></script>' if code != "en" and (ASSETS / name).exists() else ""

class FreshFiles(StaticFiles):
    """Static files the browser checks before every use. Without this a browser guesses how long a script stays
    good from its age, so after an update it can keep drawing with the old researchers for hours. With it, an
    unchanged file costs one short "not modified" answer and a changed one is fetched at once."""
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response
ICON_MISSIONS = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 21V4"/><path d="M5 4c4-2.5 7 2.5 14 0v10c-7 2.5-10-2.5-14 0"/></svg>'
ICON_SITE = ('<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18"/>'
             '<path d="M12 3c3 3 3.6 6 3.6 9s-.6 6-3.6 9c-3-3-3.6-6-3.6-9s.6-6 3.6-9z"/></svg>')

ICON_SETTINGS = ('<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/>'
                 '<path d="M9.8 4.9 10.4 2.1 13.6 2.1 14.2 4.9 15.5 5.5 17.9 3.9 20.1 6.1 18.5 8.5 19.1 9.8 21.9 10.4 '
                 '21.9 13.6 19.1 14.2 18.5 15.5 20.1 17.9 17.9 20.1 15.5 18.5 14.2 19.1 13.6 21.9 10.4 21.9 9.8 19.1 '
                 '8.5 18.5 6.1 20.1 3.9 17.9 5.5 15.5 4.9 14.2 2.1 13.6 2.1 10.4 4.9 9.8 5.5 8.5 3.9 6.1 6.1 3.9 8.5 5.5z"/></svg>')

ICON_BOARD = f'<svg viewBox="0 0 24 24" aria-hidden="true">{ICON_PATHS["board"]}</svg>'   # one drawing, in icons.py

ICON_CHAT = ('<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v11H9l-5 4z"/></svg>')

def part(name: str, title: str, anchor: str = "") -> str:
    """A part of one of the Settings page's sections, e.g. Library under Research."""
    a = f' id="{anchor}"' if anchor else ""
    return f'<h3 class="part"{a}>{icon(name)}<span>{escape(title)}</span></h3>'

def heading(name: str, title: str, count: int | None = None, anchor: str = "", cls: str = "group") -> str:
    """A section heading with its icon, e.g. the finger for anything waiting on you."""
    n = f'<span class="n" data-n="{count}">{count}</span>' if count is not None else ""
    a = f' id="{anchor}"' if anchor else ""
    return f'<h2 class="{cls}"{a}>{icon(name)}<span>{escape(title)}</span>{n}</h2>'

def loop_button() -> str:
    """Play/pause for the loop, in the top bar on every page beside the Auto switch, with the loop's state. It only
    says Running while the loop really holds the lock. Play starts it (resting settings.loop_every minutes when it
    has nothing to do); pause asks it to stop after the current agent saves its work."""
    every = settings.loop_every()
    if not loop_running():
        action, ico, text, dot = "start", "play", _("Loop off"), ""
        title = _("Start the loop")
    elif orchestrator.STOP_FLAG.exists():
        action, ico, text, dot = "", "pause", _("Stopping"), "stopping"
        title = _("Stopping after the current task")
    elif waiting := doctor.waiting():
        action, ico, text, dot = "stop", "pause", _("Waiting"), "stopping"
        title = _("Waiting: {reason}. Checks again every 30 s. Pause to stop.").format(reason=waiting["reason"])
    else:
        action, ico, text, dot = "stop", "pause", _("Running"), "on"
        title = _("Pause the loop")
    button = (f'<button class="play {dot or "off"}" title="{escape(title)}" aria-label="{escape(title)}"'
              f' onclick="this.form.back.value = location.pathname"{"" if action else " disabled"}>{icon(ico)}</button>')
    return (f'<form class="loop-ctl" method="post" action="/loop/{action or "stop"}">'
            f'<input type="hidden" name="back" value="/"><input type="hidden" name="every" value="{every:g}">'
            f'{button}'
            f'<span class="status {dot}"><span class="dot {dot}" aria-hidden="true"></span>{text}</span>'
            f'{search_down_note()}</form>')

def search_down_note() -> str:
    """A small amber "Search down" beside the loop's state while web search fails, the reason on hover and a link
    to Settings → Setup. The one place outside Setup that shows it."""
    down = search_status.current()
    if not down:
        return ""
    title = _("Web search is down: {reason}.").format(reason=down["reason"]) + " " + _(down.get("fix") or "")
    return (f'<a class="status search-down" href="/settings#setup" title="{escape(title.strip(), quote=True)}">'
            f'<span class="dot stopping" aria-hidden="true"></span>{_("Search down")}</a>')

def auto_switch() -> str:
    """Auto mode, in the top bar on every page: approves every brief and proposed epic, never publishing."""
    on = settings.auto_mode()
    title = _("Auto-approve briefs and new epics. Articles still need you to publish.")
    return (f'<form class="auto" method="post" action="/auto"><input type="hidden" name="back" value="/">'
            f'<label class="switch" title="{escape(title)}"><input type="checkbox" role="switch" name="auto" value="on"'
            f'{" checked" if on else ""} onchange="this.form.back.value = location.pathname; this.form.submit()">'
            f'<span>{_("Auto")}</span></label></form>')

def html_attrs() -> str:
    """The theme and chat colour from the Settings page, on <html> so they apply before first paint. "system"
    sets no data-theme, so the stylesheet's prefers-color-scheme rules follow the computer, live."""
    theme = settings.theme()
    out = f' data-theme="{theme}"' if theme != "system" else ""
    if color := settings.chat_color():
        out += f' style="--chat:{color};--chat-ink:{settings.ink_on(color)}"'
    return out

def page(title: str, body: str, poll: bool = False) -> HTMLResponse:
    # Refresh itself when the loop changes something, unless the human is typing.
    script = f"""<script>
let sig = "{signature()}";
async function check() {{
  const busy = [...document.querySelectorAll("textarea:not(#chat-q), input[type=text]")].some(e => e.value.trim() && e.value !== e.defaultValue)
            ||["TEXTAREA", "INPUT"].includes(document.activeElement.tagName)
            || document.body.classList.contains("chat-open") || !!document.querySelector("dialog[open]");
  if (busy || document.hidden) return;
  try {{ const s = await (await fetch("/api/signature")).text(); if (s !== sig && await refresh()) sig = s; }} catch (e) {{}}
}}
setInterval(check, 5000);
window.checkPage = check;              // the strip calls this the moment its step changes
// Swap in the fresh page without reloading, keeping the live pipeline strip (and anyone walking on it).
async function refresh() {{
  const res = await fetch(location.href);
  if (!res.ok || res.redirected) {{ location.reload(); return false; }}
  const doc = new DOMParser().parseFromString(await res.text(), "text/html");
  const fresh = doc.querySelector("main"), main = document.querySelector("main");
  if (!fresh || !main) {{ location.reload(); return false; }}
  const open = new Set([...main.querySelectorAll("details[open] > summary")].map(e => e.textContent));
  const next = document.importNode(fresh, true);
  const keep = document.getElementById("flow"), slot = next.querySelector("#flow");
  if (keep && slot && keep.dataset.shape === slot.dataset.shape) slot.replaceWith(keep);
  next.querySelectorAll("details > summary").forEach(e => {{ if (open.has(e.textContent)) e.parentNode.open = true; }});
  next.prepend(...main.querySelectorAll(".flash.err"));   // an error stays until clicked away
  main.replaceChildren(...next.childNodes);
  for (const sel of ["nav.top form.loop-ctl", "nav.top form.auto"]) {{
    const fresh = doc.querySelector(sel), shown = document.querySelector(sel);
    if (fresh && shown) shown.replaceWith(document.importNode(fresh, true));
  }}
  document.title = doc.title;
  showElapsed();
  if (window.Researchers) window.Researchers.rescan();
  if (window.Flow) window.Flow.attach();
  return true;
}}
</script>{ELAPSED_JS}""" if poll else ""
    html = (f'<!doctype html><html lang="{i18n.current()}"{html_attrs()}><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{escape(title)}</title>{avatars.script_tag()}<style>{CSS}{ICON_CSS}{flowchart.CSS}{UI_CSS}</style></head><body><main>"
            f'<nav class="top"><a class="brand" href="/">Mission Research Loop</a>'
            f'<span class="links">{auto_switch()}{loop_button()}<a href="/">{ICON_MISSIONS}{_("Missions")}</a>'
            f'<a href="/published">{ICON_SITE}{_("Published")}</a>'
            f'<a href="/board">{ICON_BOARD}{_("Board")}</a>'
            f'<a href="/settings">{ICON_SETTINGS}{_("Settings")}</a>'
            f'<a href="/about" target="_blank" title="{escape(_("How it works, on the published site"), quote=True)}">{icon("book")}{_("About")}</a></span></nav>'
            f"{body}</main>{js_strings()}{lines_script()}{chat_widget()}{KEEP_SCROLL_JS}{BOARD_LINK_JS}{script}</body></html>")
    return HTMLResponse(html)

JS_STRINGS = ["(default)", "Web search off. Switch a source on in Settings → Web search.", "Looking…",
              "No answer. Is the UI still running?", "{n} parked here", "Answer lost when the UI restarted. Ask again.",
              "You", "Researcher Chat", "Copy", "Nothing to copy", "Copied", "Copy failed", "Show", "Hide", "Press Ctrl/Cmd-C",
              "just now", "{n}m ago", "{n}h ago"]

def js_strings() -> str:
    """The few strings the page's scripts show, in the UI's language (window.I18N; English needs none)."""
    if i18n.current() == "en":
        return ""
    table = {s: _(s) for s in JS_STRINGS}
    return f"<script>window.I18N = Object.assign(window.I18N || {{}}, {json.dumps(table, ensure_ascii=False).replace('</', '<\\/')});</script>"

def flash(request: Request) -> str:
    done, err = request.query_params.get("done"), request.query_params.get("error")
    if err == "notready":
        why = (doctor.waiting() or {}).get("reason", "")
        return f'<div class="flash err">{escape(_(ERRORS[err]))}{": " + escape(why) if why else ""}.</div>'
    if err in ERRORS:
        text = _(ERRORS[err])
        for key, value in (("{max}", settings.MAX_COUNT), ("{name}", settings.NAME_MAX),
                           ("{tagline}", settings.TAGLINE_MAX), ("{about}", settings.ABOUT_MAX)):
            text = text.replace(key, str(value))
        return f'<div class="flash err">{escape(text)}</div>'
    if done in MESSAGES:
        return f'<div class="flash">{escape(_(MESSAGES[done]))}</div>'
    return ""

def strip_step(agent: str) -> str:
    """Where an agent sits on the strip: posting to a channel is part of the one Publish step."""
    return "publisher" if agent in ("1f916", "github", "bluesky") else agent

def doing(e: dict) -> str:
    """What one working agent is doing, e.g. "Source finder · <topic> · 2m 3s"."""
    since = f' · <span data-since="{float(e["since"])}">{elapsed(e["since"])}</span>'
    if not e["slug"]:                      # the Theorist and the mission finder work for the desk, not one mission
        task = _("all missions") if e["agent"] == "theorist" else _("new missions")
        return f'<strong>{agent_name(e["agent"])}</strong> · {task}{since}'
    m = store.get_mission(e["slug"])
    return (f'<strong>{agent_name(e["agent"])}</strong> · '
            f'<a href="/m/{quote(e["slug"])}">{escape(m.topic if m else e["slug"])}</a>{since}')

def took(seconds: float) -> str:
    """How long a step took: "0.4s", "12s", "4m 12s", "1h 3m"."""
    seconds = max(0.0, float(seconds))
    if seconds < 10:
        return f"{seconds:.1f}s"
    s = int(seconds)
    return f"{s // 3600}h {s % 3600 // 60}m" if s >= 3600 else f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"

def ago(at: float) -> str:
    """When a step ended, from now: "just now", "5m ago", "2h ago", else the date."""
    s = time.time() - float(at)
    if s < 60:
        return _("just now")
    if s < 3600:
        return _("{n}m ago").format(n=int(s // 60))
    if s < 86400:
        return _("{n}h ago").format(n=int(s // 3600))
    return datetime.fromtimestamp(float(at)).strftime("%Y-%m-%d %H:%M")

PAST = {"theorist": "all missions", "finder": "new missions"}     # what a desk-wide agent worked on, as doing() says it
OUTCOME = {"failed": "failed", "stopped": "stopped"}

def step_task(e: dict, topics: dict[str, str]) -> str:
    """What a finished step worked on: its mission (a link), or all missions / new missions for a desk-wide agent."""
    if not e.get("slug"):
        return _(PAST.get(e["agent"], "all missions"))
    return f'<a href="/m/{quote(e["slug"])}">{escape(topics.get(e["slug"], e["slug"]))}</a>'

def step_outcome(e: dict) -> str:
    kind = e.get("outcome", "done")
    if kind not in OUTCOME:
        return ""
    why = f' title="{escape(e.get("why", ""), quote=True)}"' if e.get("why") else ""
    return f' · <span class="out {kind}"{why}>{_(OUTCOME[kind])}</span>'

def past_step(e: dict, topics: dict[str, str]) -> str:
    """One finished step: "Source finder · <topic> · took 4m 12s · 3m ago"."""
    return (f'<strong>{agent_name(e["agent"])}</strong> · {step_task(e, topics)}'
            + ' · ' + _("took {time}").format(time=took(e["ended"] - e["since"])) + step_outcome(e) +
            f' · <span class="ago" data-ago="{float(e["ended"])}">{ago(e["ended"])}</span>')

RECENT_STEPS = 3                           # finished steps shown under the strip

def flow_status(now: list[dict], running: bool, topics: dict[str, str] | None = None) -> str:
    """The brief log under the strip: who is working on what and for how long (with more than one worker busy,
    one line each, named), then the last few finished steps, with how long each took, and a link to the full log
    (activity.LOG, on /log)."""
    lines = [f'<li class="live">' + (f'<span class="who">{seat_name(e["worker"])}</span> '
             if seat_name(e["worker"]) and len(now) > 1 else "") + doing(e) + "</li>" for e in now]
    if not lines:
        idle = _("Idle") if running else _("Loop off")
        lines.append(f'<li class="live idle">{idle}</li>')
    if topics is None:
        topics = {m.slug: m.topic for m in store.all_missions()}
    lines += [f'<li class="past">{past_step(e, topics)}</li>' for e in activity.recent(RECENT_STEPS)]
    return (f'<ul class="steps">{"".join(lines)}</ul>'
            f'<a class="steps-all" href="/log">' + _("Full log") + ' &rarr;</a>')

def flow_state() -> dict:
    """What the strip shows: the steps working right now (one per busy worker, while the loop runs), how
    many missions wait at each human review, and how many each check has parked. /api/flow serves it to
    the page every few seconds, with every mission's state, so the page can tell where a mission went
    when a worker's step ends."""
    running = loop_running()
    shown = {k for k, _, _ in flow_steps()}
    now = [e for e in running_now() if strip_step(e["agent"]) in shown]
    missions = store.all_missions()
    parked = [m for m in missions if m.state == State.PARKED]
    writing = [m for m in parked if orchestrator.parked_while_writing(m)]
    rated = sum(bool(m.rejected_article and m.rejected_article.rating) for m in writing)
    return {
        "running": running, "stopping": running and orchestrator.STOP_FLAG.exists(),
        "working": [{"worker": e["worker"], "step": strip_step(e["agent"]), "slug": e["slug"], "since": float(e["since"]),
                     "name": agent_name(e["agent"])} for e in now],
        "waiting": {"epic_review": len(store.all_epics("proposed")) + len(store.all_combines("proposed"))
                                   + len(store.all_dead_leads("proposed")),
                    "brief_review": sum(m.state == State.BRIEF_READY and not m.approved for m in missions),
                    "article_review": sum(m.state == State.ARTICLE_READY and not m.reviewed for m in missions)},
        "parked": {"source_check": len(parked) - len(writing), "article_check": len(writing) - rated,
                   "rating_check": rated},
        "states": {m.slug: m.state.value for m in missions},
        "now": flow_status(now, running, {m.slug: m.topic for m in missions}),
    }

def flow_strip(state: dict) -> str:
    """The pipeline strip, laid out as a snake: lane 0 runs left to right from the top left (the Theorist) to the
    first human review, a square end on the right; lane 1 comes back right to left one level lower to the next
    review on the left, and so on. Each review spans the two lanes it joins, so the snake turns through it. It is
    always drawn; with the loop off nothing on it is lit. assets/flow.js keeps it current without reloading the
    page (and stacks it on a narrow screen), and the pixel researchers stand on its tiles."""
    auto = settings.auto_mode()
    steps, lit = flow_steps(auto), {e["step"]: settings.seat_worker(e["worker"]) for e in state["working"]}
    track = flowchart.track(lit, state["waiting"], state["parked"], review_href="/#needs-you", auto=auto)
    lab = " data-lab" if settings.animations() else ""
    working = json.dumps([{k: e[k] for k in ("worker", "step", "slug")} for e in state["working"]])
    attrs = f'data-shape="{",".join(k + ("*" if kind == "auto" else "") for k, kind, _ in steps)}" ' \
            f'data-working="{escape(working, quote=True)}"'
    off = "" if state["running"] else " off"
    return (f'<section id="flow" class="flow{off}" {attrs} aria-label="{_("Pipeline")}">'
            f'<div class="flow-scroll"><div class="flow-track"{lab}>{track}</div></div>'
            f'<div class="now" aria-live="polite">{state["now"]}</div></section>')

def loop_panel(back: str) -> str:
    running, stopping = loop_running(), orchestrator.STOP_FLAG.exists()
    if running and stopping:
        state, dot = _("Stopping after the current task"), "stopping"
        controls = ""
    elif running and (waiting := doctor.waiting()):
        state = _("Loop waiting: {reason}. Checks again every 30 s.").format(reason=escape(waiting["reason"]))
        state, dot = f'{state} <a href="/settings#setup">{_("Settings → Setup")}</a>', "stopping"
        controls = ""
    elif running:
        state, dot = _("Loop running"), "on"
        controls = (f'<form method="post" action="/loop/wake"><input type="hidden" name="back" value="{back}">'
                    f'<button>{_("Check for work now")}</button></form>')
    else:
        state, dot = _("Loop off. Press play at the top to start."), ""
        controls = ""
    return (f'<div class="loop"><span class="dot {dot}"></span><span class="grow">{state}</span>{controls}'
            f'{flow_strip(flow_state())}</div>{script("flow.js")}')   # flow.js keeps the pipeline strip live

DESK_AGENTS = ("theorist", "finder")       # work for the whole desk (slug ""), so the Inspiration box is their card

def desk_live(now: list[dict] | None) -> dict | None:
    """The Theorist's or the mission finder's step running now, if either is."""
    return next((e for e in now or [] if not e["slug"] and e["agent"] in DESK_AGENTS), None)

def finder_panel(now: list[dict] | None = None, lab: bool = False) -> str:
    """The Inspiration box. It is the desk's card: while the Theorist or the finder works it lights like a mission
    card a model is on (green outline, "... working now") and, with animations on, gets a visitor from that worker's
    crew doing that agent's kind of work. assets/flow.js relights it in place (data-desk)."""
    line = store.get_inspiration()
    idle = _("The mission finder searches from this line. Empty turns the finder and Theorist off.")
    live = desk_live(now)
    state = _("{agent} working now").format(agent=agent_name(live["agent"])) if live else idle
    attrs = ' class="actions desk active"' if live else ' class="actions desk"'
    attrs += " data-lab-ok" if lab else ""       # researchers may visit it
    if live and lab:
        crew = settings.seat_worker(live["worker"])
        attrs += f' data-lab="{live["agent"]}"' + (f' data-worker="{crew}"' if crew in settings.WORKERS else "")
    think = (f'<button formaction="/theorist" title="{escape(_("Run the Theorist on the next loop pass"))}">'
             f'{_("Run Theorist")}</button>') if line else ""
    example = escape(_("e.g. Who really decides what the Federal Reserve does?"), quote=True)
    return heading("brain", _("Inspiration")) + f"""<form method="post" action="/inspiration" id="desk" data-desk{attrs}>
  <label for="inspiration" style="margin-top:0"><span class="hint" data-idle="{escape(idle, quote=True)}">{escape(state)}</span></label>
  <input type="text" id="inspiration" name="inspiration" maxlength="500" value="{escape(line, quote=True)}" placeholder="{example}">
  <p><button>{_("Save")}</button> {think}</p>{theorist_notes()}
</form>"""

def _when(at: str) -> str:
    return escape(at[:16].replace("T", " ")) + " UTC" if at else ""

BY = {"human": "you", "chat": "Researcher Chat", "theorist": "Theorist"}   # who changed the line

def theorist_notes() -> str:
    """The Theorist's last turn and how the line got here, folded away under the inspiration box."""
    last, history = store.last_theory(), store.inspiration_history(6)
    if not last and not history:
        return ""
    parts = []
    if last.get("kind") == "board":        # a board turn, then the turn before it that looked across the desk
        board = f'<a href="/board">{_("board")}</a>'
        if last.get("failed"):
            failed = _("Last turn {when} ({board}) failed: {error}").format(
                when=_when(last["at"]), board=board, error=escape(last["failed"]))
            parts.append(f'<p class="muted">{failed}</p>')
        else:
            moved = last.get("moved", [])
            sized = sum(1 for mv in moved if mv.get("size") is not None)
            what = ((_n("moved {n} note", "moved {n} notes", len(moved))
                     + (", " + _n("resized {n}", "resized {n}", sized) if sized else ""))
                    if moved else _("moved nothing"))
            tidied = _("Last turn {when} tidied the {board}: {what}.").format(when=_when(last["at"]), board=board, what=what)
            parts.append(f'<p class="muted">{tidied} {escape(last.get("why", ""))}</p>')
            made = [f'<li>{_("Proposed epic")} <strong>{escape(t)}</strong></li>' for t in last.get("epics", [])]
            made += [f'<li>{_("Proposed mission")} <a href="/m/{quote(m.slug)}">{escape(m.topic)}</a></li>'
                     for m in (store.get_mission(s) for s in last.get("missions", [])) if m]
            if made:
                parts.append(f"<ul>{''.join(made)}</ul>")
            if last.get("notes"):
                parts.append(f'<p class="hint">{_("Dropped by check:")} ' + escape("; ".join(last["notes"])) + "</p>")
        last = last.get("ideas") or {}
        if last:
            parts.append(f'<p class="hint">{_("Turn before:")}</p>')
    if last.get("failed"):
        failed = _("Last run {when} failed: {error}").format(when=_when(last["at"]), error=escape(last["failed"]))
        parts.append(f'<p class="muted">{failed}</p>')
    elif last:
        parts.append(f'<p class="muted">{_("Last run {when}.").format(when=_when(last["at"]))} '
                     f'{escape(last.get("why", ""))}</p>')
        if last.get("patterns"):
            parts.append(f'<p>{_("Patterns:")}</p><ul>' + "".join(f"<li>{escape(p)}</li>" for p in last["patterns"])
                         + "</ul>")
        made = [f'<li>{_("Proposed epic")} <strong>{escape(t)}</strong></li>' for t in last.get("epics", [])]
        m = store.get_mission(last["mission"]) if last.get("mission") else None
        if m:
            made.append(f'<li>{_("Proposed mission")} <a href="/m/{quote(m.slug)}">{escape(m.topic)}</a></li>')
        c = store.get_combine(last["combine"]) if last.get("combine") else None
        if c:
            state = {"proposed": _("waiting for you"), "approved": _("approved"),
                     "rejected": _("rejected")}.get(c.status, c.status)
            made.append(f'<li>{_("Proposed combining {n} missions into").format(n=len(c.members))} '
                        f'<strong>{escape(c.topic)}</strong> ({state})</li>')
        for a, b in last.get("links", []):
            made.append(f'<li>{_("Linked")} {mission_link(a)} · {mission_link(b)}</li>')
        if made:
            parts.append(f"<ul>{''.join(made)}</ul>")
        if last.get("lookups"):
            parts.append(f"<p>{_('Library searches:')}</p><ul>" + "".join(f"<li>{escape(q)}</li>"
                                                                   for q in last["lookups"]) + "</ul>")
        if last.get("notes"):
            parts.append(f'<p class="hint">{_("Dropped by check:")} ' + escape("; ".join(last["notes"])) + "</p>")
    if history:
        rows = "".join(f'<li><span class="hint">{_when(h.get("at", ""))}, '
                       f'{_(BY.get(h.get("by"), "Theorist"))}:</span> '
                       f'{escape(h.get("line") or _("(cleared)"))}</li>' for h in reversed(history))
        parts.append(f'<p>{_("Line history, newest first:")}</p><ul>{rows}</ul>')
    return f'<details><summary>{icon("bulb")}{_("Theorist\'s notes")}</summary>{"".join(parts)}</details>'


def f916_status() -> str:
    """One line on the 1F916 channel: who we post as, how many wait, when the next post can go."""
    if not agents.f916_enabled():
        return _("1F916 off. Set it up in {settings}.").format(settings=SETTINGS_LINK())
    queued, until, state = len(orchestrator.queue(f916)), f916.spent_until(), f916.load_state()
    if until and until > clock.now():
        when = _("next post after {when} UTC").format(when=f"{until:%b %-d %H:%M}")
    else:
        when = _("ready") if queued else _("nothing queued")
    problem = state.get("note", "") if not state.get("spent_until") else ""
    line = (f'1F916: <strong>{escape(f916.key()["handle"])}</strong> {_("on")} {escape(f916.api())} · {_("1 per day")} · '
            f'{_("{n} queued").format(n=queued)} · {when}')
    return line + (f'<br><span class="hint">{_("Last error:")} {escape(problem)}</span>' if problem else "")

def SETTINGS_LINK() -> str:
    return f'<a href="/settings#publishing">{_("Settings")}</a>'

def bluesky_status() -> str:
    """One line on the Bluesky channel: who we post as, how many wait, and the last failure if any."""
    if not agents.bluesky_enabled():
        return _("Bluesky off. Set it up in {settings}.").format(settings=SETTINGS_LINK())
    queued, problem = len(orchestrator.queue(bluesky)), bluesky.load_state().get("note", "")
    server = "" if bluesky._service() == settings.BLUESKY_DEFAULT else f' {_("on")} {escape(bluesky._service())}'
    line = (f'Bluesky: <strong>{escape(bluesky.handle())}</strong>{server} · {_("max 1 per 10 min")} · '
            f'{_("{n} queued").format(n=queued)}')
    if not settings.site_url():
        line += f'<br><span class="hint">{_("No site address: posts link to 1F916, or nowhere.")}</span>'
    return line + (f'<br><span class="hint">{_("Last error:")} {escape(problem)}</span>' if problem else "")

def _link(url: str, text: str = "") -> str:
    return f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener">{escape(text or url)}</a>'

def github_status() -> str:
    """One line on the GitHub Pages channel: which repo, where the site is, the last push or failure."""
    if not agents.github_enabled():
        return _("GitHub Pages off. Set it up in {settings}.").format(settings=SETTINGS_LINK())
    state, repo = github.load_state(), github.repo()
    line = (f'GitHub Pages: {_link(github.site_address())} {_("from")} '
            f"{_link(settings.github_url() + '/' + repo, repo)} · "
            f'{_("{n} queued").format(n=len(orchestrator.queue(github)))}')
    if state.get("at"):
        line += f' · {_("last push")} {escape(state["at"][:16].replace("T", " "))}'
    if state.get("pages_note"):
        line += f'<br><span class="hint">{_("Action needed:")} {escape(state["pages_note"])}</span>'
    if state.get("note"):
        line += f'<br><span class="hint">{_("Last push failed:")} {escape(state["note"])}</span>'
    return line
def custom_status(c) -> str:
    """One line on a custom channel: what it sends where, and how many wait."""
    cfg = c.cfg
    where = (f"{_('webhook')} <code>{escape(urlparse(cfg['target']).netloc)}</code>" if cfg["kind"] == "webhook"
             else f"{_('folder')} <code>{escape(cfg['target'])}</code>")
    fmt = _(settings.CUSTOM_FORMATS.get(cfg["format"], cfg["format"]))
    return f'{escape(c.NAME)}: {where} · {escape(fmt)} · {_("{n} queued").format(n=len(orchestrator.queue(c)))}'

def channel_status(c) -> str:
    if c.KEY == f916.KEY:
        return f916_status()
    if c.KEY == bluesky.KEY:
        return bluesky_status()
    if c.KEY == github.KEY:
        return github_status()
    return custom_status(c)

def live_channels() -> list:
    """The channels that are set up and post, in posting order."""
    return [c for c in agents.channels() if c.enabled()]

def places(m: Mission, queues: dict[str, list[str]] | None = None) -> list[dict]:
    """Every place this published article is or could be: the local site, then each channel that is set up,
    then any it reached on a channel since switched off. Each: key, name, posted (bool), on (bool), status (HTML)."""
    live = live_channels()
    if queues is None:
        queues = {c.KEY: [e["slug"] for e in orchestrator.queue(c)] for c in live}
    reached, out = orchestrator.channels(m), []
    keys = ["site"] + [c.KEY for c in live] + [k for k in reached if k != "site" and k not in {c.KEY for c in live}]
    for key in keys:
        post, bits = reached.get(key), []
        if post:
            where = escape(post.where)
            link = (f'<a href="{where}" target="_blank" rel="noopener">{where}</a>'
                    if post.where.startswith(("https://", "http://")) else
                    f'<a href="/site/{quote(m.slug)}/" target="_blank">{where}</a>' if key == "site" else where)
            bits.append(f"{_('written') if key == 'site' else _('posted')} {post.at:%b %-d, %Y %H:%M} UTC · {link}")
        if key in m.publish_requests:
            bits.append(f'<strong>{_("requested")} {m.publish_requests[key]:%b %-d %H:%M} UTC</strong>')
        elif not post and key in queues:
            slugs = queues[key]
            bits.append(_("queued, #{n} of {total}").format(n=slugs.index(m.slug) + 1, total=len(slugs))
                        if m.slug in slugs else _("waiting for feed"))
        if m.channel_errors.get(key):
            bits.append(f'{_("refused:")} {escape(m.channel_errors[key])}')
        out.append({"key": key, "name": _(orchestrator.channel_name(key)), "posted": bool(post),
                    "on": key == "site" or key in queues, "status": " · ".join(bits) or _("not posted")})
    return out

def channel_names(m: Mission) -> str:
    """Where a published mission went, e.g. "Local site, 1F916"."""
    return ", ".join(_(orchestrator.channel_name(k)) for k in orchestrator.channels(m))

def publish_form(m: Mission, rows: list[dict], back: str) -> str:
    """Publish a published article again: tick where. Places it hasn't reached yet are ticked already."""
    boxes = "".join(
        f'<label class="tick"><input type="checkbox" name="to" value="{escape(r["key"], quote=True)}"'
        f'{"" if r["posted"] and r["key"] != "site" else " checked"}> {escape(r["name"])}'
        f'<span class="hint">{" " + _("(rebuild page)") if r["key"] == "site" else " " + _("(again)") if r["posted"] else ""}</span></label>'
        for r in rows if r["on"])
    return (f'<form method="post" action="/m/{quote(m.slug)}/republish" class="republish">'
            f'<input type="hidden" name="back" value="{escape(back, quote=True)}">{boxes}'
            f'<button class="primary">{_("Publish")}</button></form>')

def places_html(m: Mission, rows: list[dict]) -> str:
    return "".join(f'<li><strong>{escape(r["name"])}</strong>: {r["status"]}</li>' for r in rows)

# ---------- settings: the two workers and the model providers ----------

WORKER_BLURBS = {
    "assistant": "Small model. Searches and reads.",
    "pi": "Big model. Thinks and writes.",
}
ROLE_STEPS = {                             # where each worker's model is used; the rest of the pipeline is code
    "assistant": ["<strong>Source finder</strong>: searches and reads pages",
                  "<strong>Mission finder</strong>: searches"],
    "pi": ["<strong>Theorist</strong>: refines the inspiration line",
           "<strong>Brief writer</strong>: writes the brief",
           "<strong>Outreach agent</strong>: asks people (outreach only)",
           "<strong>Article writer</strong>: writes the article",
           "<strong>Mission finder</strong>: picks new missions"],
}

def gb(size: int) -> str:
    return f"{size / 1e9:.1f} GB" if size >= 1e8 else f"{size / 1e6:.0f} MB"

def model_options(current: str, conn: dict, models: list[dict] | None = None) -> str:
    """The worker's model list: Ollama's installed models, then each connected cloud provider's, grouped."""
    models = models if models is not None else llm.catalog(conn)
    groups: dict[str, list[str]] = {}
    for m in models:
        bits = [escape(m["name"])]
        if m["size"]:
            bits.append(gb(m["size"]))
        if m["loaded"]:
            bits.append(_("loaded"))
        groups.setdefault(m["provider"], []).append(
            f'<option value="{escape(m["ref"], quote=True)}"{" selected" if m["ref"] == current else ""}>'
            f'{" · ".join(bits)}</option>')
    opts = [f'<optgroup label="{escape(provider_name(pid), quote=True)}">{"".join(o)}</optgroup>'
            for pid, o in groups.items()]
    if not current:                        # nothing chosen on this machine yet: the code has no default
        opts.insert(0, f'<option value="" selected disabled>{_("Choose a model")}</option>')
    elif current not in {m["ref"] for m in models}:   # saved before, or its provider is closed: still show it
        pid, name = settings.split_model(current)
        if pid == settings.LOCAL:
            missing = _("not pulled") if conn["ok"] else _("Ollama not reachable")
        else:
            prov = settings.PROVIDERS[pid]['name']
            missing = (_("{provider} not connected").format(provider=prov) if not settings.provider_on(pid)
                       else _("not offered by {provider}").format(provider=prov))
        opts.insert(0, f'<option value="{escape(current, quote=True)}" selected>{escape(current)} ({missing})</option>')
    return "".join(opts)

def count_stepper(worker: str) -> str:
    """How many of this worker run at once: − and + (assets: WORKER_COUNT_JS, which waits for the clicks to settle
    before saving) or a number typed in."""
    n, top = settings.worker_count(worker), settings.MAX_COUNT
    hint = _("each on its own mission") if n > 1 else ""
    name = worker_name(worker)
    fewer = escape(_("One fewer {worker}").format(worker=name), quote=True)
    more = escape(_("One more {worker}").format(worker=name), quote=True)
    return f"""<label for="{worker}_count">{_("Copies")}</label>
  <div class="stepper">
    <span class="steps"><button type="button" data-step="-1" aria-label="{fewer}"{" disabled" if n <= 1 else ""}>&minus;</button><input type="number" id="{worker}_count" name="count" value="{n}" min="1" max="{top}" step="1" inputmode="numeric" onchange="this.form.requestSubmit()"><button type="button" data-step="1" aria-label="{more}"{" disabled" if n >= top else ""}>+</button></span>
    <span class="hint">{hint}</span>
  </div>"""

def worker_card(worker: str, conn: dict, now: list[dict], models: list[dict] | None = None) -> str:
    on, model, other = settings.worker_on(worker), settings.worker_model(worker), settings.other(worker)
    name, other_name = worker_name(worker), worker_name(other)
    steps = "".join(f"<li>{_(s)}</li>" for s in ROLE_STEPS[worker])
    if not on:
        note = f'<p class="note hint">{_("Off. The {other} runs these steps.").format(other=other_name)}</p>'
    elif not model:
        note = (f'<p class="note"><strong>{_("No model chosen yet.")}</strong> '
                f'{_("The loop won\'t start without one.")}</p>')
    else:
        extra = "" if settings.worker_on(other) else f'<li>{_("The {other}\'s steps, while it\'s off").format(other=other_name)}</li>'
        steps += extra
        busy = [e for e in now if settings.seat_worker(e["worker"]) == worker]
        many = settings.worker_count(worker) > 1
        note = "".join(f'<p class="note">{_("Now")}{" (" + escape(seat_name(e["worker"])) + ")" if many else ""}: '
                       f'{doing(e)}</p>' for e in busy)
    checked = " checked" if on else ""
    crew = (f'<div class="crew-wrap"><div class="crew" data-crew="{worker}" aria-hidden="true"></div>'
            f'<a class="crew-edit" href="/avatars">{icon("pencil")}{_("Edit avatars")}</a></div>' if settings.animations() else "")
    return f"""<form method="post" action="/settings/worker" class="worker {"on" if on else "off"}">
  <input type="hidden" name="worker" value="{worker}">
  <div class="head"><h3>{name}</h3>
    <label class="switch"><input type="checkbox" role="switch" name="on" value="on"{checked} onchange="this.form.requestSubmit()" aria-label="{escape(_("{name} on").format(name=name), quote=True)}"></label></div>
  <p class="tagline hint">{_(WORKER_BLURBS[worker])}</p>
  <label for="{worker}_model">{_("Model")}</label>
  <select id="{worker}_model" name="model" onchange="this.form.requestSubmit()">{model_options(model, conn, models)}</select>
  {count_stepper(worker)}
  <label>{_("Steps")}</label>
  <ul>{steps}</ul>
  {note}<div class="grow"></div>
  <noscript><p><button>{_("Save")}</button></p></noscript>
  {crew}
</form>"""

def workers_section(conn: dict, models: list[dict] | None = None) -> str:
    now = running_now()
    if settings.missing_models():          # the code has no models of its own: this machine picks them
        lead = f'<strong>{_("Choose a model for each worker; the loop won\'t start until you do.")}</strong>'
    elif settings.parallel():
        lead = ""
    else:
        w = next(w for w in settings.WORKERS if settings.worker_on(w))
        n = settings.worker_count(w)
        lead = (_("Only the {worker} is on. It runs every step, one at a time.") if n == 1 else
                _("Only the {worker} is on. It runs every step, {n} at a time.")).format(worker=worker_name(w), n=n)
    models = models if models is not None else llm.catalog(conn)
    cards = "".join(worker_card(w, conn, now, models) for w in settings.WORKERS)
    return (f'{f"<p>{lead}</p>" if lead else ""}'
            f'<div class="workers">{cards}</div>{WORKER_COUNT_JS}')

# The − and + beside a worker's count: each click changes the number at once and the form saves once the clicks
# stop for a moment, so three quick clicks are one save, not three page loads.
WORKER_COUNT_JS = """<script>
document.querySelectorAll(".stepper").forEach(box => {
  const input = box.querySelector("input"), lo = +input.min, hi = +input.max;
  const buttons = box.querySelectorAll("button[data-step]");
  let timer = null;
  const show = n => buttons.forEach(b => { b.disabled = +b.dataset.step < 0 ? n <= lo : n >= hi; });
  buttons.forEach(b => b.addEventListener("click", () => {
    const n = Math.max(lo, Math.min(hi, (parseInt(input.value, 10) || lo) + +b.dataset.step));
    if (n === +input.value) return;
    input.value = n; show(n);
    clearTimeout(timer);
    timer = setTimeout(() => input.form.requestSubmit(), 700);
  }));
  input.addEventListener("input", () => show(parseInt(input.value, 10) || lo));
});
</script>"""

def ollama_form() -> str:
    saved, env = settings.saved().get("ollama_host", ""), settings._env("OLLAMA_HOST")
    return f"""<form method="post" action="/settings/ollama" class="inline">
  <label for="ollama_host" class="sr">{_("Ollama address")}</label>
  <input type="text" id="ollama_host" name="ollama_host" maxlength="300" value="{escape(saved, quote=True)}" placeholder="{escape(env or settings.OLLAMA_DEFAULT, quote=True)}">
  <button>{_("Save")}</button><span class="hint">{_("Empty uses {address}").format(address=escape(env or settings.OLLAMA_DEFAULT))}</span>
</form>"""

def model_users() -> dict[str, list[str]]:
    """Model ref -> the workers that are on and use it."""
    users: dict[str, list[str]] = {}
    for w in settings.WORKERS:
        if settings.worker_on(w):
            users.setdefault(settings.worker_model(w), []).append(worker_name(w))
    return users

def ollama_card(conn: dict) -> str:
    host = escape(conn["host"])
    connect = (f'<form method="post" action="/settings/ollama-connect" class="actions"><button class="primary">'
               f'{_("Connect Ollama")}</button> <span class="hint">{_("Gives each worker without a model one of its models.")}'
               f'</span></form>')
    head = (f'<div class="pubbox channel"><div class="head"><h3>Ollama</h3><span class="badge">{_("On this machine")}'
            f'</span></div>')
    if not conn["ok"]:
        link = '<a href="https://ollama.com/download" target="_blank" rel="noopener">ollama.com</a>'
        return (f'{head}<p>{_("Not reachable at {host} ({error}).").format(host=f"<code>{host}</code>", error=escape(conn["error"]))} '
                f'{_("Install it from {link}, pull a model, then connect.").format(link=link)}</p>{connect}{ollama_form()}</div>')
    users = model_users()
    rows = "".join(
        f'<tr><td><code>{escape(m["name"])}</code></td><td class="size">{gb(m["size"])}</td>'
        f'<td class="size">{gb(conn["loaded"][m["name"]]) if m["name"] in conn["loaded"] else ""}</td>'
        f'<td>{", ".join(users.get(m["name"], []))}</td></tr>' for m in conn["models"])
    count = len(conn["models"])
    table = (f'<table class="events models"><tr><td class="when">{_("Model")}</td><td class="when">{_("Size")}</td>'
             f'<td class="when">{_("In memory")}</td><td class="when">{_("Used by")}</td></tr>{rows}</table>' if rows else "")
    lead = (_("Connected at {host}").format(host=f"<code>{host}</code>") + " · " + _n("{n} model", "{n} models", count))
    return f'{head}<p>{lead}</p>{connect if settings.missing_models() else ""}{ollama_form()}{table}</div>'

def provider_name(pid: str) -> str:
    """A provider's name as shown: the brands as they are, the generic one in the UI's language."""
    name = settings.PROVIDERS[pid]["name"]
    return _(name) if pid == "custom" else name

def cloud_card(pid: str) -> str:
    spec = settings.PROVIDERS[pid]
    names, error = providers.models(pid)
    users = model_users()
    used = sorted({w for ref, ws in users.items() if settings.split_model(ref)[0] == pid for w in ws})
    if error:
        status = _("Not answering: {error}").format(error=escape(error))
    else:
        status = _("Connected") + " · " + _n("{n} model", "{n} models", len(names))
    if pid == "custom":
        status += f" · <code>{escape(settings.provider_url(pid))}</code>"
    if used:
        status += " · " + _("used by {workers}").format(workers=", ".join(used))
    if settings.provider_saved(pid):
        remove = (f'<form method="post" action="/settings/provider"><input type="hidden" name="do" value="forget">'
                  f'<input type="hidden" name="provider" value="{pid}"><button>'
                  f'{_("Disconnect {provider}").format(provider=escape(provider_name(pid)))}</button></form>')
    else:
        remove = f'<p class="hint">{_("Set in .env ({key}).").format(key=escape(spec["key_env"]))}</p>'
    return (f'<div class="pubbox channel"><div class="head"><h3>{escape(provider_name(pid))}</h3><span class="badge">{_("Cloud")}</span>'
            f'</div><p>{status}</p>{remove}</div>')

def add_provider_forms() -> str:
    out = []
    for pid in settings.PROVIDERS:
        if pid == settings.LOCAL or settings.provider_on(pid):
            continue
        spec = settings.PROVIDERS[pid]
        get_key = (" " + _("Get a key at {link}.").format(
            link=f'<a href="{escape(spec["keys"], quote=True)}" target="_blank" rel="noopener">'
                 f'{escape(spec["keys"].split("//")[-1].split("/")[0])}</a>') if spec["keys"] else "")
        env = " " + _("Or set {key} in .env.").format(key=f'<code>{escape(spec["key_env"])}</code>') if spec["key_env"] else ""
        url = ""
        if pid == "custom":
            url = f"""<label for="url_{pid}">{_("Address")} <span class="hint">{_("API base URL, before /chat/completions")}</span></label>
  <input type="text" id="url_{pid}" name="url" required maxlength="300" placeholder="http://127.0.0.1:1234/v1">"""
        key_label = _("API key") + (f' <span class="hint">{_("optional")}</span>' if spec.get("optional_key") else "")
        required = "" if spec.get("optional_key") else " required"
        out.append(f"""<details class="pubbox add"><summary><strong>{escape(provider_name(pid))}</strong> · {escape(_(spec["blurb"]))}</summary>
<p>{_("Billed per use by {provider}.").format(provider=escape(provider_name(pid)))}{get_key} {_("Key saved to data/providers.json.")}{env}</p>
<form method="post" action="/settings/provider">
  <input type="hidden" name="do" value="connect"><input type="hidden" name="provider" value="{pid}">
  {url}
  <label for="key_{pid}">{key_label}</label>
  <input type="password" id="key_{pid}" name="key"{required} maxlength="500" autocomplete="off">
  <p><button>{_("Connect")}</button></p>
</form></details>""")
    return "".join(out)

def connection_section(conn: dict) -> str:
    cards = ollama_card(conn) + "".join(cloud_card(p) for p in settings.cloud_providers())
    return f'{cards}<h3 class="sub">{_("Add a provider")}</h3>{add_provider_forms()}'

EMBEDDY = re.compile(r"embed|bge|minilm|e5-|gte|arctic|mxbai|nomic", re.I)   # an Ollama embedding model's name

def library_section(conn: dict) -> str:
    """The desk's library (library.py): which provider and model embed its passages, and how many a search returns."""
    current, env = settings.embed_model(), settings._env(settings.EMBED_ENV)
    pid, name = settings.split_model(current) if current else ("none", "")
    choices = [("none", _("None: rank by words only"))] + [
        (p, provider_name(p)) for p in settings.embed_providers()]
    if pid not in dict(choices):           # set to a provider since disconnected: keep it visible
        choices.append((pid, _("{provider} not connected").format(provider=settings.PROVIDERS.get(pid, {}).get("name", pid))))
    opts = "".join(f'<option value="{p}"{" selected" if p == pid else ""}>{escape(label)}</option>' for p, label in choices)
    names = [(m["name"], "Ollama") for m in conn["models"] if EMBEDDY.search(m["name"])]
    for p in settings.embed_providers()[1:]:
        names += [(n, settings.PROVIDERS[p]["name"]) for n in providers.models(p, embed=True)[0]]
    suggestions = "".join(f'<option value="{escape(n, quote=True)}" label="{escape(label, quote=True)}">'
                          for n, label in names)
    tops = "".join(f'<option value="{n}"{" selected" if n == settings.library_top_n() else ""}>{n}</option>'
                   for n in settings.TOP_N_CHOICES)
    from_env = env and current == env and settings.saved().get("embed_model") in (None, "")
    source = " " + _("(from {key} in .env)").format(key="MISSION_LOOP_EMBED_MODEL") if from_env else ""
    now = (" " + _("Embedding model: {model}").format(model=f"<code>{escape(current)}</code>") + f"{source}.") if current else ""
    intro = _("Missions, briefs, sources and articles, searched by the Theorist and Researcher Chat. "
              "Ranks by words; an embedding model adds meaning. Stored in {file}.").format(file="<code>data/library.db</code>")
    return f"""{part("book", _("Library"), "library")}
<p>{intro}</p>
<div class="pubbox channel"><div class="head"><h3>{_("Index")}</h3><span class="badge">{_("On this machine")}</span></div>
<p>{library_line()}{now}</p>
<form method="post" action="/settings/library" class="inline"><input type="hidden" name="do" value="index">
  <button>{_("Index now")}</button><span class="hint">{_("embeds every new passage now")}</span></form></div>
<form method="post" action="/settings/library">
  <input type="hidden" name="do" value="save">
  <label for="embed_provider">{_("Embedding provider")} <span class="hint">{_("Anthropic, DeepSeek and Groq have none")}</span></label>
  <select id="embed_provider" name="provider">{opts}</select>
  <label for="embed_model">{_("Embedding model")} <span class="hint">{_("checked before saving")}</span></label>
  <input type="text" id="embed_model" name="model" maxlength="200" list="embed_models" value="{escape(name, quote=True)}" autocomplete="off">
  <datalist id="embed_models">{suggestions}</datalist>
  <label for="top_n">{_("Passages per search")} <span class="hint">{_("more context, more cost")}</span></label>
  <select id="top_n" name="top_n">{tops}</select>
  <p><button>{_("Save")}</button></p>
</form>"""

def library_line() -> str:
    st = library.status()
    line = _("{passages} passages from {missions} missions").format(passages=st["passages"], missions=st["missions"])
    how = (_("{done} of {total} embedded").format(done=st["embedded"], total=st["passages"]) if st["model"]
           else _("ranked by words only"))
    return f"{line} · {how}."

def index_now() -> threading.Thread:
    """Bring the whole library up to date in the background, embedding everything new (searches do a minute's worth)."""
    def go():
        try:
            library.sync(time_limit=30 * 60)
        except Exception as e:
            print(f"library indexing failed: {type(e).__name__}: {e}", flush=True)
    t = threading.Thread(target=go, name="library", daemon=True)
    t.start()
    return t

def ollama_picks(conn: dict) -> dict[str, str]:
    """Which installed model each worker without one gets: the biggest to the Principal Investigator (writing), the
    smallest to the Research Assistant (legwork). Chosen from what this machine has; the code names none."""
    by_size = sorted(conn["models"], key=lambda m: (m["size"], m["name"]))
    if not by_size:
        return {}
    pick = {"pi": by_size[-1]["name"], "assistant": by_size[0]["name"]}
    return {w: pick[w] for w in settings.WORKERS if not settings.worker_model(w)}

async def save_ollama(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    try:
        settings.set_ollama_host(str((await request.form()).get("ollama_host") or "")[:300])
    except ValueError:
        return go("/settings", error="ollama_host")
    return go("/settings", done="ollama_saved")

async def connect_ollama(request: Request) -> Response:
    """One click: is Ollama there, and does every worker have a model? Fills in the workers that have none."""
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    back = _back(form) if form.get("back") else "/settings"
    conn = await run_in_threadpool(llm.connection, 5)
    if not conn["ok"]:
        return go(back, error="ollama")
    if not conn["models"]:
        return go(back, error="ollama_empty")
    picks = ollama_picks(conn)
    for worker, model in picks.items():
        settings.set_worker(worker, model, settings.worker_on(worker))
    return go(back, done="ollama_connected" if picks else "ollama_ready")

async def save_provider(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    pid, do = str(form.get("provider") or ""), str(form.get("do") or "")
    if pid not in settings.PROVIDERS or pid == settings.LOCAL:
        return go("/settings", error="provider")
    name = provider_name(pid)
    if do == "forget":
        settings.forget_provider(pid)
        providers.forget_models(pid)
        return go("/settings", done="provider_off")
    key, url = str(form.get("key") or "").strip()[:500], str(form.get("url") or "").strip()[:300]
    try:
        url = settings._http_address(url) if url else ""
        if not key and not settings.PROVIDERS[pid].get("optional_key"):
            raise ValueError(_("paste the API key"))
        if any(c.isspace() for c in key):
            raise ValueError(_("that doesn't look like an API key"))
        if not url and not settings.PROVIDERS[pid]["url"]:
            raise ValueError(_("enter the server address"))
        found = await run_in_threadpool(providers.list_models, pid, key, url or settings.PROVIDERS[pid]["url"])
        settings.save_provider(pid, key, url)
    except (ValueError, providers.ProviderError) as e:
        return page(_("Settings"), settings_body(request, _("{provider} not connected: {error}").format(provider=name, error=e)))
    providers.forget_models(pid)
    if not found:
        return page(_("Settings"), settings_body(request, _("{provider} connected, but offers no chat models for this key.").format(provider=name)))
    return go("/settings", done="provider_on")

async def save_worker(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    worker, on = str(form.get("worker") or ""), form.get("on") == "on"   # an unticked switch sends nothing
    model = str(form.get("model") or "").strip()[:200]
    if worker not in settings.WORKERS:
        return go("/settings", error="worker_model")
    was_on, was_count, was_model = settings.worker_on(worker), settings.worker_count(worker), settings.worker_model(worker)
    if form.get("count") is not None:       # the card's stepper (saved on its own); a form without one leaves it be
        try:
            settings.set_worker_count(worker, int(str(form.get("count")).strip()))
        except ValueError:
            return go("/settings", error="worker_count")
    if model == was_model and on == was_on:  # only the count changed: no model to check yet is fine
        return go("/settings", done="worker_count" if settings.worker_count(worker) != was_count else "worker_saved")
    if model != was_model:
        pid = settings.split_model(model)[0]
        if pid == settings.LOCAL:
            conn = await run_in_threadpool(llm.connection)
            if not conn["ok"]:
                return go("/settings", error="ollama")
            offered = {m["name"] for m in conn["models"]}
        else:
            if not settings.provider_on(pid):
                return go("/settings", error="worker_model")
            offered = {settings.model_ref(pid, n) for n in (await run_in_threadpool(providers.models, pid))[0]}
        if model not in offered:
            return go("/settings", error="worker_model")
    try:
        settings.set_worker(worker, model, on)
    except ValueError as e:
        return go("/settings", error="worker_last" if "stay on" in str(e) else "worker_model")
    done = "worker_saved" if on == was_on else "worker_on" if on else "worker_off"
    return go("/settings", done=done)


# ---------- settings: the feed and channels ----------

HANDLE = re.compile(r"[A-Za-z0-9_-]{2,32}")
GITHUB_LOGIN: dict = {}                     # the GitHub sign-in in progress (start_login's answer); one at a time

def github_row() -> str:
    """The GitHub Pages channel on the Settings page: one step at a time, connect, then pick the repo."""
    if os.environ.get("MISSION_LOOP_STUBS") == "1":
        return f"<p>{_(STUB_RUN)}</p>"
    gh = escape(settings.github_url(), quote=True)
    if github.enabled():
        return f"""<p>{github_status()}</p>
<p class="hint">{_("Changes in site/ are pushed after each publish.")}</p>
<form method="post" action="/settings/github" class="actions inline-buttons">
  <button name="do" value="unlink">{_("Use a different repo")}</button> <button name="do" value="forget">{_("Disconnect GitHub")}</button>
</form>"""
    if github.connected():
        name = escape(github.suggested_name(), quote=True)
        served = _("{repo} is served at {address}").format(
            repo=f"<code>{escape(github.suggested_name())}</code>",
            address=escape(github.pages_address(github.login_name() + "/" + github.suggested_name())))
        connected = _("Connected as {login}.").format(login=f"<strong>{escape(github.login_name())}</strong>")
        return f"""<p>{connected} {_("Choose a repo for the site. Its contents will be replaced, so use a new or empty public repo.")}</p>
<form method="post" action="/settings/github" class="actions">
  <input type="hidden" name="do" value="create">
  <label for="gh_name" style="margin-top:0">{_("New repo")} <span class="hint">{served}</span></label>
  <input type="text" id="gh_name" name="name" maxlength="100" value="{name}">
  <p><button>{_("Create repo and publish")}</button></p>
</form>
<form method="post" action="/settings/github" class="actions">
  <input type="hidden" name="do" value="link">
  <label for="gh_repo">{_("Existing repo")} <span class="hint">{_("repo link")}</span></label>
  <input type="text" id="gh_repo" name="repo" maxlength="300" required placeholder="{gh}/{escape(github.login_name())}/my-site">
  <p><button>{_("Link repo and publish")}</button></p>
</form>
<form method="post" action="/settings/github" class="actions"><input type="hidden" name="do" value="forget">
  <p><button>{_("Disconnect GitHub")}</button></p></form>"""
    if GITHUB_LOGIN:
        code, where = escape(GITHUB_LOGIN["user_code"]), escape(GITHUB_LOGIN["verification_uri"], quote=True)
        return f"""<div class="gh-code" data-interval="{GITHUB_LOGIN['interval']}">
<p>{_("Open {link} and enter this code:").format(link=f'<a href="{where}" target="_blank" rel="noopener">{where}</a>')}</p>
<p class="code-big"><code>{code}</code> <button type="button" class="copy" data-code="{code}">{_("Copy")}</button></p>
<p class="hint gh-wait">{_("Waiting for approval…")}</p>
</div>
<form method="post" action="/settings/github" class="actions"><input type="hidden" name="do" value="cancel">
  <p><button>{_("Cancel")}</button></p></form>
<script>
(() => {{
  const box = document.querySelector(".gh-code"), wait = box.querySelector(".gh-wait");
  let every = +box.dataset.interval * 1000;
  box.querySelector(".copy").onclick = e => navigator.clipboard?.writeText(e.target.dataset.code);
  const poll = async () => {{
    try {{
      const r = await (await fetch("/settings/github/poll", {{method: "POST"}})).json();
      if (r.state === "done") return location.assign("/settings?done=github_connected");
      if (r.state === "error") {{ wait.textContent = r.message + " " + {json.dumps(_("Cancel and connect again."))}; return; }}
      if (r.state === "slow_down") every += 5000;
    }} catch (e) {{}}
    setTimeout(poll, every);
  }};
  setTimeout(poll, every);
}})();
</script>"""
    if not settings.github_client_id():
        needs = _("Needs a GitHub OAuth app. {create} with any name and URLs (e.g. http://127.0.0.1:8770), tick "
                  "{device}, then paste its client ID.").format(
            create=f'<a href="{gh}/settings/applications/new" target="_blank" rel="noopener">{_("Create one")}</a>',
            device="<strong>Enable Device Flow</strong>")
        return f"""<p>{needs}</p>
<form method="post" action="/settings/github" class="actions">
  <input type="hidden" name="do" value="client_id">
  <label for="gh_client" style="margin-top:0">{_("OAuth app client ID")}</label>
  <input type="text" id="gh_client" name="client_id" maxlength="64" required placeholder="Ov23li…">
  <p><button>{_("Save")}</button></p>
</form>"""
    return f"""<p>{_("Hosts the site free on GitHub Pages. You sign in on GitHub; access is limited to public repos.")}</p>
<form method="post" action="/settings/github" class="actions"><input type="hidden" name="do" value="connect">
  <p><button>{_("Connect GitHub")}</button></p></form>"""

STUB_RUN = "Off in stub mode (MISSION_LOOP_STUBS=1)."

def local_site_box() -> str:
    """The foundation: the static site in site/ on this machine, which every channel posts from."""
    url, env = settings.site_url(), settings.SITE_URL
    count = len(publisher.entries())
    feeds = ('<a href="/site/feed.xml" target="_blank">site/feed.xml</a> (RSS) · '
             '<a href="/site/feed.json" target="_blank">site/feed.json</a> (JSON Feed)')
    hosted = escape(url.rstrip("/") + "/")
    links = _("Feed links use {address}").format(address=hosted) if url else _("Feed links are relative to the feed file.")
    source = (f' <span class="hint">{_("(from .env)")}</span>'
              if env and not settings.saved().get("site_url") else
              ' <span class="hint">(GitHub Pages)</span>'
              if url and not settings.saved().get("site_url") else "")
    return f"""<div class="pubbox">
<div class="head"><h3>{_("Local site")}</h3><span class="badge">{_("Always on")}</span></div>
<p>{_("Files in site/. Every channel posts from here.")}</p>
<p>{_n("{n} article", "{n} articles", count)} · {feeds} · <a href="/published">{_("Published")}</a></p>
<form method="post" action="/settings/site-url">
  <label for="site_url">{_("Site address")}{source} <span class="hint">{_("optional: where site/ is hosted, for feed and Bluesky links")}</span></label>
  <input type="text" id="site_url" name="site_url" maxlength="300" value="{escape(settings.saved().get("site_url", ""), quote=True)}" placeholder="{escape(url or "https://example.com/news/", quote=True)}">
  <p class="hint">{links}</p>
  <p><button>{_("Save")}</button></p>
</form></div>"""

def channel_card(c) -> str:
    """One channel that is set up: what it is, how it is doing, and how to take it away."""
    if c.KEY == f916.KEY:
        kind, remove = _("Built in"), f'<p class="hint">{_("Permanent: data/1f916.json is the account\'s only key.")}</p>'
    elif c.KEY == github.KEY:               # its status and its unlink / disconnect buttons
        return (f'<div class="pubbox channel"><div class="head"><h3>{escape(c.NAME)}</h3><span class="badge">{_("Built in")}</span>'
                f'</div>{github_row()}</div>')
    elif c.KEY == bluesky.KEY:
        kind = _("Built in")
        remove = (f"""<form method="post" action="/settings/bluesky"><input type="hidden" name="forget" value="1">
  <button>{_("Disconnect Bluesky")}</button></form>""" if bluesky.saved_credentials() else
                  f'<p class="hint">{_("Set in .env (BLUESKY_HANDLE, BLUESKY_APP_PASSWORD).")}</p>')
    else:
        kind = _("Custom") + " · " + _(settings.CUSTOM_KINDS.get(c.cfg["kind"], c.cfg["kind"]))
        remove = (f'<form method="post" action="/settings/custom-channel/remove">'
                  f'<input type="hidden" name="id" value="{escape(c.cfg["id"], quote=True)}">'
                  f'<button>{_("Remove channel")}</button></form>')
    return (f'<div class="pubbox channel"><div class="head"><h3>{escape(c.NAME)}</h3><span class="badge">{escape(kind)}</span>'
            f'</div><p>{channel_status(c)}</p>{remove}</div>')

def add_channel_forms() -> str:
    """Ways to add a channel: the built-in ones not set up yet, and a custom one."""
    out = []
    if not agents.f916_enabled():
        server = f'<a href="{escape(settings.f916_url(), quote=True)}" target="_blank" rel="noopener">{escape(settings.f916_url())}</a>'
        body = (f"<p>{_(STUB_RUN)}</p>" if os.environ.get("MISSION_LOOP_STUBS") == "1" else f"""<p>{_("Posts each article's full text, 1 per day. Joining creates a permanent public account on {server}. Its key is saved to data/1f916.json and can't be recovered.").format(server=server)}</p>
<form method="post" action="/settings/join-1f916">
  <label for="handle">{_("Handle")} <span class="hint">{_("2–32 letters, digits, _ or -; permanent")}</span></label>
  <input type="text" id="handle" name="handle" required maxlength="32" pattern="[A-Za-z0-9_-]{{2,32}}" placeholder="loop-desk">
  <label for="f916_url">{_("Server")}</label>
  <input type="text" id="f916_url" name="f916_url" maxlength="300" value="{escape(settings.f916_url(), quote=True)}">
  <p><button>{_("Join 1F916")}</button></p>
</form>""")
        out.append(f'<details class="pubbox add"><summary><strong>1F916</strong> · {_("built in")}</summary>{body}</details>')
    if not agents.github_enabled():         # connect, then pick the repo: open while a step is under way
        under_way = " open" if (GITHUB_LOGIN or github.connected()) and os.environ.get("MISSION_LOOP_STUBS") != "1" else ""
        out.append(f'<details class="pubbox add"{under_way}><summary><strong>GitHub Pages</strong> · {_("built in")}</summary>'
                   f'{github_row()}</details>')
    if not agents.bluesky_enabled():
        body = (f"<p>{_(STUB_RUN)}</p>" if os.environ.get("MISSION_LOOP_STUBS") == "1" else f"""<p>{escape(_("Posts a 3-post thread per article, max 1 per 10 min. Use an app password (Bluesky: Settings > Privacy and security > App passwords), not your account password."))}</p>
<form method="post" action="/settings/bluesky">
  <label for="bsky_handle">{_("Handle")}</label>
  <input type="text" id="bsky_handle" name="handle" required maxlength="253" autocomplete="username" placeholder="yourname.bsky.social">
  <label for="bsky_password">{_("App password")}</label>
  <input type="password" id="bsky_password" name="app_password" required maxlength="64" autocomplete="off" placeholder="xxxx-xxxx-xxxx-xxxx">
  <label for="bsky_service">{_("Server")} <span class="hint">{_("optional, for accounts not on bsky.social")}</span></label>
  <input type="text" id="bsky_service" name="service" maxlength="300" placeholder="{escape(settings.bluesky_service(), quote=True)}">
  <p><button>{_("Connect Bluesky")}</button></p>
</form>""")
        out.append(f'<details class="pubbox add"><summary><strong>Bluesky</strong> · {_("built in")}</summary>{body}</details>')
    kinds = "".join(f'<option value="{k}">{escape(_(v))}</option>' for k, v in settings.CUSTOM_KINDS.items())
    formats = "".join(f'<option value="{k}">{escape(_(v))}</option>' for k, v in settings.CUSTOM_FORMATS.items())
    out.append(f"""<details class="pubbox add"><summary><strong>{_("Custom channel")}</strong> · {_("webhook or folder")}</summary>
<p>{_("Sends each published article to a webhook, or as a file into a folder.")}</p>
<form method="post" action="/settings/custom-channel">
  <label for="cc_name">{_("Name")}</label>
  <input type="text" id="cc_name" name="name" required maxlength="40" placeholder="{escape(_("e.g. Team chat"), quote=True)}">
  <label for="cc_kind">{_("Type")}</label>
  <select id="cc_kind" name="kind">{kinds}</select>
  <label for="cc_target">{_("Target")} <span class="hint">{_("webhook URL or folder path")}</span></label>
  <input type="text" id="cc_target" name="target" required maxlength="500" placeholder="https://hooks.example.com/abc · /Users/me/outbox">
  <label for="cc_format">{_("Format")}</label>
  <select id="cc_format" name="format">{formats}</select>
  <details class="formats"><summary>{_("Format details")}</summary>
  <p><strong>JSON</strong>: {escape(_("a JSON Feed 1.1 item, as in site/feed.json"))} (<code>&lt;short-name&gt;.json</code>).</p>
  <p><strong>Markdown</strong>: {_("title, dek, site link, full text")} (<code>&lt;short-name&gt;.md</code>).</p>
  <p><strong>{_("Plain text")}</strong>: {_("the same without markup")} (<code>&lt;short-name&gt;.txt</code>).</p>
  <p>{_("Webhooks: 2xx is posted, 429 retries later, other 4xx refuses, anything else retries next pass.")}</p>
  </details>
  <p><button>{_("Add channel")}</button></p>
</form></details>""")
    return "".join(out)

def publishing_section() -> str:
    live = live_channels()
    if os.environ.get("MISSION_LOOP_STUBS") == "1":
        cards = f'<p class="muted">{_(STUB_RUN)}</p>'
    else:
        cards = "".join(channel_card(c) for c in live) or \
            f'<p class="muted">{_("No channels.")}</p>'
    return f"""
{local_site_box()}
<h3 class="sub">{_("Channels")} ({len(live)})</h3>
{cards}
<h3 class="sub">{_("Add a channel")}</h3>
{add_channel_forms()}"""

THEME_CHOICES = [("system", "monitor", "System"), ("light", "sun", "Light"), ("dark", "moon", "Dark")]
OWN_DEFAULT = "#c9b8a3"     # the colour picker's starting point before a human picks: a warm parchment

# Live preview while picking your own colour; letting go saves it. The page reloads on save, so this only
# has to paint the bubble and the swatch in between.
CHAT_COLOR_JS = """<script>
(function () {
  const own = document.getElementById("own-color");
  if (!own) return;
  const pick = own.closest(".swatch"), radio = pick.querySelector("input[type=radio]");
  own.addEventListener("input", () => {
    radio.checked = true;
    pick.classList.add("set");
    pick.style.setProperty("--c", own.value);
    document.documentElement.style.setProperty("--chat", own.value);
  });
  own.addEventListener("change", () => own.form.requestSubmit());
})();
</script>"""

def display_forms() -> str:
    """Appearance (system, light or dark) and the chat colour, each saved the moment it is picked."""
    theme = settings.theme()
    themes = "".join(
        f'<label><input type="radio" name="theme" value="{v}"{" checked" if v == theme else ""} '
        f'onchange="this.form.requestSubmit()">{icon(ico)}{_(label)}</label>' for v, ico, label in THEME_CHOICES)
    langs = "".join(
        f'<label><input type="radio" name="language" value="{code}"{" checked" if code == i18n.current() else ""} '
        f'onchange="this.form.requestSubmit()"><span lang="{code}">{escape(name)}</span></label>'
        for code, name in settings.LANGUAGES.items())
    current = settings.chat_color()
    known = set(settings.CHAT_COLORS.values())
    def swatch(value: str, name: str, color: str) -> str:
        on = " checked" if value == current else ""
        ink = settings.ink_on(color) if color.startswith("#") else "#ffffff"
        return (f'<label class="swatch" style="--c:{color};--ink:{ink}"><input type="radio" name="choice" value="{value}"'
                f'{on} onchange="this.form.requestSubmit()"><span class="dab"></span>{escape(_(name))}</label>')
    swatches = swatch("", "Library red", "var(--accent)")
    swatches += "".join(swatch(c, n, c) for n, c in settings.CHAT_COLORS.items())
    mine = current if current and current not in known else ""
    own_on = " checked" if mine else ""
    own_style = f' style="--c:{mine};--ink:{settings.ink_on(mine)}"' if mine else ""
    swatches += (f'<label class="swatch own{" set" if mine else ""}"{own_style}>'
                 f'<input type="radio" name="choice" value="own"{own_on} onchange="this.form.requestSubmit()">'
                 f'<span class="dab"><input type="color" id="own-color" name="own" value="{mine or OWN_DEFAULT}" '
                 f'aria-label="{escape(_("Choose your own colour"), quote=True)}"></span>{_("Your own")}</label>')
    return f"""<form method="post" action="/settings/language" class="actions display">
  <h3>{icon("globe")}{_("Language")}</h3>
  <div class="choice" role="radiogroup" aria-label="{escape(_("Language"), quote=True)}">{langs}</div>
  <noscript><p><button>{_("Save")}</button></p></noscript>
</form>
<form method="post" action="/settings/theme" class="actions display">
  <h3>{icon("sun")}{_("Appearance")}</h3>
  <p class="hint">{_("System follows your computer's setting.")}</p>
  <div class="choice" role="radiogroup" aria-label="{escape(_("Appearance"), quote=True)}">{themes}</div>
  <noscript><p><button>{_("Save")}</button></p></noscript>
</form>
<form method="post" action="/settings/chat-color" class="actions display">
  <h3>{icon("palette")}{_("Chat colour")}</h3>
  <p class="hint">{_("The chat bubble and your messages.")}</p>
  <div class="swatches" role="radiogroup" aria-label="{escape(_("Chat colour"), quote=True)}">{swatches}</div>
  <noscript><p><button>{_("Save")}</button></p></noscript>
</form>{CHAT_COLOR_JS}"""

# The Settings page's sections, in order: (anchor, icon, title). The nav down its left lists them.
SETTINGS_SECTIONS = [
    ("setup", "compass", "Setup"),
    ("workers", "people", "Workers"),
    ("providers", "plug", "Model providers"),
    ("research", "search", "Research"),
    ("mcp", "link", "MCP server"),
    ("display", "monitor", "Display"),
    ("publication", "newspaper", "Publication"),
    ("publishing", "broadcast", "Publishing"),
    ("outreach", "send", "Outreach"),
]

# The nav down the Settings page's left: a click glides to its section, and scrolling lights the section in view.
SETTINGS_NAV_JS = """<script>
(() => {
  const nav = document.querySelector(".set-nav");
  if (!nav) return;
  const links = [...nav.querySelectorAll("a")];
  const secs = links.map(a => document.getElementById(a.hash.slice(1)));
  let hold = 0;                                    // while a click glides there, scrolling past others lights nothing
  function light(i) {
    links.forEach((a, j) => { a.classList.toggle("on", j === i); a.toggleAttribute("aria-current", j === i); });
    const a = links[i];
    if (a && nav.scrollWidth > nav.clientWidth)    // the bar across the top on a narrow screen keeps it in view
      nav.scrollTo({left: a.offsetLeft - (nav.clientWidth - a.offsetWidth) / 2, behavior: "smooth"});
  }
  function spy() {
    if (Date.now() < hold) return;
    const across = getComputedStyle(nav).flexDirection === "row";     // the bar across the top on a narrow screen
    const line = Math.min(innerHeight * .3, 220) + (across ? nav.offsetHeight : 0);
    let i = 0;
    secs.forEach((s, j) => { if (s && s.getBoundingClientRect().top <= line) i = j; });
    if (innerHeight + scrollY >= document.documentElement.scrollHeight - 4) i = secs.length - 1;
    light(i);
  }
  addEventListener("scroll", spy, {passive: true});          // seven boxes measured: cheap enough on every scroll
  addEventListener("scrollend", () => { hold = 0; spy(); });
  addEventListener("resize", spy);
  nav.addEventListener("click", e => {
    const a = e.target.closest("a");
    if (!a) return;
    const i = links.indexOf(a), s = secs[i];
    if (!s) return;
    e.preventDefault();
    hold = Date.now() + 1200;
    light(i);
    const calm = matchMedia("(prefers-reduced-motion: reduce)").matches;
    s.scrollIntoView({behavior: calm ? "auto" : "smooth", block: "start"});
    history.replaceState(history.state, "", a.hash);
  });
  spy();
  addEventListener("DOMContentLoaded", spy, {once: true});   // after KEEP_SCROLL_JS puts the page back where it was
  addEventListener("load", spy, {once: true});
})();
</script>"""

def settings_section(anchor: str, body: str) -> str:
    ico, title = next((i, t) for a, i, t in SETTINGS_SECTIONS if a == anchor)
    return f'<section class="set" id="{anchor}"><h2>{icon(ico)}<span>{escape(_(title))}</span></h2>{body}</section>'

def settings_body(request: Request, error: str = "") -> str:
    err = f'<div class="flash err">{escape(error)}</div>' if error else flash(request)
    checked = " checked" if settings.animations() else ""
    conn = llm.connection()
    models = llm.catalog(conn)
    sprites = script("researchers.js") if settings.animations() else ""
    on = ' class="on" aria-current="true"'
    nav = "".join(f'<a href="#{a}"{on if n == 0 else ""}>{icon(i)}<span>{escape(_(t))}</span></a>'
                  for n, (a, i, t) in enumerate(SETTINGS_SECTIONS))
    loop = f"""{part("clock", _("Loop"))}
<form method="post" action="/settings/loop-every" class="inline">
  <label for="every" style="margin:0">{_("Rest when idle")}</label>
  <input type="number" id="every" name="every" value="{settings.loop_every():g}" min="1" step="1" style="width:5rem">
  <span class="hint">{_("minutes (applies on next start)")}</span>
  <button>{_("Save")}</button>
</form>"""
    animations = f"""<form method="post" action="/settings/animations" class="actions display">
  <h3>{icon("activity")}{_("Animations")}</h3>
  <p class="hint">{_("Pixel researchers on active missions.")}</p>
  <label class="switch"><input type="checkbox" role="switch" name="animations" value="on"{checked} onchange="this.form.requestSubmit()">
  {_("Animations")}</label>
  <noscript><p><button>{_("Save")}</button></p></noscript>
</form>"""
    sections = "".join([
        settings_section("setup", setup_section(conn)),
        settings_section("workers", workers_section(conn, models) + loop),
        settings_section("providers", connection_section(conn)),
        settings_section("research", search_section() + library_section(conn)),
        settings_section("mcp", mcp_section()),
        settings_section("display", display_forms() + board_style_form() + animations + avatar_link()),
        settings_section("publication", publication_section()),
        settings_section("publishing", publishing_section()),
        settings_section("outreach", outreach_settings()),
    ])
    return f"""{err}<h1>{_("Settings")}</h1>
<div class="settings">
<nav class="set-nav" aria-label="{escape(_("Settings sections"), quote=True)}">{nav}</nav>
<div class="set-body">{sections}</div>
</div>
{SETTINGS_NAV_JS}
{sprites}"""

async def settings_page(request: Request) -> HTMLResponse:
    return page(_("Settings"), settings_body(request))

async def save_site_url(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    try:
        url = settings.set_site_url(str(form.get("site_url") or "")[:300])
    except ValueError:
        return go("/settings", error="site_url")
    orchestrator.refresh_feed()            # the feeds pick up the new address right away
    return go("/settings", done="site_url_saved" if url else "site_url_cleared")

async def save_theme(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    try:
        settings.set_theme(str((await request.form()).get("theme") or ""))
    except ValueError:
        return go("/settings", error="theme")
    return go("/settings", done="theme_saved")

async def save_language(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    try:
        settings.set_language(str((await request.form()).get("language") or ""))
    except ValueError:
        return go("/settings", error="language")
    return go("/settings", done="language_saved")

async def save_chat_color(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    choice = str(form.get("choice") or "")
    try:
        settings.set_chat_color(str(form.get("own") or "") if choice == "own" else choice)
    except ValueError:
        return go("/settings", error="chat_color")
    return go("/settings", done="chat_color_saved")
async def save_auto(request: Request) -> Response:
    """The top bar's Auto switch. Turning it on approves what already waits (briefs, proposed epics and combines)
    right away;
    the loop approves the rest as they come. Publishing always stays with the human."""
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    on = form.get("auto") == "on"          # an unticked box sends nothing
    settings.set_auto_mode(on)
    if on:
        orchestrator.auto_approve(clock.now(), {e["slug"] for e in running_now()})
    if loop_running():
        wake()
    return go(_back(form), done="auto_on" if on else "auto_off")

async def save_loop_every(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    try:
        settings.set_loop_every(float((await request.form()).get("every") or settings.LOOP_EVERY))
    except ValueError:
        return go("/settings", error="every")
    return go("/settings", done="every_saved")

async def save_library(request: Request) -> Response:
    """The Library section: index now, or save the embedding provider and model (checked with one embedding first)
    and the top N."""
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    if form.get("do") == "index":
        index_now()
        return go("/settings", done="library_indexing")
    try:
        top_n = int(str(form.get("top_n") or settings.TOP_N))
        if top_n not in settings.TOP_N_CHOICES:
            raise ValueError
    except ValueError:
        return go("/settings", error="top_n")
    pid, model = str(form.get("provider") or "none"), str(form.get("model") or "").strip()[:200]
    try:
        if pid != "none" and pid not in settings.PROVIDERS:
            raise ValueError(_("pick a provider"))
        if pid != "none" and not model:
            raise ValueError(_("name the embedding model"))
        ref = settings.model_ref(pid, model) if pid != "none" else ""
        if ref != settings.embed_model():                 # unchanged: store nothing, so .env keeps applying
            if ref:
                await run_in_threadpool(library.check, ref)
            settings.set_embed_model(ref or settings.WORDS_ONLY)
    except (ValueError, library.LibraryError) as e:
        return page(_("Settings"), settings_body(request, _("Embedding model not saved: {error}").format(error=e)))
    settings.set_library_top_n(top_n)
    return go("/settings", done="library_saved")

async def save_animations(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    on = (await request.form()).get("animations") == "on"   # an unticked box sends nothing
    settings.set_animations(on)
    return go("/settings", done="animations_on" if on else "animations_off")

async def join_1f916(request: Request) -> Response:
    """The same registration as `python run.py join-1f916`, which saves the key before anything else."""
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    handle = str(form.get("handle") or "").strip()
    if not HANDLE.fullmatch(handle):
        return go("/settings", error="handle")
    if f916.enabled():                     # one citizen, on the server that issued its key
        return go("/settings", error="state")
    try:
        settings.set_f916_url(str(form.get("f916_url") or "")[:300])
    except ValueError:
        return go("/settings", error="server")
    try:
        await run_in_threadpool(f916.register, handle, settings.model_for("pi") or "unknown")
    except (FileExistsError, RuntimeError) as e:
        return page(_("Settings"), settings_body(request, _("1F916 did not join: {error}").format(error=e)))
    if loop_running():
        wake()
    return go("/settings", done="joined")

async def add_custom_channel(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    try:
        settings.add_custom_channel(str(form.get("name") or ""), str(form.get("kind") or ""),
                                    str(form.get("target") or "")[:500], str(form.get("format") or ""))
    except ValueError as e:
        return page(_("Settings"), settings_body(request, _("Channel not added: {error}.").format(error=e)))
    if loop_running():
        wake()
    return go("/settings", done="custom_added")

async def remove_custom_channel(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    settings.remove_custom_channel(str((await request.form()).get("id") or ""))
    return go("/settings", done="custom_removed")

async def save_bluesky(request: Request) -> Response:
    """Log in with the handle and app password first; only credentials Bluesky accepts are saved."""
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    if form.get("forget"):
        bluesky.forget_credentials()
        return go("/settings", done="bluesky_off")
    handle, password = str(form.get("handle") or "")[:253], str(form.get("app_password") or "")[:64]
    if not (handle.strip() and password.strip()):
        return go("/settings", error="bluesky")
    service = str(form.get("service") or "").strip()[:300]
    try:
        service = settings._http_address(service) if service else settings.bluesky_service()
    except ValueError:
        return go("/settings", error="server")
    try:
        await run_in_threadpool(bluesky.check_login, handle, password, service)
    except Exception as e:                 # wrong password, rate limit, network: nothing is saved
        return page(_("Settings"), settings_body(request, _("Bluesky did not accept that login: {error}").format(error=e)))
    bluesky.save_credentials(handle, password, service)
    if loop_running():
        wake()
    return go("/settings", done="bluesky_on")


async def github_action(request: Request) -> Response:
    """Each step of setting up GitHub Pages; only the sign-in itself happens on GitHub."""
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    do = str(form.get("do") or "")
    if do == "client_id":
        try:
            settings.set_github_client_id(str(form.get("client_id") or "")[:64])
        except ValueError:
            return go("/settings", error="github_app")
        return go("/settings", done="github_app_saved")
    if do == "connect":
        try:
            GITHUB_LOGIN.clear()
            GITHUB_LOGIN.update(await run_in_threadpool(github.start_login))
        except Exception as e:             # no OAuth app, device flow off, network
            return page(_("Settings"), settings_body(request, _("GitHub did not start the sign-in: {error}").format(error=e)))
        return go("/settings")
    if do == "cancel":
        GITHUB_LOGIN.clear()
        return go("/settings")
    if do == "forget":
        GITHUB_LOGIN.clear()
        github.forget()
        orchestrator.refresh_feed()        # the feeds lose the GitHub Pages address
        return go("/settings", done="github_off")
    if do == "unlink":
        github.unlink_site()
        orchestrator.refresh_feed()
        return go("/settings", done="github_unlinked")
    if do in ("create", "link") and github.connected():
        try:
            if do == "create":
                await run_in_threadpool(github.create_site, str(form.get("name") or "")[:100])
            else:
                await run_in_threadpool(github.link_site, str(form.get("repo") or "")[:300])
        except Exception as e:             # private, no access, holds other files, network
            return page(_("Settings"), settings_body(request, _("The site wasn't linked: {error}").format(error=e)))
        orchestrator.refresh_feed()        # the feeds use the site's address from now on
        try:
            await run_in_threadpool(github.sync, clock.now())   # files only; the loop records each article's link
        except Exception as e:
            return page(_("Settings"), settings_body(request, _("Linked, but the first push failed: {error}. The loop "
                                                                 "retries on each post.").format(error=e)))
        if loop_running():
            wake()
        return go("/settings", done="github_linked")
    return go("/settings", error="state")

async def github_poll(request: Request) -> JSONResponse:
    """The Settings page asks every few seconds whether the sign-in was approved on GitHub."""
    if not same_origin(request):
        return JSONResponse({"state": "error", "message": "Cross-site request refused."}, status_code=403)
    if github.connected() and not GITHUB_LOGIN:
        return JSONResponse({"state": "done"})
    if not GITHUB_LOGIN:
        return JSONResponse({"state": "error", "message": "No sign-in is in progress."})
    try:
        state = await run_in_threadpool(github.poll_login, dict(GITHUB_LOGIN))
    except github.LoginFailed as e:
        GITHUB_LOGIN.clear()
        return JSONResponse({"state": "error", "message": str(e).capitalize() + "."})
    except Exception as e:                 # network: keep polling
        return JSONResponse({"state": "pending", "message": str(e)})
    if state == "done":
        GITHUB_LOGIN.clear()
    return JSONResponse({"state": state})


# ---------- epics ----------

CLAIM_CHIP = {"confirmed": "claim: confirmed", "unconfirmed": "claim: unconfirmed"}
VERDICT_NAMES = {"refuted": "Refuted", "no_record": "No record", "untestable": "Untestable"}
VERDICT_HINTS = {"refuted": "a record shows the claim is wrong", "no_record": "nothing on the record could answer it",
                 "untestable": "no document could answer it as asked"}
SET_BY = {"code": "Code", "writer": "Writer", "rater": "Rater", "research": "Research", "theorist": "Theorist",
          "chat": "Researcher Chat", "human": "you", "auto mode": "auto mode"}

def claim_chip(slug: str) -> str:
    """The tiny claim tag on a card: ours only (the published site never shows it)."""
    t = store.get_claim_tag(slug)
    if not t:
        return ""
    title = escape(_("Set by {who}").format(who=_(SET_BY.get(t.by, t.by))) + (f": {t.why}" if t.why else ""), quote=True)
    return f'<span class="claim-tag {t.value}" title="{title}">{escape(_(CLAIM_CHIP[t.value]))}</span>'

def claim_section(m: Mission) -> str:
    """The mission page's claim tag: what it is, who set it and why, how it got there, and a way to change it."""
    t = store.get_claim_tag(m.slug)
    if not t and not claim_tags.taggable(m):
        return ""
    slug = quote(m.slug)
    options = "".join(f'<option value="{v}"{" selected" if t and t.value == v else ""}>{escape(_(CLAIM_CHIP[v]))}</option>'
                      for v in CLAIM_CHIP)
    who = (escape(_("Set by {who}").format(who=_(SET_BY.get(t.by, t.by))) + (f": {t.why}" if t.why else ""))
           if t else "")
    earlier = ""
    if t and t.history:
        rows = "".join(f'<li>{escape(_(CLAIM_CHIP.get(h.get("value"), h.get("value", ""))))} · '
                       f'{escape(_("Set by {who}").format(who=_(SET_BY.get(h.get("by"), h.get("by", "")))))}'
                       + (f': {escape(h.get("why", ""))}' if h.get("why") else "") + "</li>"
                       for h in reversed(t.history))
        earlier = f'<details><summary>{_("Earlier")}</summary><ul>{rows}</ul></details>'
    return (f'<div class="claim-row">{claim_chip(m.slug)}<span class="hint">{who}</span>'
            f'<form method="post" action="/m/{slug}/claim"><select name="tag" aria-label="{_("Claim tag")}">{options}</select>'
            f'<button>{_("Save")}</button></form></div>'
            f'<p class="hint">{_("Only you and the Theorist see this tag; it is never published.")}</p>{earlier}')

def dead_lead_section(m: Mission) -> str:
    """A dead lead's verdict, who said so and the records that sink it; or, for a mission that could still be one,
    a way to mark it."""
    d = store.get_dead_lead(m.slug)
    slug = quote(m.slug)
    if m.state == State.DEAD_LEAD and d:
        when = d.at[:10]
        by = _(SET_BY.get(d.by, d.by)) + (f" · {_(SET_BY.get(d.decided_by, d.decided_by))}"
                                          if d.decided_by and d.decided_by != d.by else "")
        sources = "".join(f"<li>{escape(x)}</li>" for x in d.sources)
        sources = f'<p class="hint" style="margin-bottom:.2rem">{_("Records that sink it")}</p><ul>{sources}</ul>' if sources else ""
        return ask("parked", f'{_(VERDICT_NAMES[d.verdict])}: {_(VERDICT_HINTS[d.verdict])}', "Dead lead",
                   f'<div class="sub">{escape(by)} · {escape(when)}</div><p>{escape(d.why)}</p>{sources}'
                   f'<p class="hint">{_("Revive: queue it again with fresh attempts.")}</p>',
                   f'<form method="post" action="/m/{slug}/requeue"><button>{_("Revive")}</button></form>')
    if m.state not in orchestrator.DEAD_ENDABLE or (d and d.status == "proposed"):
        return ""
    options = "".join(f'<option value="{v}">{escape(_(VERDICT_NAMES[v]))}: {escape(_(VERDICT_HINTS[v]))}</option>'
                      for v in VERDICT_NAMES)
    return (f'<details class="send-back"><summary>{_("Mark as dead lead")}</summary>'
            f'<form method="post" action="/m/{slug}/dead"><label for="verdict">{_("Verdict")}</label>'
            f'<select id="verdict" name="verdict">{options}</select>'
            f'<label for="why">{_("Why")}</label><textarea id="why" name="why" rows="2" required maxlength="300"></textarea>'
            f'<p><button>{_("Mark dead lead")}</button></p></form></details>')

def dead_lead_proposals() -> list[str]:
    """Dead leads the Theorist or Researcher Chat proposed, each waiting for the human to stop the mission or keep
    it going."""
    out = []
    for d in store.all_dead_leads("proposed"):
        m = store.get_mission(d.slug)
        if not m:
            continue
        who = (_("Researcher Chat proposes this is a dead lead.") if d.by == "chat"
               else _("Theorist proposes this is a dead lead."))
        out.append(ask(
            "propose", m.topic, "Dead lead",
            f'<div class="sub">{who} {escape(_(VERDICT_NAMES.get(d.verdict, d.verdict)))}: '
            f'{escape(_(VERDICT_HINTS.get(d.verdict, "")))} · {escape(label(m))}</div><p>{escape(d.why)}</p>',
            f'<form method="post" action="/dead-leads/{quote(d.slug)}/approve"><button class="primary">{_("Stop it")}</button></form>'
            f'<form method="post" action="/dead-leads/{quote(d.slug)}/reject"><button>{_("Keep going")}</button></form>',
            f"/m/{quote(d.slug)}"))
    return out

def mission_pill(m: Mission, epics: dict[str, Epic]) -> str:
    e = epics.get(m.epic)
    return epic_pill(e.title, proposed=e.status == "proposed") if e else ""

def epic_datalist() -> str:
    """Suggestions for every epic field: pick an approved epic, or type a new name."""
    options = "".join(f'<option value="{escape(e.title, quote=True)}">' for e in store.all_epics("active"))
    return f'<datalist id="epic-names">{options}</datalist>'

# ---------- anything waiting on a human: one look for every decision ----------

ASK_ACTION = {"review": "Review brief", "publish": "Review article", "parked": "See why"}

RATING_SHORT = {"logic": "claim and logic", "intrigue": "intrigue", "sources": "sources"}   # a review card's line

def rating_summary(r: ArticleRating) -> str:
    """ "Rated 24/30", or "Not rated: why", in the UI's language."""
    if r.error:
        return _("Not rated: {why}").format(why=r.error)
    return _("Rated {total}/{most}").format(total=r.total, most=RATING_SCALE * len(RATING_FACTORS))

def rating_line(r: ArticleRating) -> str:
    """One line for a review card: "Rated 24/30 by the Research Assistant (claim and logic 8, intrigue 8, sources 8)."."""
    if r.error:
        return rating_summary(r)
    return _("{rated} by the Research Assistant ({scores}).").format(
        rated=rating_summary(r), scores=", ".join(f"{_(name)} {getattr(r, key)}" for key, name in RATING_SHORT.items()))

def rating_html(r: ArticleRating) -> str:
    """The rating on a mission page: the total, then each factor's score and the rater's reason."""
    if r.error:
        return f'<div class="rating"><p class="muted">{escape(rating_summary(r))}. {_("It went to your review unrated.")}</p></div>'
    rows = "".join(f'<li><span class="score">{getattr(r, key)}/{RATING_SCALE}</span><strong>{escape(_(name))}</strong>'
                   f'{": " + escape(getattr(r, key + "_why")) if getattr(r, key + "_why").strip() else ""}</li>'
                   for key, name in RATING_FACTORS.items())
    lead = _("{rated} by the Research Assistant. {n} or less goes back to the writer.").format(
        rated=f"<strong>{escape(rating_summary(r))}</strong>", n=RATING_FAIL_AT)
    return f'<div class="rating"><p>{lead}</p><ul>{rows}</ul></div>'

def ask(color: str, title: str, badge: str, body: str, actions: str, link: str = "") -> str:
    """A decision waiting on the human, the same for a brief, an article, a parked mission or a proposed epic:
    the finger, what it is, a colored badge, what to know, and the buttons that decide it."""
    name = f'<a class="topic" href="{link}">{escape(title)}</a>' if link else f'<span class="topic">{escape(title)}</span>'
    badge = badge and _(badge)
    tag = f'<span class="badge {color}">{escape(badge)}</span>' if badge else ""
    return (f'<div class="ask {color}"><div class="ask-head">{icon("touch")}{name}{tag}</div>'
            f'{body}<div class="ask-actions">{actions}</div></div>')

def mission_ask(m: Mission, epics: dict[str, Epic]) -> str:
    color, href = tone(m), f"/m/{quote(m.slug)}"
    return ask(color, m.topic, label(m),
               f'<div class="sub">{mission_pill(m, epics)}{claim_chip(m.slug)}{escape(m.slug)} · {escape(next_step(m))}</div>',
               f'<a class="btn primary" href="{href}">{_(ASK_ACTION[color])}</a>', href)

def by_theorist(e: Epic, ms: list[Mission]) -> bool:
    """Whether the Theorist proposed epic e: a mission moved into it says so in its history ("the Theorist proposes
    the new epic ..."), whichever of its turns it came from."""
    said = f"the Theorist proposes the new epic {e.title}"
    return any(ev["note"].startswith(said) for m in ms for ev in store.events(m.slug))

def epic_proposals(missions: list[Mission]) -> list[str]:
    """Proposed epics (the finder's or the Theorist's), each waiting for the human to approve, rename, redirect
    or reject it."""
    active = store.all_epics("active")
    epics = {e.slug: e for e in store.all_epics()}
    out = []
    for e in store.all_epics("proposed"):
        ms = [m for m in missions if m.epic == e.slug]
        slug = quote(e.slug)
        items = "".join(f'<li><a href="/m/{quote(m.slug)}">{escape(m.topic)}</a>'
                        + (f' <span class="hint">· {_("if rejected: {epic}").format(epic=escape(epics[m.epic_fallback].title))}</span>'
                           if m.epic_fallback in epics else f' <span class="hint">· {_("if rejected: no epic")}</span>')
                        + "</li>" for m in ms)
        move = ""
        if active:
            options = "".join(f'<option value="{escape(a.slug, quote=True)}">{escape(a.title)}</option>' for a in active)
            move = (f'<form method="post" action="/epics/{slug}/merge"><select name="into" aria-label="{_("Existing epic")}">'
                    f'{options}</select><button>{_("Move to epic")}</button></form>')
        who = _("Proposed by Theorist") if by_theorist(e, ms) else _("Proposed by mission finder")
        out.append(ask(
            "propose", e.title, _("New epic"),
            f'<div class="sub">{who} · {_n("{n} mission", "{n} missions", len(ms))}</div><ul>{items}</ul>',
            f'<form method="post" action="/epics/{slug}/approve">'
            f'<input type="text" name="title" value="{escape(e.title, quote=True)}" maxlength="48" aria-label="{_("Epic name")}">'
            f'<button class="primary">{_("Approve epic")}</button></form>{move}'
            f'<form method="post" action="/epics/{slug}/reject"><button>{_("Reject")}</button></form>'))
    return out

def mission_link(slug: str) -> str:
    m = store.get_mission(slug)
    return f'<a href="/m/{quote(slug)}">{escape(m.topic if m else slug)}</a>'

def combine_proposals() -> list[str]:
    """The Theorist's proposed combines, each waiting for the human to approve or reject it."""
    out = []
    for c in store.all_combines("proposed"):
        items = "".join(f'<li>{mission_link(s)}'
                        + (f' <span class="hint">· {escape(label(m))}</span>' if (m := store.get_mission(s)) else "")
                        + "</li>" for s in c.members)
        epic = f'<div class="sub">{_("Epic")}: {escape(c.epic)}</div>' if c.epic else ""
        why = f'<div class="sub">{escape(c.why)}</div>' if c.why else ""
        out.append(ask(
            "propose", c.topic, _("Combine missions"),
            f'<div class="sub">{(_("Researcher Chat proposes combining {n} missions into one.") if c.by == "chat" else _("Theorist proposes combining {n} missions into one.")).format(n=len(c.members))}</div>'
            f'{epic}{why}<ul>{items}</ul>'
            f'<details><summary>{_("Objective")}</summary><p>{escape(c.objective)}</p></details>',
            f'<form method="post" action="/combines/{quote(c.slug)}/approve"><button class="primary">{_("Combine")}</button></form>'
            f'<form method="post" action="/combines/{quote(c.slug)}/reject"><button>{_("Keep separate")}</button></form>'))
    return out

def connections(m: Mission) -> str:
    """The mission page's Connections: what it was combined from or into, and its links, each with why and a way
    to remove it, plus a form to link it to another mission by hand."""
    slug, rows = quote(m.slug), []
    if m.combined_into:
        rows.append(f"<li>{_('Combined into {mission}').format(mission=mission_link(m.combined_into))}</li>")
    if m.combines:
        rows.append(f"<li>{_('Combines {missions}').format(missions=', '.join(mission_link(s) for s in m.combines))}</li>")
    for k in store.links_of(m.slug):
        other = store.get_mission(k.other(m.slug))
        state = f' <span class="hint">· {escape(label(other))}</span>' if other else ""
        who = {"theorist": _("Theorist"), "human": _("you"), "chat": _("Researcher Chat")}.get(k.by, k.by)
        why = f": {escape(k.why)}" if k.why else ""
        rows.append(f'<li class="link-row">{_("Linked to {mission}").format(mission=mission_link(k.other(m.slug)))}{state} '
                    f'<span class="hint">{_("by {who}").format(who=escape(who))}</span>{why}'
                    f'<form method="post" action="/m/{slug}/unlink" class="inline">'
                    f'<input type="hidden" name="other" value="{escape(k.other(m.slug), quote=True)}">'
                    f'<button>{_("Unlink")}</button></form></li>')
    form = ""
    if m.state != State.COMBINED:
        options = "".join(f'<option value="{escape(o.slug, quote=True)}">{escape(o.topic)}</option>'
                          for o in store.all_missions() if o.slug != m.slug and o.state != State.COMBINED
                          and (is_open(m) or is_open(o)) and not store.get_link(m.slug, o.slug))
        if options:
            form = (f'<details><summary>{_("Link to another mission")}</summary>'
                    f'<form method="post" action="/m/{slug}/link" class="actions">'
                    f'<label for="link-to" style="margin-top:0">{_("Mission")} <span class="hint">{_("open, or published")}'
                    f'</span></label><select id="link-to" name="to">{options}</select>'
                    f'<label for="link-why">{_("Why")} <span class="hint">{_("research sees this")}</span></label>'
                    f'<input type="text" id="link-why" name="why" maxlength="300">'
                    f'<p><button>{_("Link")}</button></p></form></details>')
    if not rows and not form:
        return ""
    body = f'<ul class="places">{"".join(rows)}</ul>' if rows else f'<p class="hint">{_("No connections.")}</p>'
    return heading("link", _("Connections"), len(rows) or None, cls="sec") + body + form

def epic_list(missions: list[Mission]) -> str:
    """Every approved epic: rename it, merge it into another (its missions move, it is gone), or delete it once it
    holds no missions."""
    active = store.all_epics("active")
    if not active:
        return ""
    rows = []
    for e in active:
        n, slug = sum(m.epic == e.slug for m in missions), quote(e.slug)
        others = "".join(f'<option value="{escape(a.slug, quote=True)}">{escape(a.title)}</option>'
                         for a in active if a.slug != e.slug)
        merge = (f'<form method="post" action="/epics/{slug}/merge" class="inline">'
                 f'<select name="into" aria-label="{_("Merge into")}">{others}</select>'
                 f'<button>{_("Merge into")}</button></form>') if n and others else ""
        delete = "" if n else (f'<form method="post" action="/epics/{slug}/delete" class="inline">'
                               f'<button>{_("Delete")}</button></form>')
        rows.append(
            f'<div class="epic-row"><form method="post" action="/epics/{slug}/rename" class="inline">'
            f'<input type="text" name="title" value="{escape(e.title, quote=True)}" maxlength="48" aria-label="{_("Epic name")}">'
            f'<span class="hint">{_n("{n} mission", "{n} missions", n)}</span>'
            f'<button>{_("Rename")}</button></form>{merge}{delete}</div>')
    return heading("layers", _("Epics"), len(active)) + f'<div class="epics">{"".join(rows)}</div>'

# In progress, grouped by where each mission is in the workflow, closest to published first (flow_rank).
STAGES = [("publishing", "send", "Publishing"), ("writing", "write", "Writing"), ("outreach", "people", "Outreach"),
          ("research", "search", "Research"),
          ("queued", "clock", "Queued")]

def stage_of(m: Mission) -> str:
    if m.state == State.QUEUED:
        return "queued"
    if m.state == State.RESEARCHING:
        return "research"
    if m.state in (State.ARTICLE_READY, State.DONE):  # a reviewed article, or one a human asked to publish again
        return "publishing"
    if m.state in (State.OUTREACH, State.AWAITING_COMMENTS):
        return "outreach"
    return "writing"                       # an approved brief, writing

def card(m: Mission, now: list[dict] | None = None, epics: dict[str, Epic] | None = None, lab: bool = False) -> str:
    epics = {e.slug: e for e in store.all_epics()} if epics is None else epics
    color = tone(m)
    working, idle = working_on(m, now), next_step(m)
    if m.state == State.DONE and m.published_at and not m.publish_requests:
        working, idle = "", _("Published {date}").format(date=f"{m.published_at:%b %-d, %Y}") + f" · {channel_names(m)}"
    sub = working or idle
    classes = " ".join(c for c in ("card", color, "active" if working else "") if c)
    live = next((e for e in now or [] if e["slug"] == m.slug), None) if lab else None
    stage = ""
    if live:                               # researchers only visit while a model works on it, mostly its worker's crew
        crew = settings.seat_worker(live["worker"])
        stage = ' data-lab="busy"' + (f' data-worker="{crew}"' if crew in settings.WORKERS else "")
    return (f'<a class="{classes}" href="/m/{quote(m.slug)}"{stage}><div class="row"><span class="topic">{escape(m.topic)}</span>'
            f'<span class="badge {color}">{escape(label(m))}</span></div>'
            f'<div class="sub">{mission_pill(m, epics)}{claim_chip(m.slug)}{escape(m.slug)}<span class="doing" data-idle="{escape(idle, quote=True)}">'
            f'{" · " + escape(sub) if sub else ""}</span>{board_link(m.slug)}</div></a>')

# ---------- the publishing queue ----------

def publish_queue(missions: list[Mission]) -> list[tuple[str, str, list[tuple[str, str, bool]]]]:
    """What waits to be published, per place in posting order: (key, name, [(slug, title, asked by a human)]).
    The local site gets reviewed articles and pages asked to be rewritten; each channel that is set up, its
    backlog as it will post it (orchestrator.queue). Places with nothing waiting are left out."""
    by_slug = {m.slug: m for m in missions}
    titles = {e["slug"]: e["title"] for e in publisher.entries()}
    site = [(m.slug, m.topic, False) for m in missions if m.state == State.ARTICLE_READY and m.reviewed]
    site += [(m.slug, titles.get(m.slug, m.topic), True) for m in missions
             if m.state == State.DONE and "site" in m.publish_requests]
    out = [("site", _("Local site"), site)] if site else []
    for c in live_channels():
        entries = [(e["slug"], e.get("title") or e["slug"], c.KEY in by_slug[e["slug"]].publish_requests)
                   for e in orchestrator.queue(c) if e["slug"] in by_slug]
        if entries:
            out.append((c.KEY, c.NAME, entries))
    return out

def queue_card(queued: list, count: int) -> str:
    """The Missions page's one card for everything waiting to be published; the queue is on /published."""
    places = " · ".join(f"{escape(name)} {len(entries)}" for _, name, entries in queued)
    return (f'<a class="card publish" href="/published#queue"><div class="row"><span class="topic">'
            f'{_n("{n} article queued", "{n} articles queued", count)}</span><span class="badge publish">{_("Publishing")}</span></div>'
            f'<div class="sub">{places}</div></a>')

def queue_section(queued: list) -> str:
    """The Published page's queue: each place, then its articles in the order they go out."""
    if not queued:
        return (heading("clock", _("Queue"), 0, "queue")
                + f'<p class="muted">{_("Nothing queued.")}</p>')
    parts = []
    for key, name, entries in queued:
        items = "".join(
            f'<li><a href="{f"#a-{escape(slug)}" if key != "site" or asked else f"/m/{quote(slug)}"}">{escape(title)}</a>'
            + (f' · <strong>{_("requested")}</strong>' if asked else "") + "</li>"
            for slug, title, asked in entries)
        parts.append(f'<h3 class="stage"><span>{escape(name)}</span><span class="n">{len(entries)}</span></h3>'
                     f'<ol class="queue">{items}</ol>')
    total = len({slug for _, _, entries in queued for slug, _, _ in entries})
    return heading("clock", _("Queue"), total, "queue") + "".join(parts)

# ---------- pages ----------

# ---------- first run: the setup card, Settings → Setup, web search, publication ----------

INSPIRATIONS = (                       # example lines for the setup card; a click puts one in the box
    "Who really decides what the Federal Reserve does?",
    "Where does the money for local ballot measures come from?",
    "What changed after the last big outage, and who paid for it?",
    "Which promises in this year's farm bill can be checked?",
)

async def can_start() -> bool:
    """Can the loop work right now? Records why not (doctor.waiting) so the page can say it."""
    ok, why = await run_in_threadpool(agents.ready)
    if ok:
        doctor.clear_waiting()
    else:
        doctor.set_waiting(why)
    return ok

def _mark(ok: bool) -> str:
    return (f'<span class="mark ok" title="{_("Done")}">{icon("check")}</span>' if ok
            else f'<span class="mark todo" title="{_("To do")}"></span>')

def _step(n: int, title: str, ok: bool, body: str) -> str:
    return (f'<li class="setup-step{" done" if ok else ""}"><div class="setup-head">{_mark(ok)}'
            f'<strong>{n}. {escape(title)}</strong></div><div class="setup-body">{body}</div></li>')

def _open_link(link: str) -> str:
    if not link:
        return ""
    new_tab = ' target="_blank" rel="noopener"' if link.startswith("http") else ""
    return f' <a href="{escape(link)}"{new_tab}>{_("Open")}</a>'

def _fixes(found: list) -> str:
    return "".join(f'<p class="hint">{escape(c.detail)} {escape(c.fix)}{_open_link(c.link)}</p>' for c in found if not c.ok)

def setup_card(found: list | None = None) -> str:
    """The home page's first run: connect a model, web search, something to work on, start. Shown until every
    check passes or the human hides it; it never blocks anything (Settings → Setup brings it back)."""
    if settings.setup_dismissed():
        return ""
    conn = {"ok": True, "models": []} if doctor.stubs() else llm.connection()
    found = found if found is not None else doctor.checks(conn=conn)
    if doctor.done([c for c in found if c.key != "search_up"]):   # an outage is the top bar's to say, not setup's
        return ""
    by = {c.key: c for c in found}
    model_checks = [c for c in found if c.key in ("models", "connection")]
    models_ok = all(c.ok for c in model_checks)
    if models_ok:
        body = f'<p class="hint">{escape(by["models"].detail)}.</p>'
    else:
        body = _fixes(model_checks)
        if settings.missing_models() and conn["ok"] and conn["models"]:
            body += ('<form method="post" action="/settings/ollama-connect" class="inline"><input type="hidden" name="back" value="/">'
                     f'<button class="primary">{_("Connect Ollama")}</button><span class="hint">'
                     f'{_n("gives each worker one of your {n} model", "gives each worker one of your {n} models", len(conn["models"]))}'
                     f'</span></form>')
        elif settings.missing_models() and not conn["ok"]:
            body += (f'<p><a class="btn" href="{doctor.OLLAMA_DOWNLOAD}" target="_blank" rel="noopener">{_("Get Ollama")}</a> '
                     f'<span class="hint">{_("free, runs on this computer. Pull a model, then come back.")}</span></p>')
        cloud = _("Or use a cloud model: {link}.").format(link=f'<a href="/settings#providers">{_("add an API key")}</a>')
        body += f'<p class="hint">{cloud}</p>'
    tool_checks = [c for c in found if c.key in ("search", "uvx")]
    tools_ok = all(c.ok for c in tool_checks)
    tools = (f'<p class="hint">{_("Research can search the web and read pages.")}</p>' if tools_ok else
             f'<p class="hint">{_("Research searches the web with DuckDuckGo for free, or with a source you add a key for.")}</p>'
             + _fixes(tool_checks))
    work_ok = by["work"].ok
    chips = "".join(f'<button type="button" class="chip" data-line="{escape(line, quote=True)}">{escape(line)}</button>'
                    for line in INSPIRATIONS)
    work = (f'<p class="hint">{escape(by["work"].detail)}</p>' if work_ok else
            f'<p class="hint">{_("Give the mission finder a line to explore, or add a mission yourself.")}</p>'
            f'<form method="post" action="/inspiration" class="inline setup-form">'
            f'<input type="text" id="setup-line" name="inspiration" maxlength="500" '
            f'placeholder="{escape(_("A question worth digging into"), quote=True)}" '
            f'aria-label="{escape(_("Inspiration line"), quote=True)}">'
            f'<button>{_("Save inspiration")}</button></form><div class="chips">{chips}</div>'
            f'<p><button type="button" class="btn" onclick="document.getElementById(\'add-mission\').click()">'
            f'{icon("plus")}{_("Add a mission instead")}</button></p>')
    can = all(c.ok for c in found if c.blocks_loop)
    start = (f'<p class="hint">{_("Press play at the top. Briefs and articles wait for your review below.")}</p>' if can else
             f'<p class="hint">{_("Play works once a model is connected.")}</p>')
    steps = (_step(1, _("Connect a model"), models_ok, body) + _step(2, _("Web search"), tools_ok, tools)
             + _step(3, _("Something to work on"), work_ok, work) + _step(4, _("Start the loop"), loop_running(), start))
    left = sum(not ok for ok in (models_ok, tools_ok, work_ok))
    return (f'<section class="setup actions" id="setup" aria-label="{_("Setup")}">'
            f'<div class="setup-top"><h2>{icon("compass")}{_("Get started")}</h2>'
            f'<form method="post" action="/setup/dismiss"><button class="link" '
            f'title="{escape(_("Hide setup. Settings → Setup brings it back."), quote=True)}">{_("Hide")}</button></form></div>'
            f'<p class="hint">{_n("{n} step left. None of it is needed to look around.", "{n} steps left. None of it is needed to look around.", left)}</p>'
            f'<ol class="setup-steps">{steps}</ol>'
            f'<p class="setup-more"><a href="/about" target="_blank">{_("How it works")}</a> · '
            f'<button type="button" class="link" data-ask="{escape(_("How do I set up Mission Loop, and what is left?"), quote=True)}">'
            f'{icon("message")}{_("Ask Researcher Chat")}</button></p></section>{SETUP_JS}')

SETUP_JS = """<script>
document.addEventListener("click", e => {
  const chip = e.target.closest(".chip[data-line]");
  if (!chip) return;
  const box = document.getElementById("setup-line");
  if (box) { box.value = chip.dataset.line; box.focus(); }
});
</script>"""

def setup_section(conn: dict) -> str:
    """Settings → Setup: every check with what to do, and the way back to the home page's setup card."""
    found = doctor.checks(conn=conn, probe=True)
    rows = "".join(f'<li class="{"ok" if c.ok else "todo"}">{_mark(c.ok)}<span><strong>{escape(c.title)}</strong>: '
                   f'{escape(c.detail)}' + (f' <span class="hint">{escape(c.fix)}</span>' if c.fix else "")
                   + (_open_link(c.link) if not c.ok else "") + "</span></li>" for c in found)
    waiting = doctor.waiting() if loop_running() else None
    note = (f'<p class="hint"><strong>{_("Loop waiting:")}</strong> {escape(waiting["reason"])}.</p>' if waiting else "")
    again = (f'<form method="post" action="/setup/show" class="inline"><button>{_("Show setup on the Missions page")}</button></form>'
             if settings.setup_dismissed() and not doctor.done(found) else "")
    summary = (_("Everything is set up.") if doctor.done(found) else
               _("The loop can start. The rest helps it work.") if all(c.ok for c in found if c.blocks_loop) else
               _("The loop can't start until a model is connected."))
    cli = _("Same checks: {command}.").format(command="<code>python run.py doctor</code>")
    return (f'<div class="actions"><p>{summary} {cli}</p>'
            f'{note}<ul class="checks">{rows}</ul>{again}</div>')

def search_section() -> str:
    """Settings → Web search: every source in the order searches try them, each with on/off, up/down, its key and how
    it is doing. DuckDuckGo and GDELT need no key, so searching works on a fresh clone."""
    rows = search_sources.status()
    items = []
    for i, src in enumerate(rows):
        sid = src["id"]
        if src["resting"]:
            state = f'<span class="src-state rest">{escape(_("Resting: {reason}").format(reason=src["resting"]))}</span>'
        elif src["on"]:
            state = f'<span class="src-state on">{_("On")}</span>'
        elif src["off"]:
            state = f'<span class="src-state">{_("Off")}</span>'
        else:
            state = f'<span class="src-state">{_("Needs a key")}</span>'
        tags = "" if src["keyed"] else f' <span class="src-tag">{_("no key")}</span>'
        if src["kinds"] == ("news",):
            tags += f' <span class="src-tag">{_("news only")}</span>'
        hidden = f'<input type="hidden" name="source" value="{sid}">'
        move = "".join(f'<form method="post" action="/settings/search/move" class="inline">{hidden}'
                       f'<input type="hidden" name="step" value="{step}"><button title="{escape(label, quote=True)}" '
                       f'aria-label="{escape(label, quote=True)}"{" disabled" if off_end else ""}>{arrow}</button></form>'
                       for step, arrow, label, off_end in ((-1, "↑", _("Move up"), i == 0),
                                                           (1, "↓", _("Move down"), i == len(rows) - 1)))
        toggle = (f'<form method="post" action="/settings/search/toggle" class="inline">{hidden}'
                  f'<input type="hidden" name="on" value="{"" if not src["off"] else "1"}">'
                  f'<button>{_("Switch on") if src["off"] else _("Switch off")}</button></form>')
        key = ""
        if src["keyed"]:
            if src["saved"]:
                key = (f'<span class="hint">{_("Key saved in {file}.").format(file="<code>data/search.json</code>")}</span>'
                       f'<form method="post" action="/settings/search" class="inline">{hidden}'
                       f'<input type="hidden" name="remove" value="1"><button>{_("Remove key")}</button></form>')
            else:
                label = escape(_("{name} API key").format(name=src["name"]), quote=True)
                env = (f'<span class="hint">{_("Key set in {file}. Paste one here to use it instead.").format(file="<code>.env</code>")}</span>'
                       if src["key"] else "")
                key = (f'{env}<form method="post" action="/settings/search" class="inline">{hidden}'
                       f'<input type="password" name="key" autocomplete="off" placeholder="{label}" aria-label="{label}" required>'
                       f'<button>{_("Check and save")}</button></form>'
                       f'<a href="{escape(src["keys"], quote=True)}" target="_blank" rel="noopener">{_("Get a key")}</a>')
        items.append(f'<li class="src"><div class="src-head"><span class="src-n">{i + 1}</span><strong>{escape(src["name"])}</strong>'
                     f'{tags} {state}<span class="src-ctl">{move}{toggle}</span></div>'
                     f'<p class="hint">{escape(_(src["blurb"]))}</p><div class="src-key">{key}</div></li>')
    down = search_status.current()
    note = (f'<p class="hint"><strong>{_("Web search is down: {reason}.").format(reason=escape(down["reason"]))}</strong> '
            f'{escape(_(down.get("fix") or ""))}</p>' if down else "")
    intro = _("Each search tries these in order and moves to the next when one is out of credit, refuses its key, "
              "is rate-limited or can't be reached. Keys stay on this machine.")
    return (f'{part("search", _("Web search"), "search")}<div class="actions"><p class="hint">{intro}</p>{note}'
            f'<ol class="sources">{"".join(items)}</ol>{toolbox_status()}</div>')

def toolbox_status() -> str:
    from agents import toolbox
    found = toolbox.server_status(["fetch"])
    bad = [f"{name}: {why}" for name, why in found.items() if why and "not installed" in why]
    install = _("Install with {command}.").format(command="<code>brew install uv</code>")
    return f'<p class="hint">{escape("; ".join(bad))}. {install}</p>' if bad else ""

MCP_COPY_JS = """<script>
(() => {
const t = s => (window.I18N || {})[s] || s;
document.querySelectorAll(".mcp [data-copy]").forEach(b => b.onclick = async () => {
  const src = document.getElementById(b.dataset.copy), text = src.value ?? src.textContent;
  try { await navigator.clipboard.writeText(text); b.textContent = t("Copied"); }
  catch { src.select?.(); b.textContent = t("Press Ctrl/Cmd-C"); }
  setTimeout(() => b.textContent = t("Copy"), 1600);
});
document.querySelectorAll(".mcp [data-show]").forEach(b => b.onclick = () => {
  const f = document.getElementById(b.dataset.show), hidden = f.type === "password";
  f.type = hidden ? "text" : "password"; b.textContent = t(hidden ? "Hide" : "Show");
});
})();
</script>"""

def mcp_setups(address: str, token: str) -> list[tuple[str, str, str]]:
    """Ready-to-paste setups for the apps people connect most: (id, name and where it goes, text)."""
    name = mcp_share.NAME
    entry = {"type": "http", "url": address, "headers": {"Authorization": f"Bearer {token}"}}
    return [
        ("claude-code", _("Claude Code: run in a terminal"),
         f'claude mcp add --transport http {name} {address} --header "Authorization: Bearer {token}"'),
        ("json", _("Cursor, VS Code, LM Studio, Windsurf and most others: add to mcp.json"),
         json.dumps({"mcpServers": {name: entry}}, indent=2)),
        ("desktop", _("Claude Desktop: add to claude_desktop_config.json (needs Node)"),
         json.dumps({"mcpServers": {name: {"command": "npx", "args": [
             "-y", "mcp-remote", address, "--allow-http", "--header", f"Authorization:Bearer {token}"]}}}, indent=2)),
    ]

def mcp_section() -> str:
    """Settings -> MCP server: share every tool Researcher Chat has with other apps on this network (mcp_share.py)."""
    st = mcp_share.status()
    on = settings.mcp_share()
    intro = (f'<p class="hint">{_("Lets other apps on your network use every tool Researcher Chat has: missions, epics, briefs, the board, the library and web research.")} '
             f'{_("Only devices on this network get in, and each needs the token. Nothing goes through the cloud. Approving and publishing stay here.")}</p>')
    switch = (f'<form method="post" action="/settings/mcp"><input type="hidden" name="do" value="{"off" if on else "on"}">'
              f'<label class="switch"><input type="checkbox" role="switch"{" checked" if on else ""} '
              f'onchange="this.form.requestSubmit()">{_("Share on this network")}</label>'
              f'<noscript><p><button>{_("Stop sharing") if on else _("Share")}</button></p></noscript></form>')
    if not on:
        return f'<div class="actions mcp">{intro}{switch}</div>'
    if st["starting"]:
        state = f'<p class="hint">{_("Starting… refresh in a moment.")}</p>'
    elif not st["running"]:
        why = st["error"] or _("it runs only while this app is open")
        state = (f'<p class="warn">{icon("alert")}{_("On, but not running: {why}.").format(why=escape(why))}</p>'
                 f'<form method="post" action="/settings/mcp" class="inline"><input type="hidden" name="do" value="on">'
                 f'<button>{_("Try again")}</button></form>')
    else:
        servers = {}
        for server, tool in st["tools"]:
            servers.setdefault(server, []).append(tool)
        rows = "".join(f'<li><strong>{escape(s)}</strong> <span class="hint">{escape(", ".join(sorted(ts)))}</span></li>'
                       for s, ts in servers.items())
        not_shared = _("not shared: {why}")
        skipped = "".join(f'<li><strong>{escape(s)}</strong> <span class="hint">{not_shared.format(why=escape(why))}</span></li>'
                          for s, why in st["skipped"].items())
        search = ("" if "search" in servers or "search" in st["skipped"] else
                  f'<li><strong>search</strong> <span class="hint">{not_shared.format(why=_("no web search source is on (switch one on under Research)"))}</span></li>')
        sharing = _("Sharing {n} tools on port {port}.").format(n=len(st["tools"]), port=st["port"])
        state = (f'<p>{icon("check")}{sharing}</p>'
                 f'<details><summary>{_("The tools")}</summary><ul class="mcp-tools">{rows}{skipped}{search}</ul></details>')
    address, token = mcp_share.url(), settings.mcp_token()
    local = mcp_share.url("127.0.0.1")
    firewall = _("If another device can't connect, allow incoming connections for Python in System Settings → Network → Firewall.")
    setups = "".join(
        f'<details{" open" if n == 0 else ""}><summary>{escape(title)}</summary>'
        f'<pre class="mcp-snippet" id="mcp-{sid}">{escape(text)}</pre>'
        f'<p><button type="button" data-copy="mcp-{sid}">{_("Copy")}</button></p></details>'
        for n, (sid, title, text) in enumerate(mcp_setups(address, token)))
    connect = (f'<label for="mcp-url">{_("Address")}</label><div class="inline">'
               f'<input type="text" id="mcp-url" value="{escape(address, quote=True)}" readonly>'
               f'<button type="button" data-copy="mcp-url">{_("Copy")}</button></div>'
               f'<p class="hint">{_("On this computer, {address} works too.").format(address=f"<code>{escape(local)}</code>")} '
               f'{firewall}</p>'
               f'<label for="mcp-token">{_("Token")}</label><div class="inline">'
               f'<input type="password" id="mcp-token" value="{escape(token, quote=True)}" readonly autocomplete="off">'
               f'<button type="button" data-show="mcp-token">{_("Show")}</button>'
               f'<button type="button" data-copy="mcp-token">{_("Copy")}</button></div>'
               f'<p class="hint">{_("Apps send it as {header}. Keep it like a password: anyone on your network with it can use the tools.").format(header="<code>Authorization: Bearer &lt;token&gt;</code>")}</p>'
               f'<form method="post" action="/settings/mcp" class="inline"><input type="hidden" name="do" value="token">'
               f'<button>{_("New token")}</button><span class="hint">{_("Apps with the old one stop working.")}</span></form>')
    port = (f'<form method="post" action="/settings/mcp" class="inline"><input type="hidden" name="do" value="port">'
            f'<label for="mcp-port" style="margin:0">{_("Port")}</label>'
            f'<input type="number" id="mcp-port" name="port" value="{settings.mcp_port()}" min="1024" max="65535" '
            f'style="width:6rem"><button>{_("Save")}</button></form>')
    return (f'<div class="actions mcp">{intro}{switch}{state}{connect}'
            f'{part("plug", _("Connect an app"))}{setups}{port}</div>{MCP_COPY_JS}')

async def save_mcp(request: Request) -> Response:
    """Settings -> MCP server: switch sharing on or off, make a new token, or change the port."""
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    do = str(form.get("do") or "")
    back = "/settings#mcp"
    if do == "off":
        settings.set_mcp_share(False)
        await run_in_threadpool(mcp_share.stop)
        return go(back, done="mcp_off")
    if do == "token":
        settings.new_mcp_token()
        return go(back, done="mcp_token")
    if do == "port":
        try:
            settings.set_mcp_port(int(str(form.get("port") or "")))
        except ValueError:
            return go(back, error="mcp_port")
        if settings.mcp_share():
            await run_in_threadpool(mcp_share.stop)
            if await run_in_threadpool(mcp_share.start):
                return go(back, error="mcp_start")
        return go(back, done="mcp_port")
    settings.set_mcp_share(True)
    settings.mcp_token()                   # made now, so the page has one to show
    if await run_in_threadpool(mcp_share.start):
        return go(back, error="mcp_start")
    return go(back, done="mcp_on")

def publication_section() -> str:
    name, tag, about_text = settings.publication_name(), settings.tagline(), settings.about_statement()
    return (f'<form method="post" action="/settings/publication" class="actions">'
            f'<p class="hint">{_("Name, tagline and About text for the published site and feeds. Saving redraws every page.")}</p>'
            f'<label for="pub-name">{_("Name")}</label><input type="text" id="pub-name" name="name" maxlength="{settings.NAME_MAX}" '
            f'value="{escape(name, quote=True)}" placeholder="{settings.PUBLICATION_DEFAULT}">'
            f'<label for="pub-tagline">{_("Tagline")} <span class="hint">{_("optional, shown under the name")}</span></label>'
            f'<input type="text" id="pub-tagline" name="tagline" maxlength="{settings.TAGLINE_MAX}" value="{escape(tag, quote=True)}">'
            f'<label for="pub-about">{_("About statement")} <span class="hint">'
            f'{_("optional, top of the About page. A blank line starts a paragraph. Empty: the built-in text.")}</span></label>'
            f'<textarea id="pub-about" name="about" rows="5" maxlength="{settings.ABOUT_MAX}">{escape(about_text)}</textarea>'
            f'<p><button>{_("Save publication")}</button></p></form>')

OUTREACH_HOW = {"email": "an email to an address found on a page",
                "bluesky": "a post that mentions them, from the Bluesky account under Publishing",
                "1f916": "a reply to their comment on one of our posts"}
OUTREACH_SETUP = {"email": "set up email below", "bluesky": "add Bluesky under Publishing",
                  "1f916": "join 1F916 under Publishing"}

def outreach_settings() -> str:
    """Settings → Outreach: the switch, the reply wait, how many people a mission asks, which channels, and email."""
    on = " checked" if settings.outreach_on() else ""
    ticked = settings.outreach_channels()
    boxes = []
    for key, name in settings.OUTREACH_CHANNELS.items():
        state = "" if contacts.set_up(key) else f' <span class="hint">({_("not set up")}: {_(OUTREACH_SETUP[key])})</span>'
        boxes.append(f'<label class="tick out-channel"><input type="checkbox" name="channels" value="{key}"'
                     f'{" checked" if key in ticked else ""}><span><strong>{name}</strong>: '
                     f'{_(OUTREACH_HOW[key])}{state}</span></label>')
    warn = ""
    if settings.outreach_on() and not contacts.ready_channels():
        warn = f'<p class="hint"><strong>{_("Outreach is on, but no ticked channel is set up. Missions skip it.")}</strong></p>'
    elif settings.outreach_on() and not settings.site_url():
        warn = f'<p class="hint">{_("No site address, so messages leave out the link to the site.")}</p>'
    sample = escape(contacts.message("Jane", "the 2024 Texas grid outage",
                                     "Did ERCOT order the shutdown before the freeze warning?"))
    form = f"""<form method="post" action="/settings/outreach" class="actions">
  <p class="hint">{_("After a brief is approved, the Outreach agent asks a few people one question each. It picks the person, channel and question; code writes the message:")}</p>
  <blockquote lang="en">{sample}</blockquote>
  <label class="switch"><input type="checkbox" role="switch" name="on" value="on"{on}> {_("Outreach")}</label>
  <label for="out-wait">{_("Wait for replies")} <span class="hint">{_("hours; writing starts sooner if everyone answers")}</span></label>
  <input type="number" id="out-wait" name="wait" min="0" max="{settings.OUTREACH_WAIT_MAX}" step="1"
    value="{settings.outreach_wait_hours():g}" style="width:6rem">
  <label for="out-n">{_("People per mission")}</label>
  <input type="number" id="out-n" name="contacts" min="1" max="{settings.OUTREACH_CONTACTS_MAX}" step="1"
    value="{settings.outreach_contacts()}" style="width:5rem">
  <fieldset class="channels"><legend>{_("Channels")}</legend>{"".join(boxes)}</fieldset>
  <p class="hint">{_("Nobody is asked twice about a mission, or more than once a week.")}</p>
  <p><button>{_("Save outreach")}</button></p>
</form>"""
    return warn + form + email_settings()

def email_settings() -> str:
    cfg = settings.email_config()
    if cfg:
        sender = escape(f'{cfg["from_name"] or settings.publication_name()} <{cfg["from_address"]}>')
        state = ("<p>" + _("Sending as {sender} through {server}.").format(
                     sender=f"<strong>{sender}</strong>", server=escape(f'{cfg["smtp_host"]}:{cfg["smtp_port"]}')) + " "
                 + (_("Replies are read from {server}.").format(server=escape(cfg["imap_host"])) if cfg["imap_host"]
                    else _("Replies aren't read (no IMAP server).")) + "</p>"
                 f'<form method="post" action="/settings/email" class="inline"><input type="hidden" name="forget" value="1">'
                 f'<button>{_("Remove email")}</button></form>')
    else:
        state = f"<p>{_('Not set up.')}</p>"
    val = lambda k, d="": escape(str(cfg.get(k) or d), quote=True)
    keep = _("blank keeps the saved one") if cfg else ""
    return f"""{part("message", _("Email"), "email")}<div class="actions">{state}
<details{"" if cfg else " open"}><summary>{_("Change email") if cfg else _("Set up email")}</summary>
<p class="hint">{_("Use an app password, not your account password. It is checked by logging in, then saved in {file}. Gmail: smtp.gmail.com, port 587, imap.gmail.com.").format(file="<code>data/email.json</code>")}</p>
<form method="post" action="/settings/email">
  <label for="em-smtp">{_("SMTP server")}</label><input type="text" id="em-smtp" name="smtp_host" required maxlength="253" value="{val("smtp_host")}" placeholder="smtp.gmail.com">
  <label for="em-port">{_("SMTP port")} <span class="hint">587 (STARTTLS) / 465 (SSL)</span></label>
  <input type="number" id="em-port" name="smtp_port" min="1" max="65535" value="{val("smtp_port", 587)}" style="width:6rem">
  <label for="em-imap">{_("IMAP server")} <span class="hint">{_("optional: to read replies")}</span></label>
  <input type="text" id="em-imap" name="imap_host" maxlength="253" value="{val("imap_host")}" placeholder="imap.gmail.com">
  <label for="em-user">{_("Username")}</label><input type="text" id="em-user" name="username" required maxlength="253" autocomplete="username" value="{val("username")}">
  <label for="em-pass">{_("App password")} <span class="hint">{keep}</span></label>
  <input type="password" id="em-pass" name="password" maxlength="200" autocomplete="off"{"" if cfg else " required"}>
  <label for="em-name">{_("From name")} <span class="hint">{_("optional: else the publication's name")}</span></label>
  <input type="text" id="em-name" name="from_name" maxlength="60" value="{val("from_name")}" placeholder="{escape(settings.publication_name(), quote=True)}">
  <label for="em-from">{_("From address")} <span class="hint">{_("optional: else the username")}</span></label>
  <input type="email" id="em-from" name="from_address" maxlength="253" value="{val("from_address")}">
  <p><button>{_("Check and save email")}</button></p>
</form></details></div>"""

async def save_outreach(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    try:
        settings.set_outreach(form.get("on") == "on", float(str(form.get("wait") or settings.OUTREACH_WAIT)),
                              int(str(form.get("contacts") or settings.OUTREACH_CONTACTS)),
                              [str(c) for c in form.getlist("channels")])
    except ValueError:
        return go("/settings#outreach", error="outreach")
    return go("/settings#outreach", done="outreach_saved")

async def save_email(request: Request) -> Response:
    """Log in first; only a login the mail server accepts is saved."""
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    if form.get("forget"):
        settings.forget_email_config()
        return go("/settings#outreach", done="email_off")
    f = lambda k, n=253: str(form.get(k) or "")[:n]
    try:
        cfg = settings.clean_email_config(f("smtp_host"), f("smtp_port", 6), f("imap_host"), f("username"),
                                          f("password", 200), f("from_name", 60), f("from_address"))
        await run_in_threadpool(contacts.check_email, cfg)
    except ValueError as e:
        return page(_("Settings"), settings_body(request, _("Email wasn't saved: {why}").format(why=e)))
    settings.save_email_config(cfg)
    return go("/settings#outreach", done="email_saved")

def _search_source(form) -> str:
    source = str(form.get("source") or "brave")
    return source if source in settings.SEARCH_SOURCES else ""

async def save_search(request: Request) -> Response:
    """Save or remove a search source's key, after one test search with it."""
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    back = _back(form) if form.get("back") else "/settings#search"
    source = _search_source(form)
    if not source or not settings.SEARCH_SOURCES[source]["key_env"]:
        return go(back, error="search_format")
    if form.get("remove"):
        settings.forget_search_key(source)
        await run_in_threadpool(mcp_share.restart)   # what is shared follows the sources that are on
        return go(back, done="search_off")
    key = str(form.get("key") or "").strip()
    if not key or len(key) > 200 or any(c.isspace() for c in key):
        return go(back, error="search_format")
    ok, note = await run_in_threadpool(doctor.check_search_key, key, source)
    if not ok:
        return go(back, error="search_key")
    settings.save_search_key(key, source)
    await run_in_threadpool(mcp_share.restart)
    return go(back, done="search_unchecked" if note else "search_saved")

async def move_search(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    if source := _search_source(form):
        settings.move_search_source(source, -1 if str(form.get("step")) == "-1" else 1)
    return go("/settings#search", done="search_order")

async def toggle_search(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    if source := _search_source(form):
        on = bool(form.get("on"))
        settings.set_search_on(source, on)
        if on:
            search_sources.wake(source)
        await run_in_threadpool(mcp_share.restart)
    return go("/settings#search", done="search_order")

async def save_publication(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    try:
        settings.set_publication(str(form.get("name") or "")[:500], str(form.get("tagline") or "")[:500],
                                 str(form.get("about") or "")[:5000])
    except ValueError:
        return go("/settings#publication", error="publication")
    await run_in_threadpool(orchestrator.refresh_site)
    return go("/settings#publication", done="publication_saved")

async def setup_action(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    action = request.path_params["action"]
    if action == "dismiss":
        settings.set_setup_dismissed(True)
        return go("/", done="setup_hidden")
    if action == "show":
        settings.set_setup_dismissed(False)
        return go("/", done="setup_shown")
    return PlainTextResponse("Unknown action.", status_code=404)

async def about(request: Request) -> Response:
    """The About page lives on the published site; make sure it is there (a fresh clone has published nothing)."""
    if not (publisher.SITE / "about" / "index.html").exists():
        await run_in_threadpool(publisher.rebuild_index)
    return RedirectResponse("/site/about/", status_code=303)

async def home(request: Request) -> HTMLResponse:
    """The flow chart read from its end: what needs you, then what is in progress closest to published first,
    grouped by stage; then the inputs (inspiration, epics). Adding a mission is one slim card right under the
    strip that opens a wizard (new_mission). Published articles live on /published:
    the top bar's Published button, and a card at the very bottom for anyone who scrolls looking for it. What waits
    to be published is one card under In progress that leads there; the queue itself is on that page."""
    missions = store.all_missions()
    flow = lambda ms: sorted(ms, key=flow_rank)   # stable: within a step, oldest first
    now = running_now()
    epics = {e.slug: e for e in store.all_epics()}
    lab = settings.animations()
    setup = await run_in_threadpool(setup_card)  # it asks Ollama, which may be slow or off: never on the event loop
    body = [flash(request), setup, loop_panel("/"), new_mission()]

    # Needs you: every decision in one place and one look, closest to published first; new epics before parked
    asks = [(flow_rank(m), mission_ask(m, epics)) for m in flow(m for m in missions if needs_you(m))]
    asks += [(6, a) for a in epic_proposals(missions) + combine_proposals() + dead_lead_proposals()]
    body.append(heading("touch", _("Needs you"), len(asks), "needs-you"))
    body.append("".join(a for _rank, a in sorted(asks, key=lambda a: a[0]))
                or f'<p class="muted empty">{_("Nothing needs you.")}</p>')

    # In progress, by stage. assets/flow.js relights cards in place; researchers may only visit data-lab-ok groups.
    going = flow(m for m in missions if in_progress(m) and stage_of(m) != "publishing")
    queued = publish_queue(missions)
    waiting = {slug for _, _, entries in queued for slug, _, _ in entries}
    body.append(heading("activity", _("In progress"), len(going) + len(waiting)))
    if not going and not waiting:
        body.append(f'<p class="muted empty">{_("Nothing in progress.")}</p>')
    for key, name, title in STAGES:
        title = _(title)
        if key == "publishing":
            if waiting:
                body.append(f'<h3 class="stage">{icon(name)}<span>{title}</span><span class="n">{len(waiting)}</span></h3>'
                            f'<div class="cards">{queue_card(queued, len(waiting))}</div>')
            continue
        ms = [m for m in going if stage_of(m) == key]
        if ms:
            body.append(f'<h3 class="stage">{icon(name)}<span>{title}</span><span class="n">{len(ms)}</span></h3>'
                        f'<div class="cards"{" data-lab-ok" if lab else ""}>'
                        + "".join(card(m, now, epics, lab) for m in ms) + "</div>")

    body.append(finder_panel(now, lab))
    body.append(epic_list(missions))
    tail = log_tail()
    if tail:
        body.append(f'<details><summary>{icon("terminal")}{_("Loop log")}</summary><pre class="notes">'
                    + escape("\n".join(tail)) + "</pre></details>")
    done = sum(m.state == State.DONE for m in missions)   # the very last thing, for anyone looking for it
    body.append(f'<a class="card site-link" href="/published"><div class="row"><span class="topic">{ICON_SITE}{_("Published")}</span>'
                f'<span class="badge">{_n("{n} article", "{n} articles", done)}</span></div>'
                f'<div class="sub">{_("Where each article went, and republish")}</div></a>')
    if lab:
        body.append(script("researchers.js"))
    return page("Mission Loop", "\n".join(body), poll=True)

def new_mission() -> str:
    """A long slim card right under the pipeline strip; clicking it opens a three-step wizard (topic, objective,
    epic and short name) in a dialog that posts to /missions like the CLI's add. The page's own refresh holds off
    while it is open."""
    steps = [
        (_("Topic"), f"""<label for="topic">{_("Topic")} <span class="hint">{_("one line")}</span></label>
  <input type="text" id="topic" name="topic" required placeholder="{escape(_("e.g. Did the new transit levy pass, and who paid for the campaign?"), quote=True)}">"""),
        (_("Objective"), f"""<label for="objective">{_("Objective")} <span class="hint">{_("optional: the question, what counts as evidence, a hypothesis")}</span></label>
  <textarea id="objective" name="objective" rows="5"></textarea>"""),
        (_("Epic"), f"""<label for="epic">{_("Epic")} <span class="hint">{_("optional, pick or type 1–4 words")}</span></label>
  <input type="text" id="epic" name="epic" list="epic-names" maxlength="48" placeholder="{escape(_("e.g. Fed Policy Capture"), quote=True)}">
  <label for="slug">{_("Short name")} <span class="hint">{_("optional, made from the topic")}</span></label>
  <input type="text" id="slug" name="slug" placeholder="transit-levy">
  <p class="hint wiz-sum" data-prefix="{escape(_("Adding:"), quote=True)}"></p>"""),
    ]
    panes = "".join(f'<fieldset class="wiz-step" data-step="{i}"{"" if i == 0 else " hidden"}>'
                    f'<legend>{_("Step {n} of {total}").format(n=i + 1, total=len(steps))}: {title}</legend>{fields}</fieldset>'
                    for i, (title, fields) in enumerate(steps))
    if loop_running():
        go_label, go_title = _("Add and research next"), _("Research Assistant takes it next.")
    else:
        go_label, go_title = _("Add and start loop"), _("Starts the loop on this mission first.")
    return (epic_datalist()
            + f'<button type="button" class="card add-mission" id="add-mission" aria-haspopup="dialog" '
              f'aria-controls="wizard">{icon("plus")}{icon("pencil")}<span>{_("Add a mission")}</span></button>'
            + f'''<dialog id="wizard" class="wizard" aria-labelledby="wizard-title">
<form method="post" action="/missions">
  <header><strong id="wizard-title">{icon("pencil")}{_("New mission")}</strong>
    <button type="button" class="wiz-close" aria-label="{_("Close")}">&times;</button></header>
  <ol class="wiz-dots" aria-hidden="true">{"".join("<li></li>" for _step in steps)}</ol>
  {panes}
  <p class="wiz-nav"><button type="button" class="wiz-back" hidden>{_("Back")}</button>
    <button type="button" class="primary wiz-next">{_("Next")}</button>
    <span class="wiz-add" hidden><button class="primary">{_("Add mission")}</button>
    <button class="wiz-go" name="go" value="start" title="{escape(go_title)}">{icon("play")}{go_label}</button></span></p>
</form></dialog>{WIZARD_JS}''')

WIZARD_JS = """<script>
(() => {
  if (window.wizardReady) return;
  window.wizardReady = true;
  // The page's refresh swaps in a fresh dialog (scripts in it don't run), so each click sets up the one on the page now.
  function setup(dlg) {
    if (dlg.wiz) return dlg.wiz;
    const form = dlg.querySelector("form"), steps = [...dlg.querySelectorAll(".wiz-step")];
    const dots = [...dlg.querySelectorAll(".wiz-dots li")], topic = dlg.querySelector("#topic");
    const back = dlg.querySelector(".wiz-back"), next = dlg.querySelector(".wiz-next"), add = dlg.querySelector(".wiz-add");
    let at = 0;
    function show(i) {
      at = i;
      steps.forEach((s, n) => s.hidden = n !== i);
      dots.forEach((d, n) => d.className = n < i ? "done" : n === i ? "on" : "");
      back.hidden = i === 0; next.hidden = i === steps.length - 1; add.hidden = !next.hidden;
      if (i === steps.length - 1) { const sum = dlg.querySelector(".wiz-sum"); sum.textContent = sum.dataset.prefix + " " + topic.value.trim(); }
      const first = steps[i].querySelector("input, textarea");
      if (first) first.focus();
    }
    function forward() {
      if (at === 0 && !topic.reportValidity()) return;
      if (at < steps.length - 1) show(at + 1);
    }
    next.addEventListener("click", forward);
    back.addEventListener("click", () => show(at - 1));
    dlg.querySelector(".wiz-close").addEventListener("click", () => dlg.close());
    dlg.addEventListener("click", e => { if (e.target === dlg) dlg.close(); });   // a click on the backdrop
    form.addEventListener("keydown", e => {                                        // Enter moves on, never submits early
      if (e.key === "Enter" && e.target.tagName === "INPUT" && at < steps.length - 1) { e.preventDefault(); forward(); }
    });
    return dlg.wiz = { open() { form.reset(); show(0); dlg.showModal(); topic.focus(); } };
  }
  document.addEventListener("click", e => {
    const dlg = document.getElementById("wizard");
    if (dlg && e.target.closest("#add-mission")) setup(dlg).open();
  });
})();
</script>"""

def actions_for(m: Mission) -> str:
    slug = quote(m.slug)
    def send_back(what: str, placeholder: str) -> str:
        return (f'<details class="send-back"><summary>{_("Send back with notes")}</summary>'
                f'<form method="post" action="/m/{slug}/reject"><label for="notes">{what}</label>'
                f'<textarea id="notes" name="notes" rows="4" required placeholder="{escape(placeholder, quote=True)}"></textarea>'
                f'<p><button>{_("Send back")}</button></p></form></details>')
    if m.state == State.BRIEF_READY and not m.approved:
        return ask("review", _("Review brief"), "Brief ready", "",
                   f'<form method="post" action="/m/{slug}/approve"><button class="primary">{_("Approve brief")}</button></form>'
                   + send_back(_("Notes for the researcher"),
                               _("e.g. Find the FAA's own incident report; the thesis overstates the denial.")))
    if m.state == State.ARTICLE_READY and not m.reviewed:
        where = _("Publishes to: {places}").format(places=", ".join([_("Local site")] + [c.NAME for c in live_channels()]))
        rated = f'<p class="hint">{escape(rating_line(m.article_rating))}</p>' if m.article_rating else ""
        return ask("publish", _("Review article"), "Article ready", f'<p class="hint">{escape(where)}</p>{rated}',
                   f'<form method="post" action="/m/{slug}/publish"><button class="primary">{_("Publish article")}</button></form>'
                   f'<a class="btn" href="/m/{slug}/preview" target="_blank">{_("Open preview")}</a>'
                   + send_back(_("Notes for the writer"),
                               _("e.g. The second section repeats the first; lead with the FAA findings.")))
    if m.state == State.PARKED:
        writing = orchestrator.parked_while_writing(m)
        if orchestrator.parked_draft(m):
            push = f'<form method="post" action="/m/{slug}/push"><button>{_("Push through anyway")}</button></form>'
            push_hint = (_("Push through: send the last failed article to review, marked as failed.") if writing
                         else _("Push through: send the last failed brief to review, marked as failed."))
        else:
            push = ""
            push_hint = _("Can't push through: no finished article.") if writing else _("Can't push through: no finished brief.")
        requeue = _("Requeue: {n} new attempts, notes kept.").format(n=m.max_attempts)
        return ask("parked", next_step(m), "Parked",
                   f'<p class="hint">{requeue} {push_hint}</p>',
                   f'<form method="post" action="/m/{slug}/requeue"><button class="primary">{_("Requeue")}</button></form>{push}')
    if m.state == State.DONE:
        rows = places(m)
        return (f'<div class="actions"><div class="buttons">'
                f'<a class="btn" href="/site/{slug}/" target="_blank">{_("View on local site")}</a>'
                f'<a class="btn" href="/published">{_("All published")}</a></div>'
                f'<p class="hint" style="margin-bottom:.2rem">{_("Published to")}</p><ul class="places">{places_html(m, rows)}</ul>'
                f'<p class="hint" style="margin-bottom:.2rem">{_("Publish it again")}</p>{publish_form(m, rows, f"/m/{slug}")}</div>')
    return ""

async def published_page(request: Request) -> HTMLResponse:
    """The published side: the local site, every channel, and each article with where it went and a way to
    publish it again."""
    missions = sorted((m for m in store.all_missions() if m.state == State.DONE),
                      key=lambda m: str(m.published_at or ""), reverse=True)
    titles = {e["slug"]: e["title"] for e in publisher.entries()}
    live = live_channels()
    queued = publish_queue(store.all_missions())
    queues = {c.KEY: [] for c in live} | {key: [slug for slug, _, _ in entries] for key, _, entries in queued}
    queues.pop("site", None)
    epics = {e.slug: e for e in store.all_epics()}
    body = [flash(request), f'<p><a class="back" href="/">&larr; {_("Missions")}</a></p>', f'<h1>{_("Published")}</h1>',
            f'<p>{_n("{n} article", "{n} articles", len(missions))} · '
            f'<a class="btn" href="/site/" target="_blank">{_("Open the local site")}</a> '
            f'<a class="btn" href="/settings#publishing">{_("Publishing settings")}</a></p>']
    rows = [f'<li><strong>{_("Local site")}</strong>: {_("always on")} · <code>site/</code> · '
            '<a href="/site/feed.xml" target="_blank">RSS</a> · <a href="/site/feed.json" target="_blank">JSON Feed</a></li>']
    rows += [f"<li>{channel_status(c)}</li>" for c in live]
    if not live:
        rows.append(f'<li class="muted">{_("No channels. Add one in {settings}.").format(settings=SETTINGS_LINK())}</li>')
    body.append(f'{heading("send", _("Channels"))}<ul class="places">{"".join(rows)}</ul>')
    body.append(queue_section(queued))
    body.append(heading("newspaper", _("Articles")))
    if not missions:
        body.append(f'<p class="muted">{_("Nothing published yet.")}</p>')
    for m in missions:
        rows = places(m, queues)
        when = f"{m.published_at:%b %-d, %Y}" if m.published_at else ""
        body.append(
            f'<div class="card pub{" publish" if m.publish_requests else ""}" id="a-{escape(m.slug)}">'
            f'<div class="row"><a class="topic" href="/site/{quote(m.slug)}/" target="_blank">{escape(titles.get(m.slug, m.topic))}</a>'
            f'<span class="badge{" publish" if m.publish_requests else ""}">{escape(label(m))}</span></div>'
            f'<div class="sub">{mission_pill(m, epics)}{escape(when)} · <a href="/m/{quote(m.slug)}">{_("mission page")}</a></div>'
            f'<ul class="places">{places_html(m, rows)}</ul>{publish_form(m, rows, "/published")}</div>')
    return page(_("Published"), "\n".join(body), poll=True)

def outreach_section(m: Mission) -> str:
    """Who the Outreach agent asked, on which channel, the exact message, and what they answered (their words are
    shown as text, escaped)."""
    found = contacts.records(m.slug)
    if not found:
        return ""
    items = []
    for r in found:
        where = (f' <a href="{escape(r.where, quote=True)}" target="_blank" rel="noopener">{_("open")}</a>'
                 if r.where.startswith("https://") else "")
        replies = "".join(f'<blockquote class="reply">{escape(x.get("text", ""))}'
                          f'<span class="hint"> {escape(str(x.get("at", ""))[:16].replace("T", " "))}</span></blockquote>'
                          for x in r.replies) or f'<p class="hint">{_("No reply yet.")}</p>'
        if r.thanked_at:
            replies += f'<p class="hint">{_("Thanked {when} UTC").format(when=f"{r.thanked_at:%Y-%m-%d %H:%M}")}</p>'
        items.append(f'<li><strong>{escape(r.name)}</strong> <span class="muted">{escape(r.address)} · '
                     f'{escape(settings.OUTREACH_CHANNELS.get(r.channel, r.channel))} · '
                     f'{r.sent_at:%Y-%m-%d %H:%M}</span>{where}'
                     f'<p class="sent" lang="en">{escape(r.message)}</p>{replies}</li>')
    answered = sum(1 for r in found if r.replies)
    title = _("Outreach: {n} of {total} answered").format(n=answered, total=len(found))
    return heading("people", title, cls="sec") + f'<ul class="outreach">{"".join(items)}</ul>'

async def mission(request: Request) -> Response:
    slug = request.path_params["slug"]
    m = store.get_mission(slug)
    if not m:
        return page(_("Not found"), f'<h1>{_("No such mission")}</h1><p><a href="/">{_("Back to missions")}</a></p>')
    epics = {e.slug: e for e in store.all_epics()}
    body = [flash(request),
            f'<p><span class="badge {tone(m)}">{escape(label(m))}</span> {mission_pill(m, epics)} '
            f'<span class="muted">{escape(m.slug)}</span> {board_link(m.slug, real=True)}</p>',
            f"<h1>{escape(m.topic)}</h1>"]
    body.append(claim_section(m))
    if m.objective:
        body.append(f'<p class="muted objective">{escape(m.objective)}</p>')
    step = working_on(m, running_now()) or next_step(m)
    if step:
        body.append(f"<p><strong>{escape(step)}</strong></p>")
    ask = escape(_("Where is the mission {slug} at, and what is taking so long?").format(slug=slug), quote=True)
    body.append(f'<p><button type="button" class="btn" data-ask="{ask}">{_("Ask chat about this mission")}</button></p>')
    body.append(actions_for(m))
    body.append(dead_lead_section(m))
    current = epics.get(m.epic)
    hint = " " + _("(proposed)") if current and current.status == "proposed" else ""
    body.append(f'{epic_datalist()}<form method="post" action="/m/{quote(slug)}/epic" class="inline">'
                f'<label for="epic" style="margin:0">{_("Epic")}<span class="hint">{hint}</span></label>'
                f'<input type="text" id="epic" name="epic" list="epic-names" maxlength="48" '
                f'value="{escape(current.title if current else "", quote=True)}" placeholder="{_("none")}">'
                f'<button>{_("Save")}</button></form>')
    body.append(connections(m))
    body.append(outreach_section(m))

    if m.validator_notes:
        title = "Why it stopped" if m.state == State.PARKED else \
                "Your feedback" if m.validator_notes.startswith("Human reviewer feedback") else \
                "Why it went back" if m.rejected_article and m.rejected_article.rating else \
                "Check notes"
        name = "message" if title == "Your feedback" else "alert"
        body.append(f'{heading(name, _(title), cls="sec")}<pre class="notes">{escape(m.validator_notes)}</pre>')
    if m.unchecked_brief:
        body.append(heading("alert", _("Brief pushed through: failed source check"), cls="sec")
                    + f'<pre class="notes">{escape(m.unchecked_brief)}</pre>')
    if m.unchecked_article:
        body.append(heading("alert", _("Article pushed through: failed article check"), cls="sec")
                    + f'<pre class="notes">{escape(m.unchecked_article)}</pre>')

    brief, article = _brief(slug), _article(slug)
    checked = not m.unchecked_brief
    preview = Path("missions") / slug / "preview.html"
    if m.article_rating and m.state in (State.ARTICLE_READY, State.DONE):
        body.append(f'{heading("scale", _("Rating"), cls="sec")}{rating_html(m.article_rating)}')
    if article and preview.exists() and m.state in (State.ARTICLE_READY, State.DONE, State.WRITING):
        body.append(f'{heading("newspaper", _("Article"), cls="sec")}<iframe class="article" src="/m/{quote(slug)}/preview"></iframe>')
    if brief and m.state == State.BRIEF_READY:
        body.append(f'{heading("search", _("Research brief"), cls="sec")}<div class="doc">{brief_html(brief, checked)}</div>')
    elif brief:
        body.append(f'<details><summary>{icon("search")}{_("Research brief")}</summary><div class="doc">{brief_html(brief, checked)}</div></details>')
    rejected = Path("missions") / slug / "brief.rejected.md"
    if rejected.exists() and m.state in (State.PARKED, State.RESEARCHING, State.QUEUED):
        body.append(f'<details><summary>{icon("cross")}{_("Last failed brief")}</summary>'
                    f'<pre class="notes">{escape(rejected.read_text())}</pre></details>')
    rejected = Path("missions") / slug / "article.rejected.md"
    if rejected.exists() and m.state in (State.PARKED, State.WRITING):
        failed = _("Last low-rated article") if m.rejected_article and m.rejected_article.rating else _("Last failed article")
        body.append(f'<details><summary>{icon("cross")}{failed}</summary>'
                    f'<pre class="notes">{escape(rejected.read_text())}</pre></details>')

    events = store.events(slug)
    if events:
        rows = "".join(f'<tr><td class="when">{escape(e["at"][:16].replace("T", " "))}</td>'
                       f'<td>{escape(_(LABELS.get(State(e["to_state"]), e["to_state"])))}</td>'
                       f'<td>{escape(e["note"] or "")}</td></tr>' for e in reversed(events))
        body.append(f'{heading("history", _("History"), cls="sec")}<table class="events">{rows}</table>'
                    f'<p><a class="steps-all" href="/log?m={quote(slug)}">{_("Step log")} &rarr;</a></p>')
    return page(m.topic, "\n".join(body), poll=True)

async def preview(request: Request) -> Response:
    slug = request.path_params["slug"]
    path = Path("missions") / slug / "preview.html"
    if not SLUG.match(slug) or not store.get_mission(slug) or not path.exists():
        return PlainTextResponse("No preview yet.", status_code=404)
    return HTMLResponse(themed_preview(path.read_text()))

def themed_preview(html: str) -> str:
    """The preview page is the published page, which picks its own theme in the reader's browser. Here it is
    shown inside the UI, so it takes the UI's appearance instead (set after the page's own script, before
    first paint) and drops its own switch, so the article never sits dark in a light page or the other way."""
    theme = settings.theme()
    set_theme = (f'document.documentElement.dataset.theme = "{theme}";' if theme != "system"
                 else 'document.documentElement.removeAttribute("data-theme");')
    override = f"<script>{set_theme}</script><style>.theme-toggle {{ display:none; }}</style>"
    return html.replace("</head>", override + "</head>", 1) if "</head>" in html else override + html

# ---------- Researcher Chat: ask what the loop is doing, or look something up on the web ----------
# A bubble on every page opens a panel over the left third. The conversation, the unsent draft and a question still
# being answered are kept in this browser tab (sessionStorage), so moving between pages or closing the panel loses
# nothing; Clear starts over and Copy puts the conversation on the clipboard. The assistant itself remembers nothing.
# It needs only Ollama, not the loop: its loop tools read what the loop leaves on disk.
# With animations on, a pixel researcher for the picked model walks in at the top of the panel (assets/researchers.js,
# the "chatguide" event): a worker's own model brings that worker's crew, any other model by its size.

SUGGESTIONS = ["What's going on right now?", "What needs me?", "What's taking so long?", "What got parked, and why?"]

CHAT_JS = """<script>
(() => {
  const panel = document.getElementById("chat-panel"), bubbleBtn = document.getElementById("chat-bubble");
  const log = document.getElementById("chat-log"), form = document.getElementById("chat-form");
  const box = document.getElementById("chat-q"), pickModel = document.getElementById("chat-model");
  const hint = document.getElementById("chat-hint"), suggest = document.getElementById("chat-suggest");
  const T = window.I18N || {}, t = s => T[s] || s;
  const store = (k, v) => { try { v === undefined ? null : sessionStorage.setItem(k, JSON.stringify(v));
                                  return JSON.parse(sessionStorage.getItem(k)); } catch (e) { return null; } };
  let history = store("chat-history") || [], loaded = false, crews = {};
  const copyBtn = document.getElementById("chat-copy");

  // A small Markdown subset for the assistant's replies: **bold**, *italic*, `code`, http(s) links, headings,
  // bullet and numbered lists, paragraphs. The text is escaped first, so a reply can never inject HTML.
  const esc = s => s.replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[c]);
  function inline(s) {
    const codes = [];
    s = esc(s).replace(/`([^`\\n]+)`/g, (_, c) => `\\u0000${codes.push(c) - 1}\\u0000`);
    s = s.replace(/\\[([^\\]\\n]+)\\]\\((https?:\\/\\/[^\\s)]+)\\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
         .replace(/\\*\\*(?=\\S)(.+?)\\*\\*/g, "<strong>$1</strong>").replace(/__(?=\\S)(.+?)__/g, "<strong>$1</strong>")
         .replace(/(^|[^*\\w])\\*(?=\\S)([^*\\n]+?)\\*(?!\\w)/g, "$1<em>$2</em>")
         .replace(/(^|[^_\\w])_(?=\\S)([^_\\n]+?)_(?!\\w)/g, "$1<em>$2</em>");
    return s.replace(/\\u0000(\\d+)\\u0000/g, (_, i) => `<code>${codes[i]}</code>`);
  }
  function markdown(text) {
    const out = []; let list = null, para = [];
    const flushPara = () => { if (para.length) out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; };
    const flushList = () => { if (list) out.push(`<${list.tag}>${list.items.map(i => `<li>${inline(i)}</li>`).join("")}</${list.tag}>`); list = null; };
    for (const line of text.split("\\n")) {
      const bullet = line.match(/^\\s*[-*+]\\s+(.*)$/), num = line.match(/^\\s*\\d+[.)]\\s+(.*)$/), head = line.match(/^\\s*#{1,6}\\s+(.*)$/);
      if (bullet || num) {
        const tag = bullet ? "ul" : "ol";
        flushPara(); if (list && list.tag !== tag) flushList();
        list = list || {tag, items: []}; list.items.push((bullet || num)[1]);
      } else if (head) { flushPara(); flushList(); out.push(`<p class="md-head"><strong>${inline(head[1])}</strong></p>`); }
      else if (!line.trim()) { flushPara(); flushList(); }
      else { flushList(); para.push(line); }
    }
    flushPara(); flushList();
    return out.join("");
  }
  function show(role, text, model, cls = "") {
    const div = document.createElement("div");
    div.className = `msg ${role} ${cls}`;
    if (role === "assistant" && !cls) { div.classList.add("md"); div.innerHTML = markdown(text || ""); }
    else div.textContent = text;
    if (model) { const tag = document.createElement("span"); tag.className = "model-tag"; tag.textContent = model; div.appendChild(tag); }
    log.appendChild(div); log.scrollTop = log.scrollHeight; return div;
  }
  function redraw() {
    log.replaceChildren(); history.forEach(h => show(h.role, h.content, h.model));
    suggest.hidden = history.length > 0;
  }
  async function loadModels() {
    if (loaded) return; loaded = true;
    let data = {models: [], default: ""};
    try { data = await (await fetch("/chat/models")).json(); } catch (e) {}
    const saved = (() => { try { return localStorage.getItem("chat-model"); } catch (e) { return null; } })();
    pickModel.replaceChildren(...data.models.map(m => new Option(m === data.default ? `${m} ${t("(default)")}` : m, m)));
    if (data.models.includes(saved)) pickModel.value = saved;
    crews = data.crews || {};
    if (data.error) hint.textContent = data.error;
    else if (data.web === false) hint.textContent += " " + t("Web search off. Switch a source on in Settings → Web search.");
    guide();
  }
  // Send in the picked model's researcher. The pixel researchers' script loads the first time the panel opens
  // (unless the page already has it) and picks up window.chatGuide when it starts.
  function guide() {
    const model = pickModel.value, c = crews[model] || {}, host = panel.querySelector(".chat-guide");
    if (!model || !host) return;
    window.chatGuide = {model, crew: c.crew || "assistant", family: c.family || model.split(":")[0]};
    document.dispatchEvent(new CustomEvent("chatguide", {detail: window.chatGuide}));
    if (!window.Researchers && !document.querySelector(`script[src="${host.dataset.sprites}"]`)) {
      const s = document.createElement("script"); s.src = host.dataset.sprites; document.head.appendChild(s);
    }
  }
  function setOpen(open) {
    panel.hidden = !open; document.body.classList.toggle("chat-open", open);
    bubbleBtn.setAttribute("aria-expanded", open); store("chat-open", open);
    if (open) { loadModels(); box.focus(); log.scrollTop = log.scrollHeight; }
  }
  // The question goes into the history as soon as it is sent, and the answer it waits for is remembered by id, so
  // a page that navigates away mid-answer hands it to the next page (/chat/answer) instead of losing both.
  function save() { history = history.slice(-20); store("chat-history", history); }
  async function wait(pending, request) {
    box.disabled = true;
    const note = show("assistant", t("Looking…"), "", "wait");
    let answer, model = pending.model;
    try {
      const r = await request();
      const data = await r.json();
      answer = data.lost ? t("Answer lost when the UI restarted. Ask again.") : data.answer;
    } catch (e) {
      if (leaving) return;                     // the page is going; the next one picks it up
      answer = t("No answer. Is the UI still running?");
    }
    note.remove();
    if ((store("chat-pending") || {}).id !== pending.id) return;      // cleared while it was thinking
    show("assistant", answer, model);
    history.push({role: "assistant", content: answer, model}); save(); store("chat-pending", null);
    box.disabled = false; if (!panel.hidden) box.focus();
  }
  async function ask(question) {
    question = question.trim();
    if (!question || box.disabled) return;
    setOpen(true); suggest.hidden = true;
    const model = pickModel.value, id = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
    const past = history.slice(-4).map(({role, content}) => ({role, content}));
    show("user", question); box.value = ""; store("chat-draft", "");
    history.push({role: "user", content: question}); save();
    const pending = {id, model}; store("chat-pending", pending);
    await wait(pending, () => fetch("/chat/ask", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({question, model, id, history: past})}));
  }
  function transcript() {
    return history.map(h => h.role === "user" ? `${t("You")}: ${h.content}`
                                              : `${t("Researcher Chat")}${h.model ? ` (${h.model})` : ""}: ${h.content}`).join("\\n\\n");
  }
  async function copy() {
    const text = transcript(), was = t("Copy");
    let ok = !!text;
    if (ok) {
      try { await navigator.clipboard.writeText(text); }
      catch (e) {                                    // older browsers: copy from a hidden textarea instead
        const t = document.createElement("textarea"); t.value = text; t.style.position = "fixed"; t.style.opacity = "0";
        document.body.appendChild(t); t.select();
        try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
        t.remove();
      }
    }
    copyBtn.textContent = !text ? t("Nothing to copy") : ok ? t("Copied") : t("Copy failed");
    clearTimeout(copy.timer); copy.timer = setTimeout(() => { copyBtn.textContent = was; }, 1500);
  }
  let leaving = false;
  addEventListener("pagehide", () => { leaving = true; });
  bubbleBtn.addEventListener("click", () => setOpen(panel.hidden));
  document.getElementById("chat-close").addEventListener("click", () => setOpen(false));
  document.getElementById("chat-clear").addEventListener("click", () => {
    history = []; save(); store("chat-pending", null); store("chat-draft", "");
    box.value = ""; box.disabled = false; redraw(); box.focus();
  });
  copyBtn.addEventListener("click", copy);
  box.addEventListener("input", () => store("chat-draft", box.value));
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !panel.hidden) setOpen(false); });
  pickModel.addEventListener("change", () => { try { localStorage.setItem("chat-model", pickModel.value); } catch (e) {} guide(); });
  form.addEventListener("submit", e => { e.preventDefault(); ask(box.value); });
  box.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(box.value); } });
  suggest.querySelectorAll("button").forEach(b => b.addEventListener("click", () => ask(b.textContent)));
  // one listener for every "Ask the assistant" button, so ones swapped in by a page refresh work too
  document.addEventListener("click", e => { const b = e.target.closest("[data-ask]"); if (b) ask(b.dataset.ask); });
  redraw();
  box.value = store("chat-draft") || "";
  if (store("chat-open")) setOpen(true);
  const pending = store("chat-pending");
  if (pending && pending.id) wait(pending, () => fetch(`/chat/answer?id=${encodeURIComponent(pending.id)}`));
})();
</script>"""

def chat_widget() -> str:
    chips = "".join(f'<button type="button">{escape(_(q))}</button>' for q in SUGGESTIONS)
    sprites_src = asset("researchers.js")
    chat = escape(_("Researcher Chat"), quote=True)
    guide = (f'<div class="chat-guide" aria-hidden="true" data-sprites="{sprites_src}"></div>'
             if settings.animations() else "")
    return f"""<button type="button" id="chat-bubble" class="chat-bubble" aria-label="{chat}" aria-expanded="false"
 aria-controls="chat-panel">{ICON_CHAT}</button>
<aside id="chat-panel" class="chat-panel" aria-label="{chat}" hidden>{guide}
<header><strong>{chat}</strong><span><button type="button" id="chat-copy" title="{_("Copy the conversation")}">{_("Copy")}</button>
 <button type="button" id="chat-clear" title="{_("Start over")}">{_("Clear")}</button>
 <button type="button" id="chat-close" aria-label="{_("Close")}">×</button></span></header>
<label class="model">{_("Model")} <select id="chat-model" aria-label="{_("Model")}"></select></label>
<p class="hint" id="chat-hint">{_("Ask about the loop, or search the web.")}</p>
<div class="chat-log" id="chat-log" aria-live="polite"></div>
<div class="suggest" id="chat-suggest">{chips}</div>
<form id="chat-form" class="chat-form"><textarea id="chat-q" rows="2" maxlength="600"
 placeholder="{escape(_("Where is the grid outage mission at?"), quote=True)}" aria-label="{_("Your question")}"></textarea>
<button class="primary">{_("Ask")}</button></form>
</aside>{CHAT_JS}"""

async def chat_models(request: Request) -> JSONResponse:
    """Installed models for the picker. Ollama being closed is reported, not an error page."""
    MODEL = settings.chat_model()
    web = agents.chat_web()
    try:
        found = await run_in_threadpool(agents.chat_catalog)
    except Exception:
        return JSONResponse({"models": [MODEL], "default": MODEL, "crews": {MODEL: chat_crew(MODEL)}, "web": web,
                             "error": _("Can't reach Ollama. Start it to use chat.")})
    found = found or [{"name": MODEL, **chat_crew(MODEL)}]
    return JSONResponse({"models": [c["name"] for c in found], "default": found[0]["name"], "web": web,
                         "crews": {c["name"]: {"crew": c["crew"], "family": c["family"]} for c in found}})

def chat_crew(model: str) -> dict:
    """Which researcher stands for a model Ollama couldn't describe: its worker's crew, or by the size in its name."""
    from agents.assistant import crew
    return {"crew": crew(model), "family": model.split(":")[0]}

# Answers the page is waiting for, by the id the page gave its question, so one survives the page navigating away
# mid-answer: the next page picks it up from /chat/answer. This UI process only, the last few, never on disk.
CHAT_JOBS: dict[str, concurrent.futures.Future] = {}
CHAT_JOBS_KEPT = 20
_chat_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="chat")

def _answer(question: str, history: list, model: str | None) -> str:
    try:
        return agents.chat(question, history, model)
    except Exception as e:                 # Ollama closed, model missing: say so instead of a blank page
        return _("I couldn't answer just now ({error}). Is Ollama running?").format(error=f"{type(e).__name__}: {str(e)[:200]}")

def _keep_job(job_id: str, job: concurrent.futures.Future) -> None:
    CHAT_JOBS[job_id] = job
    for old in [k for k, f in CHAT_JOBS.items() if f.done()][:max(0, len(CHAT_JOBS) - CHAT_JOBS_KEPT)]:
        CHAT_JOBS.pop(old, None)

async def chat_ask(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    try:
        data = await request.json()
        question, history = str(data.get("question") or ""), data.get("history") or []
        model = str(data.get("model") or "")[:100] or None
        job_id = str(data.get("id") or "")[:64]
        history = history if isinstance(history, list) else []
    except (ValueError, AttributeError):
        return JSONResponse({"answer": _("That question didn't arrive in one piece. Try again.")}, status_code=400)
    job = _chat_pool.submit(_answer, question, [h for h in history if isinstance(h, dict)], model)
    if job_id:
        _keep_job(job_id, job)
    # shielded, so a page that navigates away doesn't cancel the answer the next page will pick up
    return JSONResponse({"answer": await asyncio.shield(asyncio.wrap_future(job))})

async def chat_answer(request: Request) -> JSONResponse:
    """The answer to a question an earlier page asked, waiting for it if it is still being worked out."""
    job = CHAT_JOBS.get(request.query_params.get("id", ""))
    if job is None:
        return JSONResponse({"lost": True}, status_code=404)
    return JSONResponse({"answer": await asyncio.shield(asyncio.wrap_future(job))})

async def api_signature(request: Request) -> PlainTextResponse:
    return PlainTextResponse(signature())

LOG_PAGE = 200                             # steps per page of the full log

def step_row(e: dict, topics: dict[str, str], live: bool = False) -> str:
    """One row of the full log: when it ended, which agent, on what, on which worker, how long, how it ended."""
    if live:
        when = _("now")
        took_ = _("{time} so far").format(time=f'<span data-since="{float(e["since"])}">{elapsed(e["since"])}</span>')
        out = f'<span class="out live">{_("working")}</span>'
    else:
        when = datetime.fromtimestamp(float(e["ended"])).strftime("%Y-%m-%d %H:%M:%S")
        took_ = took(e["ended"] - e["since"])
        kind = e.get("outcome", "done")
        out = (f'<span class="out {kind}">{_(OUTCOME.get(kind, "done"))}</span>'
               + (f' <span class="muted">{escape(e["why"])}</span>' if e.get("why") else ""))
    task = (f'<a href="/m/{quote(e["slug"])}">{escape(topics.get(e["slug"], e["slug"]))}</a>' if e.get("slug")
            else f'<span class="muted">{_(PAST.get(e["agent"], "all missions"))}</span>')
    who = seat_name(e.get("worker", "")) or _("Desk")
    return (f'<tr><td class="when">{when}</td><td><strong>{agent_name(e["agent"])}</strong></td><td>{task}</td>'
            f'<td class="when">{escape(who)}</td><td class="took">{took_}</td><td>{out}</td></tr>')

LOG_COLUMNS = ("Ended", "Step", "Mission", "Worker", "Took", "Outcome")

async def log_page(request: Request) -> HTMLResponse:
    """The full step log (activity.LOG): every agent step the loop has finished, newest first, with how long each
    took and how it ended, the ones at work now on top. ?m=<slug> keeps one mission's; ?page= goes back in time."""
    slug = request.query_params.get("m", "")
    try:
        page_no = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page_no = 1
    topics = {m.slug: m.topic for m in store.all_missions()}
    steps = activity.history()
    live = running_now()
    if slug:
        steps, live = [e for e in steps if e.get("slug") == slug], [e for e in live if e.get("slug") == slug]
    pages = max(1, -(-len(steps) // LOG_PAGE))
    page_no = min(page_no, pages)
    shown = steps[(page_no - 1) * LOG_PAGE: page_no * LOG_PAGE]
    back = (f'<a class="back" href="/m/{quote(slug)}">&larr; {_("Back to the mission")}</a>' if slug
            else f'<a class="back" href="/">&larr; {_("Back to missions")}</a>')
    total = sum(e["ended"] - e["since"] for e in steps)
    count = _n("{n} step", "{n} steps", len(steps)).format(n=len(steps))
    if slug:
        count = _("{steps} on {mission}").format(
            steps=count, mission=f'<a href="/m/{quote(slug)}">{escape(topics.get(slug, slug))}</a>')
    summary = _("{steps} · {time} in all · saved in {file}").format(
        steps=count, time=took(total), file=f"<code>{escape(str(activity.LOG))}</code>")
    body = [f'<p>{back}</p>', f'<h1>{_("Step log")}</h1>',
            f'<p class="muted">{summary}'
            + (f' · <a href="/log">{_("All missions")}</a>' if slug else "") + "</p>"]
    rows = [step_row(e, topics, live=True) for e in (live if page_no == 1 else [])]
    rows += [step_row(e, topics) for e in shown]
    if rows:
        head = "".join(f'<td class="when">{_(h)}</td>' for h in LOG_COLUMNS)
        body.append(f'<table class="events steps-log"><tr>{head}</tr>{"".join(rows)}</table>')
    else:
        body.append(f'<p class="muted">{_("No steps yet.")}</p>')
    if pages > 1:
        link = lambda n, label: f'<a class="btn" href="/log?{("m=" + quote(slug) + "&") if slug else ""}page={n}">{label}</a>'
        body.append(f'<p>{link(page_no - 1, "&larr; " + _("Newer")) if page_no > 1 else ""} '
                    f'<span class="muted">{_("Page {n} of {total}").format(n=page_no, total=pages)}</span> '
                    f'{link(page_no + 1, _("Older") + " &rarr;") if page_no < pages else ""}</p>')
    return page(_("Step log"), "".join(body), poll=True)

async def api_flow(request: Request) -> JSONResponse:
    return JSONResponse(flow_state())

# ---------- the cork board (board.py places the notes and finds the strings; this adds the words) ----------

# A card is one link already, so its "On the board" is a span this sends to the board, zoomed onto the mission.
BOARD_LINK_JS = """<script>
(() => {
  const go = e => {
    const b = e.target.closest && e.target.closest("[data-board]");
    if (!b || (e.type === "keydown" && e.key !== "Enter")) return;
    e.preventDefault(); e.stopPropagation();
    location.href = "/board?focus=" + encodeURIComponent(b.dataset.board);
  };
  document.addEventListener("click", go, true);
  document.addEventListener("keydown", go, true);
})();
</script>"""

def board_link(slug: str, real: bool = False) -> str:
    """A small "On the board" link to the board, zoomed onto this mission (a span inside a card, which is a link)."""
    if real:
        return f'<a class="onboard" href="/board?focus={quote(slug)}">{ICON_BOARD}{_("On the board")}</a>'
    return (f'<span class="onboard" role="link" tabindex="0" data-board="{escape(slug, quote=True)}" '
            f'title="{escape(_("Show on the board"), quote=True)}">{ICON_BOARD}{_("On the board")}</span>')

PAPER = {State.QUEUED: "queued", State.RESEARCHING: "research", State.AWAITING_COMMENTS: "writing",
         State.OUTREACH: "writing",
         State.WRITING: "writing", State.ARTICLE_READY: "publish", State.DONE: "done", State.PARKED: "parked",
         State.COMBINED: "queued", State.DEAD_LEAD: "parked"}

def paper(m: Mission) -> str:
    """The sticky note's colour: the stage it is at, like the cards' tones."""
    if m.state == State.BRIEF_READY:
        return "writing" if m.approved else "review"
    return PAPER.get(m.state, "queued")

def _cut(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rsplit(" ", 1)[0] + "…"

def board_state() -> dict:
    """What the board shows: board.layout()'s notes and strings, with each note's words. /api/board serves it."""
    data = board.layout()
    missions = {m.slug: m for m in store.all_missions()}
    epics = {e.slug: e for e in store.all_epics()}
    live = {e["slug"]: settings.seat_worker(e["worker"]) for e in running_now()}   # slug -> whose crew visits it
    strung: dict[str, int] = {}
    for s in data["strings"]:
        if s["kind"] != "epic":
            for end in (s["a"], s["b"]):
                strung[end] = strung.get(end, 0) + 1
    members: dict[str, int] = {}
    for m in missions.values():
        if m.state != State.COMBINED:
            key = m.epic if m.epic in epics else board.UNSORTED
            members[key] = members.get(key, 0) + 1
    for n in data["notes"]:
        if n["kind"] == "mission":
            m = missions[n["slug"]]
            try:
                brief, article = _brief(m.slug), _article(m.slug)
            except ValueError:             # an artifact from an older schema: show the note without it
                brief = article = None
            thesis = brief.thesis if brief else m.brief_sources.thesis if m.brief_sources else ""
            sources = len(brief.sources) if brief else 0
            links = strung.get(n["id"], 0)
            meta = [_n("{n} source", "{n} sources", sources) if sources else "",
                    _n("{n} connection", "{n} connections", links) if links else "",
                    _("attempt {n} of {total}").format(n=m.attempts, total=m.max_attempts)
                    if m.state == State.RESEARCHING and m.attempts else "",
                    _("combined into {mission}").format(mission=m.combined_into) if m.combined_into else ""]
            n.update(title=article.title if article else m.topic, short=_cut(article.title if article else m.topic, 90),
                     thesis=_cut(thesis or m.objective or m.topic, board.PREVIEW_CHARS),
                     thesis_label=_("Thesis") if thesis else _("Objective"), stage=label(m), paper=paper(m),
                     epic=epics[m.epic].title if m.epic in epics else "", url=f"/m/{quote(m.slug)}",
                     open=_("Open article") if article else _("Open brief") if brief else _("Open mission"),
                     meta=" · ".join(x for x in meta if x), live=m.slug in live,
                     faded=m.state in (State.COMBINED, State.DEAD_LEAD), dead=m.state == State.DEAD_LEAD,
                     worker=live.get(m.slug) if live.get(m.slug) in settings.WORKERS else "")
        else:
            count = members.get(n["slug"], 0)
            e = epics.get(n["slug"])
            n.update(title=e.title if e else _("Unsorted"), proposed=bool(e and e.status == "proposed"),
                     count_text=_n("{n} mission", "{n} missions", count))
    data.update(poll=board.POLL_SECONDS * 1000, size_min=board.SIZE_MIN, size_max=board.SIZE_MAX,
                style=settings.board_style(), animations=settings.animations())
    data["sig"] = hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:12]
    return data

async def api_board(request: Request) -> JSONResponse:
    data = await run_in_threadpool(board_state)
    if request.query_params.get("sig") == data["sig"]:
        return JSONResponse({"same": True})
    return JSONResponse(data)

async def board_move(request: Request) -> JSONResponse:
    """A human dragged or resized a note (assets/board.js)."""
    if not same_origin(request):
        return JSONResponse({"error": "cross-site"}, status_code=403)
    try:
        body = await request.json()
        prefix, _, slug = str(body["id"]).partition(":")
        kind = {"m": "mission", "e": "epic"}[prefix]
        x, y, size = body["x"], body["y"], body.get("size")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (x, y)) or \
                (size is not None and (isinstance(size, bool) or not isinstance(size, (int, float)))):
            raise ValueError("x, y and size must be numbers")
        note = board.move(kind, slug, float(x), float(y), None if size is None else float(size))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(note.model_dump())

def board_style_panel(style: dict) -> str:
    """The board's own gear: surface, cards, string and its colour, changed in place (assets/board.js)."""
    def seg(key: str, options: dict) -> str:
        return (f'<div class="seg" data-key="{key}">' + "".join(
            f'<button type="button" data-value="{v}" class="{"on" if style[key] == v else ""}">{escape(_(n))}</button>'
            for v, n in options.items()) + "</div>")
    dots = "".join(f'<button type="button" class="dot{" on" if style["color"] == c else ""}" data-kind="{kind}" '
                   f'data-value="{c}" style="--c:{c}" title="{escape(_(name))}" aria-label="{escape(_(name))}"'
                   f'{"" if kind == style["string"] else " hidden"}></button>'
                   for kind, colors in settings.BOARD_STRINGS.items() for name, c in colors.items())
    own = escape(_("Your own colour"), quote=True)
    return (f'<div class="board-style" hidden><div class="lbl">{_("Surface")}</div>{seg("surface", settings.BOARD_SURFACES)}'
            f'<div class="lbl">{_("Cards")}</div>{seg("cards", settings.BOARD_CARDS)}'
            f'<div class="lbl">{_("Joined by")}</div>{seg("string", settings.BOARD_STRING_NAMES)}'
            f'<div class="lbl">{_("Colour")}</div><div class="dots">{dots}'
            f'<label class="dot own" title="{own}"><input type="color" value="{style["color"]}" '
            f'aria-label="{own}"></label></div>'
            f'<a class="more" href="/settings#board">{_("Settings")}</a></div>')

# A click on one of the Settings page's board choices sends that choice alone (`changed`), so a page showing an older
# look (the gear on the board changed it in another tab, or the browser restored old ticks) never saves the rest back.
BOARD_PICK = "this.form.changed.value = this.name; this.form.requestSubmit()"

def board_style_form() -> str:
    """The same choices on the Settings page; each change saves at once."""
    style = settings.board_style()
    def choice(key: str, options: dict) -> str:
        return "".join(f'<label><input type="radio" name="{key}" value="{v}"{" checked" if style[key] == v else ""} '
                       f'onchange="{BOARD_PICK}">{escape(_(n))}</label>' for v, n in options.items())
    colors = settings.BOARD_STRINGS[style["string"]]
    swatches = "".join(
        f'<label class="swatch" style="--c:{c};--ink:{settings.ink_on(c)}"><input type="radio" name="color" value="{c}"'
        f'{" checked" if style["color"] == c else ""} onchange="{BOARD_PICK}"><span class="dab"></span>'
        f'{escape(_(n))}</label>' for n, c in colors.items())
    presets = {c for kind in settings.BOARD_STRINGS.values() for c in kind.values()}
    own = style["color"] not in presets
    swatches += (f'<label class="swatch own{" set" if own else ""}" style="--c:{style["color"]};--ink:{settings.ink_on(style["color"])}">'
                 f'<span class="dab"></span><input type="color" name="own" value="{style["color"]}" aria-label="{escape(_("Your own colour"), quote=True)}" '
                 f'onchange="this.form.querySelectorAll(\'input[name=color]\').forEach(r => r.checked = false); '
                 f'this.form.changed.value = \'color\'; this.form.requestSubmit()">{_("Your own")}</label>')
    return f"""<form method="post" action="/settings/board-style" class="actions display" id="board" autocomplete="off">
  <input type="hidden" name="changed" value="">
  <h3>{icon("board")}{_("Board")}</h3>
  <p class="hint">{_("Surface, cards and string for the {board}. Same as the gear on the board.").format(board=f'<a href="/board">{_("board")}</a>')}</p>
  <div class="choice" role="radiogroup" aria-label="{_("Surface")}">{choice("surface", settings.BOARD_SURFACES)}</div>
  <p></p><div class="choice" role="radiogroup" aria-label="{_("Cards")}">{choice("cards", settings.BOARD_CARDS)}</div>
  <p></p><div class="choice" role="radiogroup" aria-label="{_("Joined by")}">{choice("string", settings.BOARD_STRING_NAMES)}</div>
  <p></p><div class="swatches" role="radiogroup" aria-label="{_("Colour")}">{swatches}</div>
  <noscript><p><button>{_("Save")}</button></p></noscript>
</form>"""

def _board_style_from(values) -> dict:
    get = lambda k: (str(values.get(k)) if values.get(k) is not None else None)
    return settings.set_board_style(surface=get("surface"), cards=get("cards"), string=get("string"), color=get("color"))

async def board_style_save(request: Request) -> JSONResponse:
    """The board's gear changed its look (assets/board.js)."""
    if not same_origin(request):
        return JSONResponse({"error": "cross-site"}, status_code=403)
    try:
        return JSONResponse(_board_style_from(await request.json()))
    except (ValueError, AttributeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)

async def save_board_style(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    values = {k: form.get(k) for k in ("surface", "cards", "string", "color") if form.get(k)}
    if "color" not in values and form.get("own"):
        values["color"] = form.get("own")      # the colour picker: no preset is ticked
    if form.get("changed") in ("surface", "cards", "string", "color"):
        values = {k: v for k, v in values.items() if k == form.get("changed")}   # one click, one change
    if "string" in values and values["string"] != settings.board_style()["string"]:
        values.pop("color", None)              # a new kind brings its own first colour
    try:
        _board_style_from(values)
    except ValueError:
        return go("/settings", error="board_style")
    return go("/settings", done="board_style_saved")

_MENTIONS_FOUND = False                    # an older desk's mentions are found the first time a board opens

async def board_page(request: Request) -> Response:
    global _MENTIONS_FOUND
    if not _MENTIONS_FOUND:
        _MENTIONS_FOUND = True             # once per start, even if it fails: the board shows without them
        try:
            await run_in_threadpool(board.find_all_mentions)
        except Exception as e:
            print(f"board: finding mentions failed ({type(e).__name__}: {e})", flush=True)
    data = await run_in_threadpool(board_state)
    focus = request.query_params.get("focus", "")
    raw = json.dumps(data).replace("<", "\\u003c")   # no "</script>" or "<!--" can end the data block
    empty = "" if data["notes"] else (f'<p class="board-empty">{_("Board empty.")} '
                                      f'<a href="/">{_("Add a mission")}</a></p>')
    style = data["style"]
    looks = (f'surface-{style["surface"]} cards-{style["cards"]} str-{style["string"]}'
             + (" animate" if data["animations"] else ""))
    body = (f'<style>{BOARD_CSS}</style>'
            f'<div class="board-wrap {looks}" id="board" style="--string:{style["color"]}" '
            f'data-focus="{escape(focus, quote=True)}">'
            f'<div class="world"><svg class="strings" aria-hidden="true"></svg></div>{empty}'
            f'<div class="board-crumb" hidden></div>'
            f'<div class="board-pop" hidden></div>'
            f'<div class="board-legend"><span><i class="e"></i>{_("Epic")}</span><span><i class="l"></i>{_("Links")}</span>'
            f'<span><i class="m"></i>{_("Same source or mention")}</span></div>'
            f'<div class="board-zoom"></div>'
            f'<div class="board-tools"><button type="button" data-tool="out" title="{_("Zoom out")}" aria-label="{_("Zoom out")}">{ICON_ZOOM_OUT}</button>'
            f'<button type="button" data-tool="in" title="{_("Zoom in")}" aria-label="{_("Zoom in")}">{ICON_ZOOM_IN}</button>'
            f'<button type="button" data-tool="fit" title="{_("Fit everything")}" aria-label="{_("Fit everything")}">{ICON_FIT}</button>'
            f'<button type="button" data-tool="style" title="{_("Board style")}" aria-label="{_("Board style")}" '
            f'aria-expanded="false">{ICON_SETTINGS}</button></div>{board_style_panel(style)}'
            f'</div><script type="application/json" id="board-data">{raw}</script>'
            f'{script_i18n("board.js")}{script("board.js")}{script("researchers.js") if data["animations"] else ""}')
    return page(_("Board"), body)

# The board's tools are drawn, not typed: a "−" or "+" glyph sits on its font's baseline, never in the middle.
ICON_ZOOM_OUT = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 12h12"/></svg>'
ICON_ZOOM_IN = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 12h12M12 6v12"/></svg>'
ICON_FIT = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>'

BOARD_CSS = """
/* the view's offsets and zoom, which only a moving surface's own background reads: never inherited, so moving the
   view restyles the board alone, not its hundreds of notes and strings */
@property --gx { syntax:"<length>"; inherits:false; initial-value:0px; }
@property --gy { syntax:"<length>"; inherits:false; initial-value:0px; }
@property --gz { syntax:"<number>"; inherits:false; initial-value:1; }
body:has(.board-wrap) { overflow-x:hidden; }
body:has(.board-wrap) nav.top { margin-bottom:1rem; }
.board-wrap { --string:#c0242b; position:relative; width:calc(100vw - 2rem); margin-left:calc(50% - 50vw + 1rem);
  height:calc(100vh - 8rem); min-height:420px; overflow:hidden; border-radius:6px; touch-action:none; user-select:none;
  cursor:grab; background-color:#b98a57;
  background-image:
    radial-gradient(rgba(70,40,15,.28) 1px, transparent 1.4px),
    radial-gradient(rgba(255,230,190,.18) 1px, transparent 1.6px),
    radial-gradient(rgba(90,55,20,.22) 1.5px, transparent 2px),
    radial-gradient(ellipse at 30% 20%, rgba(255,220,170,.25), transparent 60%),
    radial-gradient(ellipse at 80% 90%, rgba(60,30,10,.25), transparent 55%);
  background-size:7px 7px, 11px 13px, 17px 19px, 100% 100%, 100% 100%;
  background-position:0 0, 3px 5px, 8px 2px, 0 0, 0 0;
  box-shadow:inset 0 0 0 12px #6e4a2a, inset 0 0 0 14px #4c321b, inset 0 0 60px rgba(0,0,0,.35); }
.board-wrap:active { cursor:grabbing; }
.board-wrap .world { position:absolute; left:0; top:0; transform-origin:0 0; will-change:transform; }
.board-wrap svg.strings { position:absolute; left:0; top:0; width:1px; height:1px; overflow:visible;   /* board.js sizes it */
  filter:drop-shadow(1px 3px 1.5px rgba(0,0,0,.35)); }       /* one shadow for every string, never one each */
.board-wrap .str path { fill:none; stroke-linecap:round; }
.board-wrap .str .line { stroke:var(--string); stroke-width:2.4; }
.board-wrap .str.link .line { stroke-width:1.8; }
.board-wrap .str.mention .line { stroke-width:1.3; stroke-dasharray:5 4; opacity:.8; }
.board-wrap .str .hit { stroke:transparent; stroke-width:12; pointer-events:stroke; }
.board-wrap .str.epic .hit { pointer-events:none; }
.board-wrap .faded { opacity:.2; filter:saturate(.3); transition:opacity .3s; }
.board-wrap .str.faded { filter:none; }                     /* the strings are one colour: fading is enough */
.board-wrap .note { border:none; margin:0; }                /* not the site's .note */
.board-wrap .note { --s:1; position:absolute; transform:translate(-50%,-50%) rotate(var(--r,0deg));
  width:calc(160px * var(--s)); padding:calc(20px * var(--s)) calc(13px * var(--s)) calc(11px * var(--s));
  font-size:calc(13px * var(--s)); line-height:1.3; background:var(--paper,#fff3a3); color:#2b2620; border-radius:2px;
  box-shadow:0 1px 1px rgba(0,0,0,.15), 0 8px 14px -6px rgba(0,0,0,.45); cursor:pointer; transition:opacity .3s, filter .3s; }
.board-wrap .note.lifted { box-shadow:0 2px 3px rgba(0,0,0,.2), 0 18px 26px -8px rgba(0,0,0,.5); z-index:2; cursor:grabbing; }
.board-wrap .note .pin { position:absolute; top:6px; left:50%; width:14px; height:14px; margin-left:-7px; border-radius:50%;
  background:radial-gradient(circle at 35% 30%, #ff8a8a, #c0242b 55%, #6d0f13); box-shadow:1px 3px 3px rgba(0,0,0,.45); }
.board-wrap .note .t { font-family:Georgia, serif; font-weight:600; }
.board-wrap .note .stage { margin-top:.6em; font-size:.8em; letter-spacing:.04em; text-transform:uppercase; color:rgba(0,0,0,.55); }
.board-wrap .note .stage::before { content:""; display:inline-block; width:.55em; height:.55em; border-radius:50%;
  margin-right:.4em; background:var(--dot,#888); }
.board-wrap .note .stamp { display:inline-block; margin-top:.6em; font:700 .75em/1 system-ui, sans-serif; letter-spacing:.08em;
  text-transform:uppercase; color:var(--dot); border:1.5px solid var(--dot); padding:3px 5px; border-radius:3px;
  transform:rotate(-4deg); opacity:.85; }
.board-wrap .note .now { position:absolute; top:-10px; right:-8px; background:#2f8a4a; color:#fff;
  font:600 9.5px/1 system-ui, sans-serif; padding:3px 6px; border-radius:999px; }
.board-wrap .note.live { outline:2.5px solid #2f8a4a; outline-offset:3px; }
.board-wrap .note .grip { position:absolute; right:0; bottom:0; width:14px; height:14px; cursor:nwse-resize; opacity:0;
  background:linear-gradient(135deg, transparent 50%, rgba(0,0,0,.25) 50%); }
.board-wrap .note:hover .grip { opacity:1; }
.board-wrap .queued   { --paper:#eef1f4; --dot:#7d8792; }
.board-wrap .research { --paper:#fff3a3; --dot:#b08f00; }
.board-wrap .review   { --paper:#cfe6ff; --dot:#2f6fd0; }
.board-wrap .writing  { --paper:#ffe1bf; --dot:#d9822b; }
.board-wrap .publish  { --paper:#d4f2cf; --dot:#2f8a4a; }
.board-wrap .done     { --paper:#fffdf8; --dot:#2f8a4a; }
.board-wrap .parked   { --paper:#ffd6d6; --dot:#c0242b; }
.board-wrap .note.combined { opacity:.55; filter:saturate(.5); }
.board-wrap .note.combined .stamp { --dot:#6b6b70; }
.board-wrap .note.epic { display:block; white-space:normal; font:400 calc(13px * var(--s))/1.3 -apple-system, system-ui, sans-serif;
  letter-spacing:normal; border:none; color:#2b2620; }      /* not the site's .epic pill */
.board-wrap .note.epic { --paper:#fffaf0; text-align:center; padding-top:calc(24px * var(--s));
  background-image:repeating-linear-gradient(to bottom, transparent 0 1.6em, rgba(60,110,200,.18) 1.6em calc(1.6em + 1px));
  background-position:0 calc(3.1em); }
.board-wrap .note.epic::before { content:""; position:absolute; top:calc(2.9em); left:0; right:0; height:1.5px; background:rgba(200,50,50,.45); }
.board-wrap .note.epic .k { font:600 .62em/1 system-ui, sans-serif; letter-spacing:.14em; color:#8a2b2b; text-transform:uppercase; }
.board-wrap .note.epic .t { font-size:1.45em; margin-top:.75em; line-height:1.15; }
.board-wrap .note.epic .n { margin-top:.5em; font-size:.85em; color:#6b6b70; }
.board-wrap .note.epic .pin { width:18px; height:18px; margin-left:-9px; top:4px;
  background:radial-gradient(circle at 35% 30%, #fff1a6, #e0b100 55%, #7a5c00); }
.board-wrap .note.epic.proposed { outline:2px dashed rgba(138,43,43,.7); outline-offset:4px; }
.board-wrap .note.unsorted { background:none; box-shadow:none; text-align:center; width:200px; cursor:default; color:rgba(40,22,8,.7); }
.board-wrap .note.unsorted .t { font-style:italic; font-size:1.3em; }
.board-wrap .note.unsorted .n { font-size:.85em; }
.board-pop { position:absolute; width:320px; max-width:calc(100% - 24px); background:var(--bg); color:var(--fg); border-radius:12px;
  padding:14px 16px 12px; box-shadow:0 18px 40px -10px rgba(0,0,0,.5), 0 0 0 1px rgba(0,0,0,.06); font-size:13.5px;
  z-index:4; cursor:auto; user-select:text; }
.board-pop .pop-row { display:flex; justify-content:space-between; align-items:center; gap:.6rem; }
.board-pop .tag { font-size:11px; padding:2px 8px; border-radius:999px; background:var(--quote); font-weight:600; }
.board-pop .tag.review { background:#cfe6ff; color:#1f4f99; } .board-pop .tag.publish { background:#d4f2cf; color:#1e6b34; }
.board-pop .tag.parked { background:#ffd6d6; color:#8f1a1f; } .board-pop .tag.research { background:#fff3a3; color:#6d5700; }
.board-pop .tag.writing { background:#ffe1bf; color:#8a4b0f; } .board-pop .tag.done { background:#e8f5e4; color:#1e6b34; }
.board-pop .epicpill { font-size:11px; padding:2px 8px; border-radius:999px; border:1px solid var(--rule); color:var(--muted); }
.board-pop h3 { font-family:Georgia, serif; font-size:16px; line-height:1.3; margin:.55rem 0 .45rem; }
.board-pop .lbl { font:600 10.5px/1 system-ui, sans-serif; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); margin-top:.5rem; }
.board-pop p { margin:.3rem 0 .6rem; line-height:1.45; }
.board-pop .meta { font-size:12px; color:var(--muted); }
.board-pop .open { flex:none; font-size:12.5px; font-weight:600; color:#fff; background:var(--accent); border-radius:8px;
  padding:.38rem .7rem; text-decoration:none; white-space:nowrap; }
.board-wrap .deg { display:inline-flex; gap:4px; }
.board-wrap .deg button { min-width:30px; border:1px solid var(--rule); background:var(--bg); color:var(--fg); border-radius:7px;
  padding:.2rem .45rem; font:600 12px system-ui, sans-serif; cursor:pointer; }
.board-wrap .deg button.on { background:var(--fg); color:var(--bg); border-color:var(--fg); }
.board-pop .deg { margin:.45rem 0 .2rem; }
.board-crumb, .board-legend, .board-tools button { background:color-mix(in srgb, var(--bg) 95%, transparent);
  box-shadow:0 4px 12px rgba(0,0,0,.3); border-radius:10px; z-index:3; }
.board-crumb { position:absolute; left:24px; top:24px; padding:.4rem .7rem; font-size:12.5px; display:flex; gap:.5rem;
  align-items:center; cursor:auto; }
.board-crumb[hidden] { display:none; }
.board-crumb b { font-family:Georgia, serif; } .board-crumb .x { color:var(--muted); }
.board-crumb .clear { border:none; background:none; color:var(--muted); cursor:pointer; font-size:13px; }
.board-legend { position:absolute; left:24px; bottom:24px; padding:.5rem .8rem; font-size:11.5px; display:flex; gap:1rem; cursor:auto; }
.board-legend span { display:inline-flex; align-items:center; gap:.35rem; }
.board-legend i { display:inline-block; width:24px; border-top:2.4px solid var(--string); }
.board-legend i.l { border-top-width:1.8px; } .board-legend i.m { border-top:1.4px dashed var(--string); }
.board-zoom { position:absolute; right:206px; top:36px; font:600 11px system-ui, sans-serif; color:rgba(255,255,255,.9);
  text-shadow:0 1px 2px rgba(0,0,0,.6); z-index:3; }
.board-tools { position:absolute; right:24px; top:24px; display:flex; gap:6px; z-index:3; }
.board-tools button { width:38px; height:38px; padding:0; border:none; display:grid; place-items:center; line-height:0;
  color:var(--fg); cursor:pointer; }                         /* padding:0: the app's button padding pushed icons off centre */
.board-tools svg { display:block; width:18px; height:18px; stroke:var(--fg); fill:none; stroke-width:1.8; stroke-linecap:round; }
.board-empty { position:absolute; inset:0; display:grid; place-items:center; margin:0; color:#fff;
  font:italic 1.2rem Georgia, serif; text-shadow:0 1px 3px rgba(0,0,0,.6); }
.board-empty a { color:#fff; }
@media (max-width: 640px) { .board-legend { display:none; } .board-wrap { height:calc(100vh - 11rem); } }/* ---------- surfaces (cork is the default above) ---------- */
.board-wrap.surface-paper { background-color:#f4efe4; border-radius:4px;
  background-image:radial-gradient(rgba(120,100,70,.09) 1px, transparent 1.5px), radial-gradient(rgba(120,100,70,.06) 1px, transparent 1.6px),
    radial-gradient(ellipse at 20% 10%, rgba(255,255,255,.7), transparent 55%), radial-gradient(ellipse at 90% 100%, rgba(150,120,70,.14), transparent 60%);
  background-size:9px 9px, 13px 11px, 100% 100%, 100% 100%; background-position:0 0, 4px 6px, 0 0, 0 0;
  box-shadow:inset 0 0 0 1px #d9d0bf, inset 0 0 50px rgba(120,90,40,.15); }
.board-wrap.surface-grid { background-color:#fbfcff;
  background-image:linear-gradient(rgba(60,110,200,.22) 1px, transparent 1px), linear-gradient(90deg, rgba(60,110,200,.22) 1px, transparent 1px),
    linear-gradient(rgba(60,110,200,.09) 1px, transparent 1px), linear-gradient(90deg, rgba(60,110,200,.09) 1px, transparent 1px);
  background-size:calc(var(--gz,1) * 100px) calc(var(--gz,1) * 100px), calc(var(--gz,1) * 100px) calc(var(--gz,1) * 100px),
    calc(var(--gz,1) * 20px) calc(var(--gz,1) * 20px), calc(var(--gz,1) * 20px) calc(var(--gz,1) * 20px);
  background-position:var(--gx,0) var(--gy,0); box-shadow:inset 0 0 0 1px #c9d6ee; }
.board-wrap.surface-dark { background-color:#0d1220;
  background-image:radial-gradient(rgba(130,180,255,.22) 1px, transparent 1.5px), radial-gradient(ellipse at 50% 0%, rgba(70,110,200,.22), transparent 65%);
  background-size:calc(var(--gz,1) * 28px) calc(var(--gz,1) * 28px), 100% 100%; background-position:var(--gx,0) var(--gy,0), 0 0;
  box-shadow:inset 0 0 90px rgba(0,0,0,.7); }
.board-wrap.surface-fridge { background-color:#eceff1; border-radius:18px;
  background-image:linear-gradient(100deg, transparent 18%, rgba(255,255,255,.8) 30%, transparent 42%),
    linear-gradient(100deg, transparent 58%, rgba(255,255,255,.45) 64%, transparent 70%), linear-gradient(180deg, #f8f9fa, #dfe3e6);
  box-shadow:inset 0 0 0 10px #d3d8dc, inset 0 0 0 11px #b6bcc2, inset 0 -40px 70px rgba(0,0,0,.07); }
.board-wrap:is(.surface-paper, .surface-grid, .surface-fridge) .board-zoom { color:#555; text-shadow:none; }
.board-wrap.surface-dark .note.unsorted, .board-wrap.surface-dark .board-empty { color:#cfe2ff; }
/* on the dark boards the controls are lifted off the board with a lighter face and a thin light ring, or a dark
   theme's buttons (#151517) vanish on night (#0d1220) and arcade (#1d2b53) */
.board-wrap:is(.surface-dark, .surface-arcade) :is(.board-crumb, .board-legend, .board-tools button, .board-style) {
  background:color-mix(in srgb, var(--bg) 86%, #9fb4d8); box-shadow:0 0 0 1px rgba(200,220,255,.28), 0 6px 16px rgba(0,0,0,.55); }
.board-wrap:is(.surface-dark, .surface-arcade) .board-tools button:hover { box-shadow:0 0 0 1px rgba(200,220,255,.6), 0 6px 16px rgba(0,0,0,.55); }
.board-wrap:is(.surface-paper, .surface-grid, .surface-fridge) .board-empty, .board-wrap:is(.surface-paper, .surface-grid, .surface-fridge) .board-empty a { color:#555; text-shadow:none; }
.board-wrap:is(.surface-paper, .surface-grid, .surface-fridge) .note.unsorted { color:#6b6b70; }

/* ---------- what joins them: string (default), laser, wire ---------- */
.board-wrap.str-laser svg.strings { filter:drop-shadow(0 0 2px var(--string)) drop-shadow(0 0 7px var(--string)); }
.board-wrap.str-laser .str .line { stroke-width:1.7; }
.board-wrap.str-laser .str .core { stroke:#fff; stroke-width:.6; opacity:.9; }
.board-wrap.str-laser .str.mention .line, .board-wrap.str-laser .str.mention .core { stroke-dasharray:6 6; }
/* the pulse fades the whole layer of strings, which the compositor does alone: pulsing each string repainted the
   whole board every frame, and a zoomed-in board fell behind and showed flat patches of cork */
.board-wrap.str-laser.animate svg.strings { animation:laser 2.6s ease-in-out infinite alternate; }
@keyframes laser { from { opacity:.65; } to { opacity:1; } }
.board-wrap.str-wire svg.strings { filter:drop-shadow(1px 2px 1px rgba(0,0,0,.4)); }
.board-wrap.str-wire .str .line { stroke-width:2; }
.board-wrap.str-wire .str .core { stroke:rgba(255,255,255,.6); stroke-width:.7; transform:translate(-.4px, -.5px); }
.board-wrap.str-wire .str.mention .line { stroke-dasharray:none; stroke-width:1.2; opacity:.75; }
.board-wrap.str-laser .board-legend i { box-shadow:0 0 6px var(--string); }

/* ---------- cards: fridge magnets ---------- */
.board-wrap.cards-magnets .note { width:auto; max-width:calc(230px * var(--s)); padding:0; background:none; box-shadow:none;
  font-size:calc(13px * var(--s)); }
.board-wrap.cards-magnets .note.lifted { filter:drop-shadow(0 14px 12px rgba(0,0,0,.35)); }
.board-wrap.cards-magnets .alpha { display:flex; gap:calc(2px * var(--s)); justify-content:center; }
.board-wrap.cards-magnets .alpha b { display:inline-block; font:900 calc(34px * var(--s))/1 "Arial Rounded MT Bold", "Nunito", "Trebuchet MS", system-ui, sans-serif;
  color:hsl(var(--h) 80% 52%); transform:rotate(var(--r)); -webkit-text-stroke:calc(1px * var(--s)) hsl(var(--h) 75% 38%);
  text-shadow:0 1px 0 hsl(var(--h) 80% 70%), 1px 3px 0 hsl(var(--h) 70% 32%), 2px 6px 6px rgba(0,0,0,.35); }
.board-wrap.cards-magnets .tape { margin:calc(6px * var(--s)) auto 0; width:max-content; max-width:calc(200px * var(--s)); padding:2px 7px;
  background:#1d1d1f; color:#f4f4f4; font:600 .72em/1.3 "Courier New", monospace; letter-spacing:.06em; text-transform:uppercase;
  border-radius:2px; box-shadow:1px 2px 3px rgba(0,0,0,.35); text-align:center; }
.board-wrap.cards-magnets .tiles { display:flex; flex-wrap:wrap; gap:calc(4px * var(--s)); justify-content:center; width:calc(200px * var(--s)); }
.board-wrap.cards-magnets .tiles i { font:500 calc(13px * var(--s))/1 Georgia, "Times New Roman", serif; font-style:normal; color:#1d1d1f;
  background:#fdfdfb; padding:calc(4px * var(--s)) calc(6px * var(--s)); transform:rotate(var(--r));
  box-shadow:0 1px 0 #d9d7d0, 1px 3px 4px rgba(0,0,0,.3); }
.board-wrap.cards-magnets .tiles i.tag { background:var(--dot,#555); color:#fff; font:700 calc(9px * var(--s))/1.2 system-ui, sans-serif;
  letter-spacing:.08em; text-transform:uppercase; align-self:center; }
.board-wrap.cards-magnets .postcard { width:calc(210px * var(--s)); background:#fffdf7; padding:calc(6px * var(--s));
  box-shadow:0 1px 1px rgba(0,0,0,.2), 2px 8px 14px -4px rgba(0,0,0,.45); border-radius:3px; }
.board-wrap.cards-magnets .postcard .photo { height:calc(58px * var(--s)); border-radius:2px; display:flex; flex-direction:column;
  justify-content:center; align-items:center; color:#fff; text-shadow:0 1px 3px rgba(0,0,0,.45); overflow:hidden;
  background:linear-gradient(180deg, hsl(var(--h) 75% 70%) 0 45%, hsl(calc(var(--h) + 30) 60% 45%) 45% 100%); }
.board-wrap.cards-magnets .postcard .greet { font:italic .7em Georgia, serif; }
.board-wrap.cards-magnets .postcard .place { font:900 1.25em/1 "Arial Black", Impact, system-ui, sans-serif; letter-spacing:.02em;
  text-transform:uppercase; text-align:center; padding:0 .3em; }
.board-wrap.cards-magnets .postcard .back { position:relative; padding:calc(7px * var(--s)) calc(44px * var(--s)) calc(4px * var(--s)) calc(3px * var(--s)); }
.board-wrap.cards-magnets .postcard .back .t { font:italic 600 1em/1.25 "Bradley Hand", "Segoe Print", "Comic Sans MS", cursive; color:#253a6b; }
.board-wrap.cards-magnets .postcard .stampbox { position:absolute; right:calc(4px * var(--s)); top:calc(6px * var(--s)); width:calc(30px * var(--s));
  height:calc(36px * var(--s)); background:repeating-linear-gradient(45deg, var(--dot) 0 3px, #fff 3px 6px); border:2px dotted #fff;
  outline:1px solid var(--dot); }
.board-wrap.cards-magnets .postcard .postmark { position:absolute; right:calc(14px * var(--s)); top:calc(32px * var(--s));
  font:700 .55em/1 system-ui, sans-serif; letter-spacing:.06em; text-transform:uppercase; color:rgba(30,60,120,.65);
  border:1.5px solid rgba(30,60,120,.5); border-radius:50%; padding:.7em .4em; transform:rotate(-14deg); }
.board-wrap.cards-magnets .souvenir { width:calc(190px * var(--s)); padding:calc(10px * var(--s)) calc(12px * var(--s)) calc(12px * var(--s));
  text-align:center; color:#fff; border-radius:calc(22px * var(--s)); border:calc(4px * var(--s)) solid #fff;
  background:radial-gradient(circle at 30% 20%, hsl(var(--h) 80% 62%), hsl(var(--h) 70% 38%));
  box-shadow:0 0 0 1px rgba(0,0,0,.15), 2px 10px 16px -4px rgba(0,0,0,.5), inset 0 2px 0 rgba(255,255,255,.35); }
.board-wrap.cards-magnets .souvenir .horseshoe { width:calc(34px * var(--s)); height:auto; fill:#e23b3b; stroke:#fff; stroke-width:1.5; }
.board-wrap.cards-magnets .souvenir .horseshoe .tip { fill:#d9dde2; }
.board-wrap.cards-magnets .souvenir .k { font:700 .6em/1 system-ui, sans-serif; letter-spacing:.16em; text-transform:uppercase; opacity:.85; margin-top:.3em; }
.board-wrap.cards-magnets .souvenir .t { font:900 1.5em/1.05 "Arial Black", Impact, system-ui, sans-serif; margin-top:.25em;
  text-shadow:0 2px 0 rgba(0,0,0,.25); }
.board-wrap.cards-magnets .souvenir .n { font-size:.8em; color:#fff; opacity:.9; margin-top:.3em; }
.board-wrap.cards-magnets .note.epic { padding:0; background:none; }
.board-wrap.cards-magnets .note.epic::before { display:none; }
.board-wrap.cards-magnets .note.epic.proposed .souvenir { border-style:dashed; }
.board-wrap.cards-magnets .note.epic.proposed { outline:none; }
.board-wrap.cards-magnets .note.live { outline:none; }
.board-wrap.cards-magnets .note.live > :first-child { outline:2.5px solid #2f8a4a; outline-offset:5px; border-radius:4px; }
.board-wrap.cards-magnets .note .now { top:-14px; }

/* ---------- cards: hologram ---------- */
.board-wrap.cards-hologram .note { --holo:#7fd8ff; color:#e8fbff; background:linear-gradient(160deg, color-mix(in srgb, var(--holo) 22%, transparent),
  color-mix(in srgb, var(--holo) 8%, transparent)), repeating-linear-gradient(0deg, rgba(255,255,255,.07) 0 1px, transparent 1px 3px);
  border:1px solid color-mix(in srgb, var(--holo) 80%, transparent); border-radius:6px;
  background-color:rgba(6,12,24,.5);                         /* a tint, not a backdrop blur: strings behind stay faint */
  box-shadow:0 0 14px color-mix(in srgb, var(--holo) 45%, transparent), inset 0 0 18px color-mix(in srgb, var(--holo) 25%, transparent);
  text-shadow:0 0 6px color-mix(in srgb, var(--holo) 70%, transparent); }
.board-wrap.cards-hologram .note .t { font-family:"SF Mono", Menlo, Consolas, monospace; font-weight:600; letter-spacing:.01em; }
.board-wrap.cards-hologram .note .stage, .board-wrap.cards-hologram .note .stamp { color:var(--holo); border-color:var(--holo); }
.board-wrap.cards-hologram .note .stage::before { background:var(--holo); box-shadow:0 0 6px var(--holo); }
.board-wrap.cards-hologram .note .pin { width:10px; height:10px; margin-left:-5px; top:6px; background:var(--holo);
  box-shadow:0 0 6px var(--holo), 0 0 14px var(--holo); }
.board-wrap.cards-hologram .note::after { content:""; position:absolute; inset:5px; pointer-events:none;
  background:linear-gradient(var(--holo), var(--holo)) top left / 10px 1.5px, linear-gradient(var(--holo), var(--holo)) top left / 1.5px 10px,
    linear-gradient(var(--holo), var(--holo)) bottom right / 10px 1.5px, linear-gradient(var(--holo), var(--holo)) bottom right / 1.5px 10px;
  background-repeat:no-repeat; opacity:.9; }
.board-wrap.cards-hologram .queued { --holo:#9fb7d4; } .board-wrap.cards-hologram .research { --holo:#ffe066; }
.board-wrap.cards-hologram .review { --holo:#5ab0ff; } .board-wrap.cards-hologram .writing { --holo:#ffa94d; }
.board-wrap.cards-hologram .publish { --holo:#4cff9a; } .board-wrap.cards-hologram .done { --holo:#b8f3ff; }
.board-wrap.cards-hologram .parked { --holo:#ff5c7a; }
.board-wrap.cards-hologram .note.epic { --holo:#d38bff; background-image:linear-gradient(160deg, rgba(211,139,255,.28), rgba(211,139,255,.08)),
  repeating-linear-gradient(0deg, rgba(255,255,255,.07) 0 1px, transparent 1px 3px); }
.board-wrap.cards-hologram .note.epic::before { background:var(--holo); opacity:.6; }
.board-wrap.cards-hologram .note.epic { color:#e8fbff; }
.board-wrap.cards-hologram .note.epic .k, .board-wrap.cards-hologram .note.epic .n { color:var(--holo); }
.board-wrap.cards-hologram .note.unsorted { --holo:transparent; border:none; box-shadow:none; background:none; color:#cfe2ff; }
.board-wrap.cards-hologram .note.unsorted::after { display:none; }
.board-wrap.cards-hologram .note.live { outline-color:#4cff9a; box-shadow:0 0 22px #4cff9a; }
.board-wrap.cards-hologram:is(.surface-paper, .surface-grid, .surface-fridge) .note,
.board-wrap.cards-hologram:is(.surface-paper, .surface-grid, .surface-fridge) .note.epic {    /* light boards: dark ink */
  color:color-mix(in srgb, var(--holo) 25%, #0b1b2b); text-shadow:none;
  background-color:color-mix(in srgb, var(--holo) 14%, rgba(255,255,255,.5)); }
.board-wrap.cards-hologram:is(.surface-paper, .surface-grid, .surface-fridge) .note .stage,
.board-wrap.cards-hologram:is(.surface-paper, .surface-grid, .surface-fridge) .note .stamp,
.board-wrap.cards-hologram:is(.surface-paper, .surface-grid, .surface-fridge) .note.epic :is(.k, .n) {
  color:color-mix(in srgb, var(--holo) 55%, #0b1b2b); border-color:currentColor; }
.board-wrap.cards-hologram.animate .note:not(.unsorted) { animation:holo 7s infinite; }
.board-wrap.cards-hologram.animate .note:nth-child(3n) { animation-delay:-2.3s; }
.board-wrap.cards-hologram.animate .note:nth-child(3n+1) { animation-delay:-4.6s; }
@keyframes holo { 0%, 92%, 100% { opacity:1; } 93% { opacity:.72; } 94% { opacity:1; } 96% { opacity:.85; } }
@media (prefers-reduced-motion: reduce) { .board-wrap .note, .board-wrap svg.strings { animation:none !important; } }

/* ---------- the gear's panel ---------- */
.board-style { position:absolute; right:24px; top:72px; width:300px; max-width:calc(100% - 48px); z-index:5; cursor:auto;
  background:var(--bg); color:var(--fg); border-radius:12px; padding:12px 14px; box-shadow:0 18px 40px -10px rgba(0,0,0,.5), 0 0 0 1px rgba(0,0,0,.06); }
.board-style[hidden] { display:none; }
.board-style .lbl { font:600 10.5px/1 system-ui, sans-serif; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); margin:.7rem 0 .35rem; }
.board-style .lbl:first-child { margin-top:0; }
.board-style .seg { display:flex; flex-wrap:wrap; gap:4px; }
.board-style .seg button { border:1px solid var(--rule); background:var(--bg); color:var(--fg); border-radius:999px; padding:.28rem .65rem;
  font:600 12px system-ui, sans-serif; cursor:pointer; }
.board-style .seg button.on { background:var(--fg); color:var(--bg); border-color:var(--fg); }
.board-style .dots { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
.board-style .dot { width:26px; height:26px; border-radius:50%; border:none; padding:0; background:var(--c); cursor:pointer;
  box-shadow:inset 0 0 0 1px rgba(0,0,0,.15); }
.board-style .dot[hidden] { display:none; }
.board-style .dot.on { box-shadow:inset 0 0 0 1px rgba(0,0,0,.15), 0 0 0 2px var(--bg), 0 0 0 4px var(--fg); }
.board-style .dot.own { position:relative; overflow:hidden; background:conic-gradient(red, yellow, lime, cyan, blue, magenta, red); }
.board-style .dot.own input { position:absolute; inset:-6px; width:40px; height:40px; opacity:0; cursor:pointer; }
.board-style .more { display:block; margin-top:.8rem; font-size:12px; }
/* ---------- researchers walk on top of a note a model is working on (assets/researchers.js) ---------- */
.board-wrap .note canvas.lab-stage { position:absolute; left:0; top:auto; bottom:100%; width:100%; height:150px;
  pointer-events:none; image-rendering:pixelated; }
.board-wrap:not(.cards-sticky) .note[data-lab] { --r:0deg !important; }   /* level ground to walk on */
/* sticky notes: the researcher stands inside the card on its bottom edge and tilts with it */
.board-wrap.cards-sticky .note canvas.lab-stage { bottom:0; }
.board-wrap.cards-sticky .note[data-lab] { padding-bottom:calc(11px * var(--s) + 46px); }

/* ---------- surface: arcade ---------- */
.board-wrap.surface-arcade { background-color:#1d2b53; border-radius:0;
  background-image:linear-gradient(rgba(255,255,255,.07) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.07) 1px, transparent 1px),
    radial-gradient(ellipse at 50% 125%, rgba(126,37,83,.6), transparent 62%);
  background-size:calc(var(--gz,1) * 32px) calc(var(--gz,1) * 32px), calc(var(--gz,1) * 32px) calc(var(--gz,1) * 32px), 100% 100%;
  background-position:var(--gx,0) var(--gy,0), var(--gx,0) var(--gy,0), 0 0;
  box-shadow:inset 0 0 0 6px #000, inset 0 0 0 10px #5f574f, inset 0 0 0 12px #000; }
.board-wrap.surface-arcade .note.unsorted, .board-wrap.surface-arcade .board-empty { color:#fff1e8; }

/* ---------- strings: pixel (right-angle elbows) ---------- */
.board-wrap.str-pixel .str path { stroke-linecap:square; stroke-linejoin:miter; }
.board-wrap.str-pixel svg.strings { filter:drop-shadow(2px 2px 0 rgba(0,0,0,.45)); }
.board-wrap.str-pixel .str .line { stroke-width:3; }
.board-wrap.str-pixel .str.link .line { stroke-width:2.5; }
.board-wrap.str-pixel .str.mention .line { stroke-width:2; stroke-dasharray:6 6; opacity:.9; }
.board-wrap.str-pixel .board-legend i { border-top-width:3px; }

/* ---------- cards: pixel (flat 8-bit colours, stepped borders, hard shadows) ---------- */
.board-wrap.cards-pixel .note { --px:calc(3px * var(--s)); --ink:#1d1d1f; transform:translate(-50%,-50%);
  background:var(--pix,#c2c3c7); color:var(--ink); border-radius:0; letter-spacing:.01em;
  font:700 calc(12px * var(--s))/1.4 ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  padding:calc(17px * var(--s)) calc(12px * var(--s)) calc(10px * var(--s));
  box-shadow:0 calc(-1 * var(--px)) 0 0 var(--ink), 0 var(--px) 0 0 var(--ink), calc(-1 * var(--px)) 0 0 0 var(--ink), var(--px) 0 0 0 var(--ink),
    inset var(--px) var(--px) 0 0 rgba(255,255,255,.5), inset calc(-1 * var(--px)) calc(-1 * var(--px)) 0 0 rgba(0,0,0,.18),
    calc(3 * var(--px)) calc(3 * var(--px)) 0 0 rgba(0,0,0,.35); }
.board-wrap.cards-pixel .note.lifted { box-shadow:0 calc(-1 * var(--px)) 0 0 var(--ink), 0 var(--px) 0 0 var(--ink),
  calc(-1 * var(--px)) 0 0 0 var(--ink), var(--px) 0 0 0 var(--ink), calc(5 * var(--px)) calc(5 * var(--px)) 0 0 rgba(0,0,0,.35); }
.board-wrap.cards-pixel .note .t { font-family:inherit; font-weight:800; }
.board-wrap.cards-pixel .note .pin { border-radius:0; width:calc(8px * var(--s)); height:calc(8px * var(--s)); top:calc(5px * var(--s));
  margin-left:calc(-4px * var(--s)); background:#ff004d; box-shadow:0 0 0 2px var(--ink), 2px 2px 0 2px rgba(0,0,0,.3); }
.board-wrap.cards-pixel .note .stage { color:rgba(0,0,0,.7); font-size:.78em; }
.board-wrap.cards-pixel .note .stage::before { border-radius:0; background:var(--ink); }
.board-wrap.cards-pixel .note .stamp { transform:none; border-radius:0; border-width:2px; color:var(--ink); border-color:var(--ink);
  background:rgba(255,255,255,.55); opacity:1; }
.board-wrap.cards-pixel .note .now { border-radius:0; background:#00e436; color:#1d1d1f; font:800 9.5px/1 ui-monospace, Menlo, monospace;
  box-shadow:0 0 0 2px #1d1d1f, 2px 2px 0 2px rgba(0,0,0,.35); top:-12px; }
.board-wrap.cards-pixel .note.live { outline:3px dashed #00e436; outline-offset:6px; }
.board-wrap.cards-pixel .queued   { --pix:#c2c3c7; } .board-wrap.cards-pixel .research { --pix:#ffec27; }
.board-wrap.cards-pixel .review   { --pix:#29adff; } .board-wrap.cards-pixel .writing  { --pix:#ffa300; }
.board-wrap.cards-pixel .publish  { --pix:#00e436; } .board-wrap.cards-pixel .done     { --pix:#fff1e8; }
.board-wrap.cards-pixel .parked   { --pix:#ff77a8; }
.board-wrap.cards-pixel .note.epic { --pix:#7e2553; --ink:#000; color:#fff1e8; background-image:none;
  font:700 calc(13px * var(--s))/1.35 ui-monospace, "SF Mono", Menlo, Consolas, monospace; }
.board-wrap.cards-pixel .note.epic::before { display:none; }
.board-wrap.cards-pixel .note.epic .k { color:#ffec27; font:800 .7em/1 ui-monospace, Menlo, monospace; letter-spacing:.2em; }
.board-wrap.cards-pixel .note.epic .t { font-family:inherit; font-weight:800; font-size:1.4em; }
.board-wrap.cards-pixel .note.epic .n { color:#fff1e8; opacity:.85; }
.board-wrap.cards-pixel .note.epic .pin { background:#ffec27; }
.board-wrap.cards-pixel .note.epic.proposed { outline:3px dashed #ffec27; outline-offset:6px; }
.board-wrap.cards-pixel .note.unsorted { background:none; box-shadow:none; }
.board-wrap.cards-pixel .note .grip { background:linear-gradient(135deg, transparent 50%, var(--ink) 50%); }
"""

# ---------- actions (POST, then redirect back) ----------

# ---------- the avatar studio: researchers built from pieces, or by asking a model (assets/avatars.js) ----------

def avatar_link() -> str:
    """The Settings page's way into the studio, with what is customised so far."""
    n, off = len(avatars.custom()), len(avatars.off())
    bits = [_n("{n} of your own", "{n} of your own", n) if n else "", _n("{n} staying home", "{n} staying home", off) if off else ""]
    now = " · ".join(b for b in bits if b) or _("built-in researchers only")
    return f"""<div class="actions display avatar-link">
  <h3>{icon("person")}{_("Avatars")}</h3>
  <p class="hint">{_("Build a researcher from pieces, or ask a model to draw one. Now: {now}.").format(now=escape(now))}</p>
  <p><a class="btn" href="/avatars">{_("Avatar studio")}</a>
  <a class="btn" href="/sprites">{_("Sprite studio")}</a></p>
  <p class="hint">{_("Sprite studio: write a sprite spec by hand ({file}) and see every pose.").format(file="<code>SPRITES.md</code>")}</p>
</div>"""

async def avatars_page(request: Request) -> HTMLResponse:
    lede = _("Build a researcher from pieces, or describe one to a model. Saved avatars join their crew. "
             "Stored in {file}.").format(file="<code>data/sprites.json</code>")
    body = (f'<style>{AVATAR_CSS}</style><h1>{_("Avatar studio")}</h1>'
            f'<p class="hint lede">{lede}</p>'
            f'<div id="studio" data-next="{escape(avatars.next_key(), quote=True)}"><noscript>{_("The studio needs JavaScript.")}</noscript></div>'
            f'{script("researchers.js")}{script_i18n("avatars.js")}{script("avatars.js")}')
    return page(_("Avatar studio"), body)

async def sprite_studio(request: Request) -> Response:
    """The sprite studio (tools/sprites.html): a sprite's spec on the left, the real renderer on the right. The page
    also opens on its own from a served copy of the repo; here its script gets the stamped address."""
    try:
        html = (TOOLS / "sprites.html").read_text()
    except OSError:
        return PlainTextResponse(_("tools/sprites.html is missing."), status_code=404)
    html = html.replace('src="../assets/researchers.js"', f'src="{asset("researchers.js")}"')
    html = html.replace('fetch("example-sprite.json")', 'fetch("/sprites/example.json")')
    html = html.replace("<!--back-->", f'<p><a href="/settings">&larr; {_("Settings")}</a></p>')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

async def sprite_example(request: Request) -> Response:
    try:
        return Response((TOOLS / "example-sprite.json").read_bytes(), media_type="application/json", headers={"Cache-Control": "no-cache"})
    except OSError:
        return JSONResponse({"error": "no example"}, status_code=404)

async def avatar_models(request: Request) -> JSONResponse:
    try:
        found = await run_in_threadpool(agents.avatar_models)
    except Exception:
        found = []
    return JSONResponse({"models": found})

async def _json(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}

async def avatar_ask(request: Request) -> JSONResponse:
    """The designer edits the spec it was sent; the page checks the reply and may send it back with `errors`."""
    if not same_origin(request):
        return JSONResponse({"error": "Cross-site request refused."}, status_code=403)
    data = await _json(request)
    spec, look = data.get("spec"), data.get("look")
    if not isinstance(spec, dict) or len(json.dumps(spec)) > avatars.MAX_SPEC * 2:
        return JSONResponse({"error": _("Avatar arrived incomplete. Try again.")}, status_code=400)
    errors = data.get("errors") if isinstance(data.get("errors"), list) else None
    try:
        out = await run_in_threadpool(agents.edit_avatar, spec, str(data.get("message") or ""),
                                      look if isinstance(look, dict) else None, str(data.get("model") or "")[:100] or None,
                                      data.get("image") if isinstance(data.get("image"), str) else None,
                                      [str(e) for e in errors] if errors else None,
                                      data.get("catalog") if isinstance(data.get("catalog"), dict) else None)
    except ValueError as e:                # a picture that didn't decode, or was too big
        return JSONResponse({"error": str(e)}, status_code=400)
    except asyncio.TimeoutError:
        return JSONResponse({"error": _("Model took too long. Try a smaller model or a simpler ask.")}, status_code=504)
    except Exception as e:                 # Ollama closed, model missing
        why = f"{type(e).__name__}: {str(e)[:160]}"
        return JSONResponse({"error": _("Model didn't answer ({error}). Is Ollama running?").format(error=why)}, status_code=502)
    return JSONResponse(out)

async def avatar_save(request: Request) -> JSONResponse:
    if not same_origin(request):
        return JSONResponse({"error": "Cross-site request refused."}, status_code=403)
    data = await _json(request)
    try:
        key = avatars.save(str(data.get("key") or "").strip(), data.get("spec"), str(data.get("replace") or ""))
    except avatars.AvatarError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "key": key})

async def avatar_action(request: Request) -> JSONResponse:
    """Switch a researcher on or off, or delete one of your own."""
    if not same_origin(request):
        return JSONResponse({"error": "Cross-site request refused."}, status_code=403)
    key, action = request.path_params["key"], request.path_params["action"]
    try:
        if action in ("on", "off"):
            avatars.set_on(key, action == "on")
        elif action == "delete":
            if not avatars.delete(key):
                return JSONResponse({"error": _("No avatar called {key}.").format(key=key)}, status_code=404)
        else:
            return JSONResponse({"error": _("Unknown action.")}, status_code=404)
    except avatars.AvatarError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True})

AVATAR_CSS = """
main:has(#studio) { max-width: 72rem; }
.lede { max-width: 46rem; margin-top:-.4rem; }
canvas.px { image-rendering: pixelated; image-rendering: crisp-edges; display:block; }
.av-studio { display:grid; grid-template-columns: minmax(0, 330px) minmax(0, 1fr); gap:1.4rem; align-items:start; margin-top:1rem; }
@media (max-width: 820px) { .av-studio { grid-template-columns: 1fr; } }
.av-left { position:sticky; top:1rem; }
@media (max-width: 820px) { .av-left { position:static; } }
/* a little room: a wall with a window of light, and floorboards the avatar stands on */
.av-room { position:relative; border-radius:14px; overflow:hidden; border:1px solid var(--rule);
  background:
    radial-gradient(circle at 78% 22%, rgba(255,236,170,.55) 0 2.4rem, transparent 2.5rem),
    linear-gradient(to bottom, transparent 0 72%, rgba(0,0,0,.06) 72% 73%, transparent 73%),
    repeating-linear-gradient(90deg, rgba(0,0,0,.035) 0 1px, transparent 1px 38px) 0 72% / 100% 28% no-repeat,
    linear-gradient(to bottom, #efe7d8 0 72%, #d9b98c 72% 100%); }
:root[data-theme="dark"] .av-room { background:
    radial-gradient(circle at 78% 22%, rgba(140,160,255,.25) 0 2.4rem, transparent 2.5rem),
    repeating-linear-gradient(90deg, rgba(255,255,255,.04) 0 1px, transparent 1px 38px) 0 72% / 100% 28% no-repeat,
    linear-gradient(to bottom, #24232b 0 72%, #3b3027 72% 100%); }
@media (prefers-color-scheme: dark) { :root:not([data-theme]) .av-room { background:
    radial-gradient(circle at 78% 22%, rgba(140,160,255,.25) 0 2.4rem, transparent 2.5rem),
    repeating-linear-gradient(90deg, rgba(255,255,255,.04) 0 1px, transparent 1px 38px) 0 72% / 100% 28% no-repeat,
    linear-gradient(to bottom, #24232b 0 72%, #3b3027 72% 100%); } }
.stage-canvas { width:304px; height:264px; margin:0 auto; max-width:100%; object-fit:contain; }
.now-doing { position:absolute; left:.7rem; top:.55rem; font:600 .72rem ui-monospace, Menlo, monospace; color:#6b5a45;
  text-transform:uppercase; letter-spacing:.08em; }
.av-acts, .av-buttons, .chips, .suggest { display:flex; flex-wrap:wrap; gap:.35rem; margin:.6rem 0; }
.av-acts button, .chip, .suggest button { font-size:.78rem; padding:.3rem .6rem; border-radius:999px; }
.av-acts button.on, .chip.on { background:var(--fg); color:var(--bg); border-color:var(--fg); }
.av-fields { display:grid; grid-template-columns:1fr 1fr; gap:.5rem; }
.av-fields label { display:flex; flex-direction:column; gap:.2rem; font-size:.8rem; font-weight:600; color:var(--muted); margin:0; }
.av-fields input { width:100%; }
.av-fields .hint { font-size:.72rem; min-height:1em; } .av-fields .hint.bad { color:#c92a2a; }
.av-buttons button { font-size:.82rem; padding:.4rem .7rem; }
.av-status { font:.78rem/1.4 ui-monospace, Menlo, monospace; margin:.2rem 0 0; }
.av-status.ok { color:#2f7d46; } .av-status.bad { color:#c92a2a; }
.av-tabs { display:flex; flex-wrap:wrap; gap:.3rem; border-bottom:1px solid var(--rule); padding-bottom:.6rem; }
.av-tabs button { font-size:.85rem; padding:.4rem .8rem; border-radius:8px 8px 8px 8px; }
.av-tabs button[aria-selected="true"] { background:var(--accent); border-color:var(--accent); color:#fff; }
.av-panel { padding-top:.4rem; }
.av-group h3 { font-size:.78rem; text-transform:uppercase; letter-spacing:.07em; color:var(--muted); margin:1rem 0 .45rem; }
.tiles { display:grid; grid-template-columns:repeat(auto-fill, minmax(84px, 1fr)); gap:.45rem; }
.tile { display:flex; flex-direction:column; align-items:center; gap:.25rem; padding:.45rem .3rem .4rem; border-radius:10px;
  background:var(--quote); border:2px solid transparent; font-size:.74rem; font-weight:600; color:var(--fg); transition:transform .12s; }
.tile:hover { transform:translateY(-2px); border-color:var(--rule); }
.tile.on { border-color:var(--accent); box-shadow:0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent); }
.tile canvas { margin:0 auto; }
/* in the dark, pieces sit on a lighter card so the sprites' dark outlines still read */
:root[data-theme="dark"] .tile, :root[data-theme="dark"] .visitor canvas, :root[data-theme="dark"] canvas.mini { background:#34333b; }
:root[data-theme="dark"] .now-doing { color:#a99a86; }
@media (prefers-color-scheme: dark) { :root:not([data-theme]) .tile, :root:not([data-theme]) .visitor canvas,
  :root:not([data-theme]) canvas.mini { background:#34333b; } :root:not([data-theme]) .now-doing { color:#a99a86; } }
.visitor canvas { border-radius:8px; }
.swatches { display:flex; flex-wrap:wrap; gap:.35rem; align-items:center; }
.swatch { width:1.7rem; height:1.7rem; padding:0; border-radius:50%; background:var(--c); border:2px solid var(--rule); }
.swatch.on { outline:2px solid var(--fg); outline-offset:2px; }
.swatch.own { display:inline-grid; place-items:center; overflow:hidden; cursor:pointer;
  background:conic-gradient(#e03131, #f08c00, #ffd43b, #2f9e44, #1c7ed6, #7048e8, #e03131); }
.swatch.own input { opacity:0; width:100%; height:100%; cursor:pointer; }
.extra { display:flex; align-items:center; gap:.7rem; padding:.45rem .6rem; border:1px solid var(--rule); border-radius:10px; margin:.35rem 0; }
.extra span { flex:1; font-size:.85rem; }
canvas.mini { background:var(--quote); border-radius:4px; }
.av-json { width:100%; font:12px/1.4 ui-monospace, Menlo, monospace; margin:.4rem 0; }
/* ask a model */
.av-chat { border:1px solid var(--rule); border-radius:14px; padding:.8rem; }
.av-log { display:flex; flex-direction:column; gap:.5rem; max-height:22rem; overflow-y:auto; }
.av-log:empty { display:none; }
.msg { max-width:80%; padding:.55rem .75rem; border-radius:14px; font-size:.9rem; display:flex; gap:.6rem; align-items:center; }
.msg p { margin:0; }
.msg.me { align-self:flex-end; background:var(--chat, var(--accent)); color:var(--chat-ink, #fff); border-bottom-right-radius:4px; }
.msg.me img { width:44px; height:44px; object-fit:cover; border-radius:6px; }
.msg.them { align-self:flex-start; background:var(--quote); border-bottom-left-radius:4px; flex-wrap:wrap; }
.msg.refused { border:1px solid #e8a33d; }
.msg.pending p { color:var(--muted); animation:blink 1.4s ease-in-out infinite; }
.msg button.link { background:none; border:0; padding:0; color:var(--accent); font-size:.82rem; text-decoration:underline; }
.av-ask .row { display:flex; gap:.5rem; align-items:center; flex-wrap:wrap; margin-top:.5rem; }
.av-ask .row.grow textarea { flex:1; min-width:12rem; }
.av-ask .model { display:flex; align-items:center; gap:.4rem; font-size:.82rem; font-weight:600; margin:0; }
.attach.sees { border-color:#2f9e44; }
.av-thumb { display:inline-flex; align-items:center; gap:.3rem; }
.av-thumb img { width:2.4rem; height:2.4rem; object-fit:cover; border-radius:6px; border:1px solid var(--rule); }
.av-thumb button { padding:.1rem .45rem; }
.av-flag { margin:.5rem 0 0; padding:.45rem .7rem; border-radius:8px; font-size:.85rem; background:#fff3bf; border:1px solid #f0b429; color:#5c3c00; }
:root[data-theme="dark"] .av-flag { background:rgba(255,212,59,.13); color:#ffd43b; border-color:rgba(255,212,59,.45); }
@media (prefers-color-scheme: dark) { :root:not([data-theme]) .av-flag { background:rgba(255,212,59,.13); color:#ffd43b; border-color:rgba(255,212,59,.45); } }
/* who visits */
.av-cast h3 { font-size:.85rem; margin:1rem 0 .5rem; color:var(--muted); }
.crew-row { display:grid; grid-template-columns:repeat(auto-fill, minmax(118px, 1fr)); gap:.5rem; }
.visitor { display:flex; flex-direction:column; align-items:center; gap:.2rem; padding:.6rem .4rem; border:1px solid var(--rule);
  border-radius:12px; text-align:center; transition:opacity .2s, filter .2s; }
.visitor.off canvas { opacity:.35; filter:grayscale(1); }
.visitor.editing { border-color:var(--accent); }
.visitor strong { font-size:.82rem; } .visitor small { color:var(--muted); font-size:.7rem; }
.visitor .switch { font-size:.75rem; margin:.2rem 0; }
.visitor-acts { display:flex; gap:.3rem; }
.visitor-acts button { font-size:.72rem; padding:.25rem .55rem; }
.visitor-acts .danger:hover { border-color:#c92a2a; color:#c92a2a; }
.avatar-link .btn { text-decoration:none; }
"""

def same_origin(request: Request) -> bool:
    """Refuse form posts from other websites (a page elsewhere can't approve or publish for you)."""
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if value:
            return urlparse(value).netloc == request.headers.get("host")
    return True

def go(path: str, done: str = "", error: str = "") -> RedirectResponse:
    q = f"?error={error}" if error else f"?done={done}" if done else ""
    path, _, fragment = path.partition("#")   # the message goes before a #section, or the server never sees it
    return RedirectResponse(path + q + (f"#{fragment}" if fragment else ""), status_code=303)

def _back(form) -> str:
    back = str(form.get("back") or "/")
    return back if back.startswith("/") and not back.startswith("//") else "/"

async def act(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    slug, action = request.path_params["slug"], request.path_params["action"]
    m = store.get_mission(slug)
    if not m:
        return PlainTextResponse("No such mission.", status_code=404)
    here, form, now = f"/m/{quote(slug)}", await request.form(), clock.now()
    if action == "approve":
        if m.state != State.BRIEF_READY or m.approved:
            return go(here, error="state")
        m.approved = True
        store.save_mission(m)
        store.log_event(slug, now, m.state.value, m.state.value, "brief approved by human")
        done = "approved"
    elif action == "publish":
        if m.state != State.ARTICLE_READY or m.reviewed:
            return go(here, error="state")
        m.reviewed = True
        store.save_mission(m)
        store.log_event(slug, now, m.state.value, m.state.value, "article approved for publishing by human")
        done = "published"
    elif action == "reject":
        notes = str(form.get("notes") or "").strip()
        if not notes:
            return go(here, error="notes")
        try:
            orchestrator.reject(m, notes, now)
        except ValueError:
            return go(here, error="state")
        done = "rejected"
    elif action == "requeue":
        dead = m.state == State.DEAD_LEAD
        try:
            orchestrator.requeue(m, now)
        except ValueError:
            return go(here, error="state")
        done = "revived" if dead else "requeued"
    elif action == "dead":
        try:
            orchestrator.mark_dead_lead(m, str(form.get("verdict") or ""), str(form.get("why") or ""), now,
                                        busy={e["slug"] for e in running_now()})
        except ValueError:
            return go(here, error="dead_lead")
        return go(here, done="dead_lead")
    elif action == "claim":
        try:
            claim_tags.set_tag(m, str(form.get("tag") or ""), "human", "", now)
        except ValueError:
            return go(here, error="claim")
        return go(here, done="claim_set")
    elif action == "push":
        try:
            orchestrator.push_through(m, now)
        except ValueError:
            return go(here, error="state")
        done = "pushed"
    elif action == "republish":
        back = _back(form)
        try:
            orchestrator.request_publish(m, [str(k) for k in form.getlist("to")], now)
        except ValueError:
            return go(back, error="places" if m.state == State.DONE else "state")
        if not loop_running():
            publish_now()
            return go(back, done="republish_off")
        wake()
        return go(back, done="republish")
    elif action in ("link", "unlink"):
        other = str(form.get("to" if action == "link" else "other") or "")
        try:
            if action == "link":
                orchestrator.link(m.slug, other, str(form.get("why") or "")[:300], now)
            else:
                orchestrator.unlink(m.slug, other, now)
        except ValueError:
            return go(here, error="link" if action == "link" else "state")
        return go(here, done=action + "ed")  # read from the next search on; nothing for the loop to wake for
    elif action == "epic":
        name = str(form.get("epic") or "")
        if name.strip() and not epic_title(name):
            return go(here, error="epic")
        orchestrator.assign_epic(m, name, now)
        return go(here, done="epic_set")    # nothing for the loop to do
    else:
        return PlainTextResponse("Unknown action.", status_code=404)
    if loop_running():
        wake()
    return go(here, done=done)

async def add_mission(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    topic = str(form.get("topic") or "").strip()
    objective = str(form.get("objective") or "").strip()
    slug = str(form.get("slug") or "").strip().lower() or slugify(topic)
    if not topic:
        return go("/", error="topic")
    if not SLUG.match(slug):
        return go("/", error="slug")
    if store.get_mission(slug) or slug in SITE_FOLDERS:
        return go("/", error="exists")
    name = str(form.get("epic") or "")
    if name.strip() and not epic_title(name):
        return go("/", error="epic")
    epic = store.create_epic(name).slug if name.strip() else ""
    rush = form.get("go") == "start"       # "Add and start loop": researched next, and the loop started if off
    store.save_mission(Mission(slug=slug, topic=topic, objective=objective, epic=epic, rush=rush))
    store.log_event(slug, clock.now(), "", State.QUEUED.value,
                    "added in the UI" + (", to be researched next" if rush else ""))
    if loop_running():
        wake()
        done = "added_next" if rush else "added"
    elif rush:
        if not await can_start():
            return go(f"/m/{quote(slug)}", error="notready")
        start_loop(settings.loop_every())
        done = "added_started"
    else:
        done = "added"
    return go(f"/m/{quote(slug)}", done=done)

async def save_inspiration(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    line = store.set_inspiration(str(form.get("inspiration") or "")[:500])
    if line and loop_running():
        wake()
    return go("/", done="inspired" if line else "uninspired")

async def ask_theorist(request: Request) -> Response:
    """The Theorist's turn comes next. ui.py never runs agents: the loop sees the flag and does it."""
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    if not store.get_inspiration():
        return go("/", done="uninspired")
    orchestrator.THINK_FLAG.parent.mkdir(parents=True, exist_ok=True)
    orchestrator.THINK_FLAG.touch()
    if loop_running():
        wake()
    return go("/", done="thinking")

async def epic_action(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    slug, action = request.path_params["slug"], request.path_params["action"]
    if not store.get_epic(slug):
        return PlainTextResponse("No such epic.", status_code=404)
    form, now = await request.form(), clock.now()
    title = str(form.get("title") or "")
    if action in ("approve", "rename") and not epic_title(title):
        return go("/", error="epic")
    try:
        if action == "approve":
            orchestrator.approve_epic(slug, title, now)
            done = "epic_approved"
        elif action == "merge":
            proposed = store.get_epic(slug).status == "proposed"
            orchestrator.merge_epic(slug, str(form.get("into") or ""), now,
                                    busy={e["slug"] for e in running_now()})
            done = "epic_moved" if proposed else "epic_merged"
        elif action == "delete":
            orchestrator.delete_epic(slug)
            done = "epic_deleted"
        elif action == "reject":
            orchestrator.reject_epic(slug, now)
            done = "epic_rejected"
        elif action == "rename":
            orchestrator.rename_epic(slug, title, now)
            done = "epic_renamed"
        else:
            return PlainTextResponse("Unknown action.", status_code=404)
    except ValueError as e:
        return go("/", error="epic_exists" if "already" in str(e) else "epic_busy" if "busy" in str(e) else
                  "epic_target" if action == "merge" else "epic_not_empty" if action == "delete" else "state")
    return go("/", done=done)

async def combine_action(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    slug, action = request.path_params["slug"], request.path_params["action"]
    if not store.get_combine(slug):
        return PlainTextResponse("No such combine.", status_code=404)
    now = clock.now()
    try:
        if action == "approve":
            made = orchestrator.approve_combine(slug, now, busy={e["slug"] for e in running_now()})
            if loop_running():
                wake()
            return go(f"/m/{quote(made.slug)}", done="combined")
        if action == "reject":
            orchestrator.reject_combine(slug, now)
            return go("/", done="combine_rejected")
    except ValueError:
        return go("/", error="combine")
    return PlainTextResponse("Unknown action.", status_code=404)

async def dead_lead_action(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    slug, action = request.path_params["slug"], request.path_params["action"]
    if not store.get_dead_lead(slug):
        return PlainTextResponse("No such dead lead.", status_code=404)
    now = clock.now()
    try:
        if action == "approve":
            orchestrator.approve_dead_lead(slug, now, by="human", busy={e["slug"] for e in running_now()})
            return go(f"/m/{quote(slug)}", done="dead_approved")
        if action == "reject":
            orchestrator.reject_dead_lead(slug, now)
            return go("/", done="dead_rejected")
    except ValueError:
        return go("/", error="dead_lead")
    return PlainTextResponse("Unknown action.", status_code=404)

async def loop_action(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form, action = await request.form(), request.path_params["action"]
    back = _back(form)
    if action == "start":
        if loop_running():
            return go(back, error="running")
        try:
            every = max(1.0, float(form.get("every") or settings.loop_every()))
        except ValueError:
            every = settings.loop_every()
        if not await can_start():
            return go(back, error="notready")
        start_loop(every)
        return go(back, done="started")
    if action == "stop":
        stopping.press(orchestrator.STOP_FLAG)
        return go(back, done="stopping")
    if action == "wake":
        wake()
        return go(back, done="woken")
    return PlainTextResponse("Unknown action.", status_code=404)

app = Starlette(routes=[
    Route("/", home),
    Route("/missions", add_mission, methods=["POST"]),
    Route("/inspiration", save_inspiration, methods=["POST"]),
    Route("/theorist", ask_theorist, methods=["POST"]),
    Route("/settings", settings_page),
    Route("/sprites", sprite_studio),
    Route("/sprites/example.json", sprite_example),
    Route("/avatars", avatars_page),
    Route("/avatars/models", avatar_models),
    Route("/avatars/ask", avatar_ask, methods=["POST"]),
    Route("/avatars/save", avatar_save, methods=["POST"]),
    Route("/avatars/{key}/{action}", avatar_action, methods=["POST"]),
    Route("/chat/models", chat_models),
    Route("/chat/ask", chat_ask, methods=["POST"]),
    Route("/chat/answer", chat_answer),
    Route("/settings/site-url", save_site_url, methods=["POST"]),
    Route("/settings/search", save_search, methods=["POST"]),
    Route("/settings/search/move", move_search, methods=["POST"]),
    Route("/settings/search/toggle", toggle_search, methods=["POST"]),
    Route("/settings/mcp", save_mcp, methods=["POST"]),
    Route("/settings/outreach", save_outreach, methods=["POST"]),
    Route("/settings/email", save_email, methods=["POST"]),
    Route("/settings/publication", save_publication, methods=["POST"]),
    Route("/setup/{action}", setup_action, methods=["POST"]),
    Route("/about", about),
    Route("/settings/animations", save_animations, methods=["POST"]),
    Route("/settings/theme", save_theme, methods=["POST"]),
    Route("/settings/language", save_language, methods=["POST"]),
    Route("/settings/chat-color", save_chat_color, methods=["POST"]),
    Route("/settings/board-style", save_board_style, methods=["POST"]),
    Route("/auto", save_auto, methods=["POST"]),
    Route("/settings/loop-every", save_loop_every, methods=["POST"]),
    Route("/settings/library", save_library, methods=["POST"]),
    Route("/settings/worker", save_worker, methods=["POST"]),
    Route("/settings/ollama", save_ollama, methods=["POST"]),
    Route("/settings/ollama-connect", connect_ollama, methods=["POST"]),
    Route("/settings/provider", save_provider, methods=["POST"]),
    Route("/settings/join-1f916", join_1f916, methods=["POST"]),
    Route("/settings/bluesky", save_bluesky, methods=["POST"]),
    Route("/settings/github", github_action, methods=["POST"]),
    Route("/settings/github/poll", github_poll, methods=["POST"]),
    Route("/settings/custom-channel", add_custom_channel, methods=["POST"]),
    Route("/settings/custom-channel/remove", remove_custom_channel, methods=["POST"]),
    Route("/published", published_page),
    Route("/log", log_page),
    Route("/board", board_page),
    Route("/board/move", board_move, methods=["POST"]),
    Route("/board/style", board_style_save, methods=["POST"]),
    Route("/m/{slug}", mission),
    Route("/m/{slug}/preview", preview),
    Route("/m/{slug}/{action}", act, methods=["POST"]),
    Route("/epics/{slug}/{action}", epic_action, methods=["POST"]),
    Route("/combines/{slug}/{action}", combine_action, methods=["POST"]),
    Route("/dead-leads/{slug}/{action}", dead_lead_action, methods=["POST"]),
    Route("/loop/{action}", loop_action, methods=["POST"]),
    Route("/api/signature", api_signature),
    Route("/api/flow", api_flow),
    Route("/api/board", api_board),
    Mount("/assets", FreshFiles(directory=ASSETS)),
    Mount("/site", FreshFiles(directory="site", html=True, check_dir=False)),
])
