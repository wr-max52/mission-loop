"""Local web UI. Run it with `python run.py ui`, then open http://127.0.0.1:8770.

It is a thin layer over the same store and orchestrator functions the CLI uses: it reads missions,
shows briefs and articles, and records human decisions (approve, reject, publish, requeue, push). It never
runs agents itself. Work happens in the loop, which the UI can start, stop and wake. Bound to
127.0.0.1 only; nothing here is reachable from another machine.
"""
import hashlib, json, os, re, subprocess, sys, threading, time
from html import escape
from pathlib import Path
from urllib.parse import urlparse, quote
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (HTMLResponse, RedirectResponse, PlainTextResponse, Response,
                                 JSONResponse)
from starlette.routing import Route, Mount
from starlette.concurrency import run_in_threadpool
from starlette.staticfiles import StaticFiles
import activity, agents, clock, orchestrator, settings, stopping, store
from agents import llm
from agents import bluesky, f916, github, publisher
from contracts import Mission, State, ResearchBrief, Article, Epic, epic_title, slugify, SITE_FOLDERS
from render import CSS, brief_html, epic_pill
from icons import icon, CSS as ICON_CSS
import flowchart, i18n
from flowchart import flow_steps
from i18n import _, _n

LOG = Path("data/loop.log")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")

LABELS = {
    State.QUEUED: "Queued", State.RESEARCHING: "Researching", State.BRIEF_READY: "Brief ready",
    State.AWAITING_COMMENTS: "Waiting for comments", State.WRITING: "Writing",
    State.ARTICLE_READY: "Article ready", State.DONE: "Published", State.PARKED: "Parked",
}
MESSAGES = {
    "approved": "Brief approved.",
    "published": "Article approved for publishing.",
    "rejected": "Sent back with notes.",
    "requeued": "Requeued.",
    "pushed": "Pushed through to review.",
    "added": "Mission added.",
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
    "site_url_saved": "Site address saved.",
    "site_url_cleared": "Site address cleared.",
    "bluesky_on": "Bluesky connected.",
    "bluesky_off": "Bluesky disconnected.",
    "every_saved": "Rest time saved. Applies when the loop next starts.",
    "auto_on": "Auto mode on.",
    "auto_off": "Auto mode off.",
    "animations_on": "Animations on.",
    "animations_off": "Animations off.",
    "theme_saved": "Appearance saved.",
    "language_saved": "Language saved.",
    "chat_color_saved": "Chat colour saved.",
    "ollama_saved": "Ollama address saved.",
    "worker_saved": "Model saved.",
    "worker_on": "Worker on.",
    "worker_off": "Worker off.",
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
}
ERRORS = {
    "topic": "A mission needs a topic.",
    "theme": "Pick System, Light or Dark.",
    "language": "Pick a language from the list.",
    "chat_color": "That colour wasn't a #rrggbb colour.",
    "slug": "Short name: lowercase letters, numbers and dashes only.",
    "exists": "A mission with that short name already exists.",
    "notes": "Add notes on what to fix.",
    "state": "That action no longer applies.",
    "running": "The loop is already running.",
    "every": "Enter whole minutes.",
    "epic": "An epic title is 1 to 4 words.",
    "epic_exists": "There is already an epic with that name.",
    "epic_target": "Pick a different approved epic.",
    "site_url": "Enter a full http(s):// address, or leave it empty.",
    "bluesky": "Bluesky needs both a handle and an app password.",
    "worker_model": "Pick a model from the list.",
    "ollama_host": "Enter a full http(s):// address, or leave it empty.",
    "server": "Enter a full http(s):// address, or leave it empty.",
    "worker_last": "At least one worker must stay on.",
    "ollama": "Can't reach Ollama. Start it and try again.",
    "handle": "A 1F916 handle is 2 to 32 letters, digits, _ or -.",
    "github_app": "That isn't a valid OAuth client ID.",
    "places": "Pick at least one place.",
}

# ---------- loop status ----------

def loop_running() -> bool:
    return orchestrator.loop_running()

def running_now() -> list[dict]:
    """Every agent working this moment, at most one per worker. The record only counts while the loop is alive."""
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
        return _("Article needs review")
    if m.state == State.PARKED:
        stopped = (_("Stopped after {n} writing attempts") if orchestrator.parked_while_writing(m)
                   else _("Stopped after {n} research attempts"))
        return stopped.format(n=m.max_attempts)
    if m.state == State.RESEARCHING:
        doing = _("Writing brief") if m.gathered or m.interrupted_brief else _("Finding sources")
        try_ = m.attempts if m.gathered or m.interrupted_brief else m.attempts + 1
        attempt = _("attempt {n} of {total}").format(n=try_, total=m.max_attempts)
        return f"{doing} · {attempt}" if m.attempts and try_ > 1 else doing
    if m.state == State.WRITING:
        attempt = _("attempt {n} of {total}").format(n=m.write_attempts + 1, total=m.max_attempts)
        return f'{_("Writing")} · {attempt}'
    if m.state == State.DONE and m.publish_requests:
        return _("To {places}").format(places=", ".join(_(orchestrator.channel_name(k)) for k in m.publish_requests))
    return ""

def in_progress(m: Mission) -> bool:
    """On the Missions page: everything not published yet, and published articles a human asked to publish again."""
    return not needs_you(m) and (m.state != State.DONE or bool(m.publish_requests))

def flow_rank(m: Mission) -> int:
    """Top to bottom on the Missions page, the flow chart read from its end: closest to published first."""
    order = {State.DONE: 0, State.ARTICLE_READY: 1, State.WRITING: 2, State.AWAITING_COMMENTS: 3,
             State.BRIEF_READY: 4, State.RESEARCHING: 5, State.QUEUED: 7, State.PARKED: 8}
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
           f"|{settings.auto_mode()}")
    return hashlib.sha1(raw.encode()).hexdigest()[:12]

# ---------- page shell ----------

UI_CSS = """
main { max-width: 56rem; }
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
.status { display:inline-flex; align-items:center; gap:.4rem;
          color:var(--muted); font-size:.9rem; font-weight:600; }
.status.on { color:var(--fg); }
.status .dot.on { animation:blink 1.4s ease-in-out infinite; }
.loop { display:flex; align-items:center; gap:.6rem; flex-wrap:wrap; padding:.8rem 1rem;
        border:1px solid var(--rule); border-radius:10px; margin-bottom:1.5rem; }
.loop form { display:flex; gap:.4rem; align-items:center; margin:0; }
.dot { width:.6rem; height:.6rem; border-radius:50%; background:var(--muted); display:inline-block; }
.dot.on { background:#2f9e44; } .dot.stopping { background:#e8a33d; }
.loop .grow { flex:1; }
.now { flex-basis:100%; color:var(--muted); font-size:.9rem; }
.now strong, .now a { color:var(--fg); }
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
.card.active { border-color:#2f9e44; box-shadow:inset 3px 0 0 #2f9e44; }
.card.active .sub { color:var(--fg); }
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
.actions .buttons { display:flex; gap:.5rem; flex-wrap:wrap; align-items:flex-start; }
.actions details { margin-top:.8rem; }
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
select { font:inherit; padding:.45rem .5rem; border:1px solid var(--rule); border-radius:8px;
         background:var(--bg); color:var(--fg); }
form.inline { display:flex; gap:.4rem; align-items:center; flex-wrap:wrap; margin:.4rem 0; }
form.inline input[type=text] { width:auto; flex:1; min-width:10rem; max-width:20rem; }

@media (prefers-reduced-motion: reduce) { .status .dot.on { animation:none; } }
.card[data-lab], .card.lab-host { position:relative; padding-bottom:4rem;
                  background-image:linear-gradient(var(--rule), var(--rule));
                  background-size:calc(100% - 2rem) 2px; background-position:1rem calc(100% - 8px);
                  background-repeat:no-repeat; }
.card canvas.lab-stage { bottom:0; height:160px; }   /* tall enough for a bubble above the tallest researcher */
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
.chat-panel header { display:flex; justify-content:space-between; align-items:center; }
.chat-panel header strong { font:700 1.2rem Georgia, serif; }
.chat-panel header button { padding:.2rem .6rem; font-size:1.1rem; line-height:1; }
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
.crew { position:relative; height:116px; margin:0 -1rem; border-top:1px solid var(--rule);
        background:linear-gradient(var(--quote), var(--quote)) bottom / 100% 9px no-repeat; }
.crew canvas.lab-stage { bottom:0; height:116px; }
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
const showElapsed = () => document.querySelectorAll("[data-since]").forEach(e => {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - Number(e.dataset.since)));
  e.textContent = s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
});
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
RESEARCHERS_SRC = "/assets/researchers.js"
RESEARCHERS = f'<script src="{RESEARCHERS_SRC}" defer></script>'
ASSETS = Path(__file__).parent / "assets"
FLOW_JS = '<script src="/assets/flow.js" defer></script>'   # keeps the pipeline strip live
ICON_MISSIONS = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 21V4"/><path d="M5 4c4-2.5 7 2.5 14 0v10c-7 2.5-10-2.5-14 0"/></svg>'
ICON_SITE = ('<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18"/>'
             '<path d="M12 3c3 3 3.6 6 3.6 9s-.6 6-3.6 9c-3-3-3.6-6-3.6-9s.6-6 3.6-9z"/></svg>')

ICON_SETTINGS = ('<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/>'
                 '<path d="M9.8 4.9 10.4 2.1 13.6 2.1 14.2 4.9 15.5 5.5 17.9 3.9 20.1 6.1 18.5 8.5 19.1 9.8 21.9 10.4 '
                 '21.9 13.6 19.1 14.2 18.5 15.5 20.1 17.9 17.9 20.1 15.5 18.5 14.2 19.1 13.6 21.9 10.4 21.9 9.8 19.1 '
                 '8.5 18.5 6.1 20.1 3.9 17.9 5.5 15.5 4.9 14.2 2.1 13.6 2.1 10.4 4.9 9.8 5.5 8.5 3.9 6.1 6.1 3.9 8.5 5.5z"/></svg>')

ICON_CHAT = ('<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v11H9l-5 4z"/></svg>')

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
    else:
        action, ico, text, dot = "stop", "pause", _("Running"), "on"
        title = _("Pause the loop")
    button = (f'<button class="play {dot}" title="{escape(title)}" aria-label="{escape(title)}"'
              f' onclick="this.form.back.value = location.pathname"{"" if action else " disabled"}>{icon(ico)}</button>')
    return (f'<form class="loop-ctl" method="post" action="/loop/{action or "stop"}">'
            f'<input type="hidden" name="back" value="/"><input type="hidden" name="every" value="{every:g}">'
            f'{button}'
            f'<span class="status {dot}"><span class="dot {dot}" aria-hidden="true"></span>{text}</span></form>')

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
  const busy = [...document.querySelectorAll("textarea, input[type=text]")].some(e => e.value.trim() && e.value !== e.defaultValue)
            ||["TEXTAREA", "INPUT"].includes(document.activeElement.tagName)
            || document.body.classList.contains("chat-open");
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
            f"<title>{escape(title)}</title><style>{CSS}{ICON_CSS}{flowchart.CSS}{UI_CSS}</style></head><body><main>"
            f'<nav class="top"><a class="brand" href="/">Mission Research Loop</a>'
            f'<span class="links">{auto_switch()}{loop_button()}<a href="/">{ICON_MISSIONS}{_("Missions")}</a>'
            f'<a href="/published">{ICON_SITE}{_("Published")}</a>'
            f'<a href="/settings">{ICON_SETTINGS}{_("Settings")}</a></span></nav>'
            f"{body}</main>{js_strings()}{i18n.lines_script()}{chat_widget()}{KEEP_SCROLL_JS}{script}</body></html>")
    return HTMLResponse(html)

JS_STRINGS = ["(default)", "Web search off (set BRAVE_API_KEY in .env).", "Looking…",
              "No answer. Is the UI still running?", "{n} parked here"]

def js_strings() -> str:
    """The few strings the page's scripts show, in the UI's language (window.I18N; English needs none)."""
    if i18n.current() == "en":
        return ""
    table = {s: _(s) for s in JS_STRINGS}
    return f"<script>window.I18N = {json.dumps(table, ensure_ascii=False).replace('</', '<\\/')};</script>"

def flash(request: Request) -> str:
    done, err = request.query_params.get("done"), request.query_params.get("error")
    if err in ERRORS:
        return f'<div class="flash err">{escape(_(ERRORS[err]))}</div>'
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

def flow_status(now: list[dict], running: bool) -> str:
    """The lines under the strip: who is working on what, and for how long. With both workers busy,
    one line each, named."""
    lines = [(f'<span class="who">{worker_name(e["worker"])}</span> ' if e["worker"] in settings.WORKERS
              and len(now) > 1 else "") + doing(e) for e in now]
    return "<br>".join(lines) or (_("Idle") if running else "")   # the loop panel already says it is off

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
    writing = sum(orchestrator.parked_while_writing(m) for m in parked)
    return {
        "running": running, "stopping": running and orchestrator.STOP_FLAG.exists(),
        "working": [{"worker": e["worker"], "step": strip_step(e["agent"]), "slug": e["slug"], "since": float(e["since"]),
                     "name": agent_name(e["agent"])} for e in now],
        "waiting": {"epic_review": len(store.all_epics("proposed")),
                    "brief_review": sum(m.state == State.BRIEF_READY and not m.approved for m in missions),
                    "article_review": sum(m.state == State.ARTICLE_READY and not m.reviewed for m in missions)},
        "parked": {"source_check": len(parked) - writing, "article_check": writing},
        "states": {m.slug: m.state.value for m in missions},
        "now": flow_status(now, running),
    }

def flow_strip(state: dict) -> str:
    """The pipeline strip, laid out as a snake: lane 0 runs left to right from the top left (the Theorist) to the
    first human review, a square end on the right; lane 1 comes back right to left one level lower to the next
    review on the left, and so on. Each review spans the two lanes it joins, so the snake turns through it. It is
    always drawn; with the loop off nothing on it is lit. assets/flow.js keeps it current without reloading the
    page (and stacks it on a narrow screen), and the pixel researchers stand on its tiles."""
    auto = settings.auto_mode()
    steps, lit = flow_steps(auto), {e["step"]: e["worker"] for e in state["working"]}
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
    elif running:
        state, dot = _("Loop running"), "on"
        controls = (f'<form method="post" action="/loop/wake"><input type="hidden" name="back" value="{back}">'
                    f'<button>{_("Check for work now")}</button></form>')
    else:
        state, dot = _("Loop off. Press play at the top to start."), ""
        controls = ""
    return (f'<div class="loop"><span class="dot {dot}"></span><span class="grow">{state}</span>{controls}'
            f'{flow_strip(flow_state())}</div>{FLOW_JS}')

def finder_panel() -> str:
    line = store.get_inspiration()
    state = _("The mission finder searches from this line. Empty turns the finder and Theorist off.")
    think = (f'<button formaction="/theorist" title="{escape(_("Run the Theorist on the next loop pass"))}">'
             f'{_("Run Theorist")}</button>') if line else ""
    example = escape(_("e.g. Who really decides what the Federal Reserve does?"), quote=True)
    return heading("brain", _("Inspiration")) + f"""<form method="post" action="/inspiration" class="actions">
  <label for="inspiration" style="margin-top:0"><span class="hint">{state}</span></label>
  <input type="text" id="inspiration" name="inspiration" maxlength="500" value="{escape(line, quote=True)}" placeholder="{example}">
  <p><button>{_("Save")}</button> {think}</p>{theorist_notes()}
</form>"""

def _when(at: str) -> str:
    return escape(at[:16].replace("T", " ")) + " UTC" if at else ""

def theorist_notes() -> str:
    """The Theorist's last turn and how the line got here, folded away under the inspiration box."""
    last, history = store.last_theory(), store.inspiration_history(6)
    if not last and not history:
        return ""
    parts = []
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
        if made:
            parts.append(f"<ul>{''.join(made)}</ul>")
        if last.get("notes"):
            parts.append(f'<p class="hint">{_("Dropped by check:")} ' + escape("; ".join(last["notes"])) + "</p>")
    if history:
        rows = "".join(f'<li><span class="hint">{_when(h.get("at", ""))}, '
                       f'{_("you") if h.get("by") == "human" else _("Theorist")}:</span> '
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

# ---------- settings: the two workers and the LLM connection ----------

WORKER_BLURBS = {
    "assistant": "Small model. Searches and reads.",
    "pi": "Big model. Thinks and writes.",
}
ROLE_STEPS = {                             # where each worker's model is used; the rest of the pipeline is code
    "assistant": ["<strong>Source finder</strong>: searches and reads pages",
                  "<strong>Mission finder</strong>: searches",
                  "<strong>Contact finder</strong>: finds people (outreach only)"],
    "pi": ["<strong>Theorist</strong>: refines the inspiration line",
           "<strong>Brief writer</strong>: writes the brief",
           "<strong>Article writer</strong>: writes the article",
           "<strong>Mission finder</strong>: picks new missions"],
}

def gb(size: int) -> str:
    return f"{size / 1e9:.1f} GB" if size >= 1e8 else f"{size / 1e6:.0f} MB"

def model_options(current: str, conn: dict) -> str:
    names = [m["name"] for m in conn["models"]]
    opts = [f'<option value="{escape(m["name"], quote=True)}"{" selected" if m["name"] == current else ""}>'
            f'{escape(m["name"])} · {gb(m["size"])}{" · " + _("loaded") if m["name"] in conn["loaded"] else ""}</option>'
            for m in conn["models"]]
    if not current:                        # nothing chosen on this machine yet: the code has no default
        opts.insert(0, f'<option value="" selected disabled>{_("Choose a model")}</option>')
    elif current not in names:             # saved before, or Ollama is closed: still show what it is set to
        missing = _("not pulled") if conn["ok"] else _("Ollama not reachable")
        opts.insert(0, f'<option value="{escape(current, quote=True)}" selected>{escape(current)} ({missing})</option>')
    return "".join(opts)

def worker_card(worker: str, conn: dict, now: list[dict]) -> str:
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
        busy = next((e for e in now if e["worker"] == worker), None)
        note = f'<p class="note">{_("Now:")} {doing(busy)}</p>' if busy else ""
    checked = " checked" if on else ""
    crew = f'<div class="crew" data-crew="{worker}" aria-hidden="true"></div>' if settings.animations() else ""
    return f"""<form method="post" action="/settings/worker" class="worker {"on" if on else "off"}">
  <input type="hidden" name="worker" value="{worker}">
  <div class="head"><h3>{name}</h3>
    <label class="switch"><input type="checkbox" role="switch" name="on" value="on"{checked} onchange="this.form.requestSubmit()" aria-label="{escape(_("{name} on").format(name=name), quote=True)}"></label></div>
  <p class="tagline hint">{_(WORKER_BLURBS[worker])}</p>
  <label for="{worker}_model">{_("Model")}</label>
  <select id="{worker}_model" name="model" onchange="this.form.requestSubmit()">{model_options(model, conn)}</select>
  <label>{_("Steps")}</label>
  <ul>{steps}</ul>
  {note}<div class="grow"></div>
  <noscript><p><button>{_("Save")}</button></p></noscript>
  {crew}
</form>"""

def workers_section(conn: dict) -> str:
    now = running_now()
    if settings.missing_models():          # the code has no models of its own: this machine picks them
        lead = f'<strong>{_("Choose a model for each worker; the loop won\'t start until you do.")}</strong>'
    elif settings.parallel():
        lead = ""
    else:
        w = next(w for w in settings.WORKERS if settings.worker_on(w))
        lead = _("Only the {worker} is on. It runs every step, one at a time.").format(worker=worker_name(w))
    cards = "".join(worker_card(w, conn, now) for w in settings.WORKERS)
    return (f'{heading("people", _("Workers"))}{f"<p>{lead}</p>" if lead else ""}'
            f'<div class="workers">{cards}</div>')

def ollama_form() -> str:
    saved, env = settings.saved().get("ollama_host", ""), settings._env("OLLAMA_HOST")
    return f"""<form method="post" action="/settings/ollama" class="inline">
  <label for="ollama_host" class="sr">{_("Ollama address")}</label>
  <input type="text" id="ollama_host" name="ollama_host" maxlength="300" value="{escape(saved, quote=True)}" placeholder="{escape(env or settings.OLLAMA_DEFAULT, quote=True)}">
  <button>{_("Save")}</button><span class="hint">{_("Empty uses {address}").format(address=escape(env or settings.OLLAMA_DEFAULT))}</span>
</form>"""

def connection_section(conn: dict) -> str:
    host = escape(conn["host"])
    if not conn["ok"]:
        return (f'{heading("plug", _("LLM connection"))}<p>'
                + _("Can't reach Ollama at {host} ({error}).").format(host=f"<code>{host}</code>",
                                                                       error=escape(conn["error"]))
                + "</p>" + ollama_form())
    users: dict[str, list[str]] = {}
    for w in settings.WORKERS:
        if settings.worker_on(w):
            users.setdefault(settings.worker_model(w), []).append(worker_name(w))
    rows = "".join(
        f'<tr><td><code>{escape(m["name"])}</code></td><td class="size">{gb(m["size"])}</td>'
        f'<td class="size">{gb(conn["loaded"][m["name"]]) if m["name"] in conn["loaded"] else ""}</td>'
        f'<td>{", ".join(users.get(m["name"], []))}</td></tr>' for m in conn["models"])
    count = len(conn["models"])
    return (f'{heading("plug", _("LLM connection"))}<p>{_("Ollama at {host}").format(host=f"<code>{host}</code>")} · '
            f'{_("connected")} · {_n("{n} model", "{n} models", count)}</p>{ollama_form()}'
            f'<table class="events models"><tr><td class="when">{_("Model")}</td><td class="when">{_("Size")}</td>'
            f'<td class="when">{_("In memory")}</td><td class="when">{_("Used by")}</td></tr>{rows}</table>')

async def save_ollama(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    try:
        settings.set_ollama_host(str((await request.form()).get("ollama_host") or "")[:300])
    except ValueError:
        return go("/settings", error="ollama_host")
    return go("/settings", done="ollama_saved")

async def save_worker(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    worker, on = str(form.get("worker") or ""), form.get("on") == "on"   # an unticked switch sends nothing
    model = str(form.get("model") or "").strip()[:200]
    if worker not in settings.WORKERS:
        return go("/settings", error="worker_model")
    was_on = settings.worker_on(worker)
    if model != settings.worker_model(worker):
        conn = await run_in_threadpool(llm.connection)
        if not conn["ok"]:
            return go("/settings", error="ollama")
        if model not in {m["name"] for m in conn["models"]}:
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
    return heading("broadcast", _("Publishing"), anchor="publishing") + f"""
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

def settings_body(request: Request, error: str = "") -> str:
    err = f'<div class="flash err">{escape(error)}</div>' if error else flash(request)
    checked = " checked" if settings.animations() else ""
    conn = llm.connection()
    sprites = RESEARCHERS if settings.animations() else ""
    return f"""{err}<h1>{_("Settings")}</h1>
{workers_section(conn)}
{connection_section(conn)}
{heading("clock", _("Loop"))}
<form method="post" action="/settings/loop-every" class="inline">
  <label for="every" style="margin:0">{_("Rest when idle")}</label>
  <input type="number" id="every" name="every" value="{settings.loop_every():g}" min="1" step="1" style="width:5rem">
  <span class="hint">{_("minutes (applies on next start)")}</span>
  <button>{_("Save")}</button>
</form>
{heading("monitor", _("Display"))}
{display_forms()}
<form method="post" action="/settings/animations" class="actions">
  <label class="switch"><input type="checkbox" role="switch" name="animations" value="on"{checked} onchange="this.form.requestSubmit()">
  {_("Animations")} <span class="hint">{_("pixel researchers on active missions")}</span></label>
  <noscript><p><button>{_("Save")}</button></p></noscript>
</form>
{publishing_section()}
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
    """The top bar's Auto switch. Turning it on approves what already waits (briefs, proposed epics) right away;
    the loop approves the rest as they come. Publishing always stays with the human."""
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    on = form.get("auto") == "on"          # an unticked box sends nothing
    settings.set_auto_mode(on)
    if on:
        orchestrator.auto_approve(clock.now())
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

def mission_pill(m: Mission, epics: dict[str, Epic]) -> str:
    e = epics.get(m.epic)
    return epic_pill(e.title, proposed=e.status == "proposed") if e else ""

def epic_datalist() -> str:
    """Suggestions for every epic field: pick an approved epic, or type a new name."""
    options = "".join(f'<option value="{escape(e.title, quote=True)}">' for e in store.all_epics("active"))
    return f'<datalist id="epic-names">{options}</datalist>'

# ---------- anything waiting on a human: one look for every decision ----------

ASK_ACTION = {"review": "Review brief", "publish": "Review article", "parked": "See why"}

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
               f'<div class="sub">{mission_pill(m, epics)}{escape(m.slug)} · {escape(next_step(m))}</div>',
               f'<a class="btn primary" href="{href}">{_(ASK_ACTION[color])}</a>', href)

def epic_proposals(missions: list[Mission]) -> list[str]:
    """Proposed epics (the finder's or the Theorist's), each waiting for the human to approve, rename, redirect
    or reject it."""
    active = store.all_epics("active")
    theorist = {t.lower() for t in store.last_theory().get("epics", [])}
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
        who = _("Proposed by Theorist") if e.title.lower() in theorist else _("Proposed by mission finder")
        out.append(ask(
            "propose", e.title, _("New epic"),
            f'<div class="sub">{who} · {_n("{n} mission", "{n} missions", len(ms))}</div><ul>{items}</ul>',
            f'<form method="post" action="/epics/{slug}/approve">'
            f'<input type="text" name="title" value="{escape(e.title, quote=True)}" maxlength="48" aria-label="{_("Epic name")}">'
            f'<button class="primary">{_("Approve epic")}</button></form>{move}'
            f'<form method="post" action="/epics/{slug}/reject"><button>{_("Reject")}</button></form>'))
    return out

def epic_list(missions: list[Mission]) -> str:
    active = store.all_epics("active")
    if not active:
        return ""
    rows = "".join(
        f'<form method="post" action="/epics/{quote(e.slug)}/rename" class="inline">'
        f'<input type="text" name="title" value="{escape(e.title, quote=True)}" maxlength="48" aria-label="{_("Epic name")}">'
        f'<span class="hint">{_n("{n} mission", "{n} missions", sum(m.epic == e.slug for m in missions))}</span>'
        f'<button>{_("Rename")}</button></form>'
        for e in active)
    return heading("layers", _("Epics"), len(active)) + f'<div class="epics">{rows}</div>'

# In progress, grouped by where each mission is in the workflow, closest to published first (flow_rank).
STAGES = [("publishing", "send", "Publishing"), ("writing", "write", "Writing"), ("research", "search", "Research"),
          ("queued", "clock", "Queued")]

def stage_of(m: Mission) -> str:
    if m.state == State.QUEUED:
        return "queued"
    if m.state == State.RESEARCHING:
        return "research"
    if m.state in (State.ARTICLE_READY, State.DONE):  # a reviewed article, or one a human asked to publish again
        return "publishing"
    return "writing"                       # an approved brief, the comment wait, writing

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
        stage = ' data-lab="busy"' + (f' data-worker="{live["worker"]}"' if live["worker"] in settings.WORKERS else "")
    return (f'<a class="{classes}" href="/m/{quote(m.slug)}"{stage}><div class="row"><span class="topic">{escape(m.topic)}</span>'
            f'<span class="badge {color}">{escape(label(m))}</span></div>'
            f'<div class="sub">{mission_pill(m, epics)}{escape(m.slug)}<span class="doing" data-idle="{escape(idle, quote=True)}">'
            f'{" · " + escape(sub) if sub else ""}</span></div></a>')

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

async def home(request: Request) -> HTMLResponse:
    """The flow chart read from its end: what needs you, then what is in progress closest to published first,
    grouped by stage; then the inputs (inspiration, epics, a new mission). Published articles live on /published:
    the top bar's Published button, and a card at the very bottom for anyone who scrolls looking for it. What waits
    to be published is one card under In progress that leads there; the queue itself is on that page."""
    missions = store.all_missions()
    flow = lambda ms: sorted(ms, key=flow_rank)   # stable: within a step, oldest first
    now = running_now()
    epics = {e.slug: e for e in store.all_epics()}
    lab = settings.animations()
    body = [flash(request), loop_panel("/")]

    # Needs you: every decision in one place and one look, closest to published first; new epics before parked
    asks = [(flow_rank(m), mission_ask(m, epics)) for m in flow(m for m in missions if needs_you(m))]
    asks += [(6, a) for a in epic_proposals(missions)]
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

    body.append(finder_panel())
    body.append(epic_list(missions))
    body.append(epic_datalist() + heading("pencil", _("New mission")) + f"""<form method="post" action="/missions" class="actions">
  <label for="topic" style="margin-top:0">{_("Topic")}</label>
  <input type="text" id="topic" name="topic" required placeholder="{escape(_("e.g. Did the new transit levy pass, and who paid for the campaign?"), quote=True)}">
  <label for="objective">{_("Objective")} <span class="hint">{_("optional")}</span></label>
  <textarea id="objective" name="objective" rows="4"></textarea>
  <label for="slug">{_("Short name")} <span class="hint">{_("optional, made from the topic")}</span></label>
  <input type="text" id="slug" name="slug" placeholder="transit-levy">
  <label for="epic">{_("Epic")} <span class="hint">{_("optional, pick or type 1–4 words")}</span></label>
  <input type="text" id="epic" name="epic" list="epic-names" maxlength="48" placeholder="{escape(_("e.g. Fed Policy Capture"), quote=True)}">
  <p><button class="primary">{_("Add mission")}</button></p>
</form>""")
    tail = log_tail()
    if tail:
        body.append(f'<details><summary>{icon("terminal")}{_("Loop log")}</summary><pre class="notes">'
                    + escape("\n".join(tail)) + "</pre></details>")
    done = sum(m.state == State.DONE for m in missions)   # the very last thing, for anyone looking for it
    body.append(f'<a class="card site-link" href="/published"><div class="row"><span class="topic">{ICON_SITE}{_("Published")}</span>'
                f'<span class="badge">{_n("{n} article", "{n} articles", done)}</span></div>'
                f'<div class="sub">{_("Where each article went, and republish")}</div></a>')
    if lab:
        body.append(RESEARCHERS)
    return page("Mission Loop", "\n".join(body), poll=True)

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
        return ask("publish", _("Review article"), "Article ready", f'<p class="hint">{escape(where)}</p>',
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

async def mission(request: Request) -> Response:
    slug = request.path_params["slug"]
    m = store.get_mission(slug)
    if not m:
        return page(_("Not found"), f'<h1>{_("No such mission")}</h1><p><a href="/">{_("Back to missions")}</a></p>')
    epics = {e.slug: e for e in store.all_epics()}
    body = [flash(request),
            f'<p><span class="badge {tone(m)}">{escape(label(m))}</span> {mission_pill(m, epics)} '
            f'<span class="muted">{escape(m.slug)}</span></p>',
            f"<h1>{escape(m.topic)}</h1>"]
    if m.objective:
        body.append(f'<p class="muted">{escape(m.objective)}</p>')
    step = working_on(m, running_now()) or next_step(m)
    if step:
        body.append(f"<p><strong>{escape(step)}</strong></p>")
    ask = escape(_("Where is the mission {slug} at, and what is taking so long?").format(slug=slug), quote=True)
    body.append(f'<p><button type="button" class="btn" data-ask="{ask}">{_("Ask chat about this mission")}</button></p>')
    body.append(actions_for(m))
    current = epics.get(m.epic)
    hint = " " + _("(proposed)") if current and current.status == "proposed" else ""
    body.append(f'{epic_datalist()}<form method="post" action="/m/{quote(slug)}/epic" class="inline">'
                f'<label for="epic" style="margin:0">{_("Epic")}<span class="hint">{hint}</span></label>'
                f'<input type="text" id="epic" name="epic" list="epic-names" maxlength="48" '
                f'value="{escape(current.title if current else "", quote=True)}" placeholder="{_("none")}">'
                f'<button>{_("Save")}</button></form>')

    if m.validator_notes:
        title = "Why it stopped" if m.state == State.PARKED else \
                "Your feedback" if m.validator_notes.startswith("Human reviewer feedback") else \
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
        body.append(f'<details><summary>{icon("cross")}{_("Last failed article")}</summary>'
                    f'<pre class="notes">{escape(rejected.read_text())}</pre></details>')

    events = store.events(slug)
    if events:
        rows = "".join(f'<tr><td class="when">{escape(e["at"][:16].replace("T", " "))}</td>'
                       f'<td>{escape(_(LABELS.get(State(e["to_state"]), e["to_state"])))}</td>'
                       f'<td>{escape(e["note"] or "")}</td></tr>' for e in reversed(events))
        body.append(f'{heading("history", _("History"), cls="sec")}<table class="events">{rows}</table>')
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
# A bubble on every page opens a panel over the left third. The conversation is kept in this browser tab
# (sessionStorage) so the page's own refreshes don't wipe it; the assistant itself remembers nothing.
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
    else if (data.web === false) hint.textContent += " " + t("Web search off (set BRAVE_API_KEY in .env).");
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
  async function ask(question) {
    question = question.trim();
    if (!question || box.disabled) return;
    setOpen(true); suggest.hidden = true;
    const model = pickModel.value;
    show("user", question); box.value = ""; box.disabled = true;
    const wait = show("assistant", t("Looking…"), "", "wait");
    let answer;
    try {
      const r = await fetch("/chat/ask", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({question, model, history: history.slice(-4).map(({role, content}) => ({role, content}))})});
      answer = (await r.json()).answer;
    } catch (e) { answer = t("No answer. Is the UI still running?"); }
    wait.remove(); show("assistant", answer, model);
    history.push({role: "user", content: question}, {role: "assistant", content: answer, model});
    history = history.slice(-20); store("chat-history", history);
    box.disabled = false; box.focus();
  }
  bubbleBtn.addEventListener("click", () => setOpen(panel.hidden));
  document.getElementById("chat-close").addEventListener("click", () => setOpen(false));
  document.getElementById("chat-clear").addEventListener("click", () => { history = []; store("chat-history", history); redraw(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !panel.hidden) setOpen(false); });
  pickModel.addEventListener("change", () => { try { localStorage.setItem("chat-model", pickModel.value); } catch (e) {} guide(); });
  form.addEventListener("submit", e => { e.preventDefault(); ask(box.value); });
  box.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(box.value); } });
  suggest.querySelectorAll("button").forEach(b => b.addEventListener("click", () => ask(b.textContent)));
  // one listener for every "Ask the assistant" button, so ones swapped in by a page refresh work too
  document.addEventListener("click", e => { const b = e.target.closest("[data-ask]"); if (b) ask(b.dataset.ask); });
  redraw();
  if (store("chat-open")) setOpen(true);
})();
</script>"""

def chat_widget() -> str:
    chips = "".join(f'<button type="button">{escape(_(q))}</button>' for q in SUGGESTIONS)
    chat = escape(_("Researcher Chat"), quote=True)
    guide = (f'<div class="chat-guide" aria-hidden="true" data-sprites="{RESEARCHERS_SRC}"></div>'
             if settings.animations() else "")
    return f"""<button type="button" id="chat-bubble" class="chat-bubble" aria-label="{chat}" aria-expanded="false"
 aria-controls="chat-panel">{ICON_CHAT}</button>
<aside id="chat-panel" class="chat-panel" aria-label="{chat}" hidden>{guide}
<header><strong>{chat}</strong><span><button type="button" id="chat-clear" title="{_("Start over")}">{_("Clear")}</button>
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

async def chat_ask(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    try:
        data = await request.json()
        question, history = str(data.get("question") or ""), data.get("history") or []
        model = str(data.get("model") or "")[:100] or None
        history = history if isinstance(history, list) else []
    except (ValueError, AttributeError):
        return JSONResponse({"answer": _("That question didn't arrive in one piece. Try again.")}, status_code=400)
    try:
        answer = await run_in_threadpool(agents.chat, question, [h for h in history if isinstance(h, dict)], model)
    except Exception as e:                 # Ollama closed, model missing: say so instead of a blank page
        answer = _("I couldn't answer just now ({error}). Is Ollama running?").format(error=f"{type(e).__name__}: {str(e)[:200]}")
    return JSONResponse({"answer": answer})

async def api_signature(request: Request) -> PlainTextResponse:
    return PlainTextResponse(signature())

async def api_flow(request: Request) -> JSONResponse:
    return JSONResponse(flow_state())

# ---------- actions (POST, then redirect back) ----------

def same_origin(request: Request) -> bool:
    """Refuse form posts from other websites (a page elsewhere can't approve or publish for you)."""
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if value:
            return urlparse(value).netloc == request.headers.get("host")
    return True

def go(path: str, done: str = "", error: str = "") -> RedirectResponse:
    q = f"?error={error}" if error else f"?done={done}" if done else ""
    return RedirectResponse(path + q, status_code=303)

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
        try:
            orchestrator.requeue(m, now)
        except ValueError:
            return go(here, error="state")
        done = "requeued"
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
    store.save_mission(Mission(slug=slug, topic=topic, objective=objective, epic=epic))
    store.log_event(slug, clock.now(), "", State.QUEUED.value, "added in the UI")
    if loop_running():
        wake()
    return go(f"/m/{quote(slug)}", done="added")

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
            orchestrator.merge_epic(slug, str(form.get("into") or ""), now)
            done = "epic_moved"
        elif action == "reject":
            orchestrator.reject_epic(slug, now)
            done = "epic_rejected"
        elif action == "rename":
            orchestrator.rename_epic(slug, title, now)
            done = "epic_renamed"
        else:
            return PlainTextResponse("Unknown action.", status_code=404)
    except ValueError as e:
        return go("/", error="epic_exists" if "already" in str(e) else
                  "epic_target" if action == "merge" else "state")
    return go("/", done=done)

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
    Route("/chat/models", chat_models),
    Route("/chat/ask", chat_ask, methods=["POST"]),
    Route("/settings/site-url", save_site_url, methods=["POST"]),
    Route("/settings/animations", save_animations, methods=["POST"]),
    Route("/settings/theme", save_theme, methods=["POST"]),
    Route("/settings/language", save_language, methods=["POST"]),
    Route("/settings/chat-color", save_chat_color, methods=["POST"]),
    Route("/auto", save_auto, methods=["POST"]),
    Route("/settings/loop-every", save_loop_every, methods=["POST"]),
    Route("/settings/worker", save_worker, methods=["POST"]),
    Route("/settings/ollama", save_ollama, methods=["POST"]),
    Route("/settings/join-1f916", join_1f916, methods=["POST"]),
    Route("/settings/bluesky", save_bluesky, methods=["POST"]),
    Route("/settings/github", github_action, methods=["POST"]),
    Route("/settings/github/poll", github_poll, methods=["POST"]),
    Route("/settings/custom-channel", add_custom_channel, methods=["POST"]),
    Route("/settings/custom-channel/remove", remove_custom_channel, methods=["POST"]),
    Route("/published", published_page),
    Route("/m/{slug}", mission),
    Route("/m/{slug}/preview", preview),
    Route("/m/{slug}/{action}", act, methods=["POST"]),
    Route("/epics/{slug}/{action}", epic_action, methods=["POST"]),
    Route("/loop/{action}", loop_action, methods=["POST"]),
    Route("/api/signature", api_signature),
    Route("/api/flow", api_flow),
    Mount("/assets", StaticFiles(directory=ASSETS)),
    Mount("/site", StaticFiles(directory="site", html=True, check_dir=False)),
])
