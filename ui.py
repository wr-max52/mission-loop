"""Local web UI. Run it with `python run.py ui`, then open http://127.0.0.1:8770.

It is a thin layer over the same store and orchestrator functions the CLI uses: it reads missions,
shows briefs and articles, and records human decisions (approve, reject, publish, requeue, push). It never
runs agents itself. Work happens in the loop, which the UI can start, stop and wake. Bound to
127.0.0.1 only; nothing here is reachable from another machine.
"""
import hashlib, os, re, subprocess, sys, time
from html import escape
from pathlib import Path
from urllib.parse import urlparse, quote
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (HTMLResponse, RedirectResponse, FileResponse, PlainTextResponse, Response,
                                 JSONResponse)
from starlette.routing import Route, Mount
from starlette.concurrency import run_in_threadpool
from starlette.staticfiles import StaticFiles
import activity, agents, clock, orchestrator, settings, stopping, store
from agents import llm
from agents import bluesky, f916, publisher
from contracts import Mission, State, ResearchBrief, Article, Epic, epic_title, slugify
from render import CSS, brief_html, epic_pill, THEME_INIT, THEME_TOGGLE

LOG = Path("data/loop.log")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")

LABELS = {
    State.QUEUED: "Queued", State.RESEARCHING: "Researching", State.BRIEF_READY: "Brief ready",
    State.AWAITING_COMMENTS: "Waiting for comments", State.WRITING: "Writing",
    State.ARTICLE_READY: "Article ready", State.DONE: "Published", State.PARKED: "Parked",
}
MESSAGES = {
    "approved": "Approved. The loop writes the article next.",
    "published": "Marked for publishing. The loop publishes it next, before any research or writing.",
    "rejected": "Sent back with your notes.",
    "requeued": "Requeued. It starts again after the missions further along.",
    "pushed": "Pushed through. It is marked as failing its check and is waiting for your review.",
    "added": "Mission added. The loop researches it after the missions further along.",
    "inspired": "Inspiration saved. The mission finder works from it each time the loop has caught up.",
    "uninspired": "Inspiration cleared. The mission finder is off.",
    "started": "Loop started.",
    "stopping": "Stop requested. The agent at work writes out what it has (2 minutes at most), then the loop exits.",
    "woken": "The loop checks for work now.",
    "epic_set": "Epic saved.",
    "epic_approved": "New epic approved.",
    "epic_moved": "Missions moved to that epic.",
    "epic_rejected": "Proposed epic rejected. Its missions went back to the epics the finder picked for them.",
    "epic_renamed": "Epic renamed. Published pages show the new name.",
    "site_url_saved": "Site address saved. The feeds use full links now.",
    "site_url_cleared": "Site address cleared. The feeds use links relative to the feed file.",
    "bluesky_on": "Bluesky login checked and saved. Published articles are announced there from now on.",
    "bluesky_off": "Bluesky disconnected. Its saved login was deleted from this machine.",
    "animations_on": "Animations on. Researchers will drop by in-progress missions.",
    "animations_off": "Animations off. The Missions page stays still.",
    "ollama_saved": "LLM connection saved. Workers use the Ollama at that address from their next task.",
    "worker_saved": "Worker saved. It takes its next task with that model.",
    "worker_on": "Worker on. With both on, they work at the same time whenever each has a task.",
    "worker_off": "Worker off. The other worker does its steps, one task at a time.",
    "joined": "Joined 1F916. Published articles post there from now on, one a day, newest first.",
}
ERRORS = {
    "topic": "A mission needs a topic.",
    "slug": "Short name: lowercase letters, numbers and dashes only.",
    "exists": "A mission with that short name already exists.",
    "notes": "Say what needs fixing so the agent can act on it.",
    "state": "That mission isn't waiting for that action any more.",
    "running": "The loop is already running.",
    "epic": "An epic title is 1 to 4 words.",
    "epic_exists": "There is already an epic with that name.",
    "epic_target": "Pick a different, approved epic to move them to.",
    "site_url": "The site address must be a full http:// or https:// address, or empty.",
    "bluesky": "Bluesky needs both a handle and an app password.",
    "worker_model": "Pick a model the LLM connection has.",
    "ollama_host": "The Ollama address must be a full http:// or https:// address, or empty for the default.",
    "server": "A server address must be a full http:// or https:// address, or empty for the default.",
    "worker_last": "One worker has to stay on: it does the other's steps while that one is off.",
    "ollama": "Ollama isn't reachable, so the model list can't be checked. Start the Ollama app and try again.",
    "handle": "A 1F916 handle is 2 to 32 letters, digits, _ or -.",
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
    if m.state == State.BRIEF_READY and m.approved:
        return "Approved, writing next"
    if m.state == State.ARTICLE_READY and m.reviewed:
        return "Publishing next"
    return LABELS[m.state]

def next_step(m: Mission) -> str:
    if m.state == State.BRIEF_READY and not m.approved:
        return "Read the brief, then approve it or send it back."
    if m.state == State.ARTICLE_READY and not m.reviewed:
        return "Read the article, then publish it or send it back."
    if m.state == State.PARKED:
        stage = "writing" if orchestrator.parked_while_writing(m) else "research"
        then = "requeue it or push it through" if orchestrator.parked_draft(m) else "requeue it"
        return f"Gave up after {m.max_attempts} {stage} attempts. Read why, then {then}."
    if m.state == State.RESEARCHING:
        return f"Research attempt {m.attempts + 1} of {m.max_attempts}." if m.attempts else "Researching."
    if m.state == State.WRITING:
        return f"Writing attempt {m.write_attempts + 1} of {m.max_attempts}."
    return ""

def _brief(slug: str) -> ResearchBrief | None:
    data = store.load_artifact(slug, "brief")
    return ResearchBrief(**data) if data else None

def _article(slug: str) -> Article | None:
    data = store.load_artifact(slug, "article")
    return Article(**data) if data else None

def working_on(m: Mission, now: list[dict]) -> str:
    """A line saying which agent is on this mission right now, or "" if none is."""
    e = next((e for e in now or [] if e["slug"] == m.slug), None)
    return f'{activity.AGENTS[e["agent"]]} is working on this now.' if e else ""

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
    raw = "".join(r["data"] for r in rows) + f"|{last}|{loop_running()}|{orchestrator.STOP_FLAG.exists()}|{working}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]

# ---------- page shell ----------

UI_CSS = """
main { max-width: 56rem; }
body { font-family: -apple-system, system-ui, sans-serif; font-size: 16px; }
nav.top { display:flex; justify-content:space-between; align-items:center; gap:1rem; flex-wrap:wrap;
          border-bottom:1px solid var(--rule); padding-bottom:1rem; margin-bottom:1.5rem; }
nav.top a.brand { font-weight:700; font-size:1.1rem; color:var(--fg); text-decoration:none; }
nav.top .links { display:flex; align-items:center; gap:.5rem; flex-wrap:wrap; }
nav.top .links a { display:inline-flex; align-items:center; gap:.4rem; padding:.45rem .8rem; border-radius:8px;
                   border:1px solid var(--muted); background:var(--quote); color:var(--fg);
                   font-size:.9rem; font-weight:600; text-decoration:none; }
nav.top .links a:hover { border-color:var(--fg); }
nav.top .links svg { width:1rem; height:1rem; fill:none; stroke:currentColor; stroke-width:2;
                     stroke-linecap:round; stroke-linejoin:round; }
.status { display:inline-flex; align-items:center; gap:.4rem; margin-right:.5rem;
          color:var(--muted); font-size:.9rem; font-weight:600; }
.status.on { color:var(--fg); }
.status .dot.on { animation:blink 1.4s ease-in-out infinite; }
.loop { display:flex; align-items:center; gap:.6rem; flex-wrap:wrap; padding:.8rem 1rem;
        border:1px solid var(--rule); border-radius:10px; margin-bottom:1.5rem; }
.loop form { display:flex; gap:.4rem; align-items:center; margin:0; }
.dot { width:.6rem; height:.6rem; border-radius:50%; background:var(--muted); display:inline-block; }
.dot.on { background:#2f9e44; } .dot.stopping { background:#e8a33d; }
.loop .grow { flex:1; }
.agents { flex-basis:100%; display:flex; flex-wrap:wrap; align-items:center; gap:.3rem; }
.agent { font-size:.78rem; font-weight:600; padding:.22rem .55rem; border-radius:999px;
         background:var(--quote); color:var(--muted); }
.agent.on { background:#237a35; color:#fff; animation:working 2s ease-out infinite; }
.agents .arrow { color:var(--muted); }
.now { flex-basis:100%; color:var(--muted); font-size:.9rem; }
.now strong, .now a { color:var(--fg); }
@keyframes working { from { box-shadow:0 0 0 0 rgba(47,158,68,.6); } to { box-shadow:0 0 0 .45rem rgba(47,158,68,0); } }
@keyframes blink { 50% { opacity:.2; } }
@media (prefers-reduced-motion: reduce) { .agent.on, .status .dot.on { animation:none; } }
h1 { font-family: Georgia, serif; }
h2.group { font-size:.8rem; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); margin:2rem 0 .5rem; }
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
button.primary { background:#8a2b2b; border-color:#8a2b2b; color:#fff; }
button:hover, .btn:hover { border-color:var(--muted); }
input, textarea { font:inherit; padding:.5rem .6rem; border:1px solid var(--rule); border-radius:8px;
                  background:var(--bg); color:var(--fg); width:100%; }
input[type=number] { width:4.5rem; }
label { display:block; font-size:.85rem; font-weight:600; margin:.8rem 0 .3rem; }
.hint { color:var(--muted); font-size:.85rem; font-weight:400; }
.flash { padding:.7rem 1rem; border-radius:8px; background:var(--quote); margin-bottom:1rem; }
.flash.err { border-left:3px solid var(--accent); }
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
.epic-review { border-style:dashed; border-color:var(--muted); }
.epic-review ul { margin:.4rem 0 .8rem; padding-left:1.2rem; }
.epic-review .buttons form { display:flex; gap:.4rem; align-items:center; margin:0; }
.epic-review input[type=text], .epic-review select { width:auto; min-width:10rem; }
select { font:inherit; padding:.45rem .5rem; border:1px solid var(--rule); border-radius:8px;
         background:var(--bg); color:var(--fg); }
form.inline { display:flex; gap:.4rem; align-items:center; flex-wrap:wrap; margin:.4rem 0; }
form.inline input[type=text] { width:auto; flex:1; min-width:10rem; max-width:20rem; }
.card[data-lab] { position:relative; padding-bottom:4rem;
                  background-image:linear-gradient(var(--rule), var(--rule));
                  background-size:calc(100% - 2rem) 2px; background-position:1rem calc(100% - 8px);
                  background-repeat:no-repeat; }
.agents[data-lab] { position:relative; padding-top:3.7rem; }
canvas.lab-stage { position:absolute; left:0; pointer-events:none; image-rendering:pixelated; width:100%; }
.card canvas.lab-stage { bottom:0; height:160px; }   /* tall enough for a bubble above the tallest researcher */
.agents canvas.lab-stage { top:0; }
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
               border-radius:50%; border:none; background:var(--accent); color:var(--bg); display:grid; place-items:center;
               box-shadow:0 4px 14px rgba(0,0,0,.25); }
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
.chat-log { flex:1; overflow-y:auto; display:flex; flex-direction:column; gap:.6rem; padding:.2rem 0; }
.chat-log .msg { max-width:88%; padding:.6rem .9rem; border-radius:12px; white-space:pre-wrap; line-height:1.5; }
.chat-log .user { align-self:flex-end; background:var(--accent); color:var(--bg); }
.chat-log .assistant { align-self:flex-start; background:var(--quote); }
.chat-log .assistant.wait { color:var(--muted); font-style:italic; }
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
"""

ELAPSED_JS = """<script>
const showElapsed = () => document.querySelectorAll("[data-since]").forEach(e => {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - Number(e.dataset.since)));
  e.textContent = s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
});
setInterval(showElapsed, 1000);
</script>"""

# Little pixel researchers who wander in-progress cards and the working agent's pill (assets/researchers.js).
RESEARCHERS = '<script src="/assets/researchers.js" defer></script>'
ASSETS = Path(__file__).parent / "assets"

ICON_MISSIONS = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 21V4"/><path d="M5 4c4-2.5 7 2.5 14 0v10c-7 2.5-10-2.5-14 0"/></svg>'
ICON_SITE = ('<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18"/>'
             '<path d="M12 3c3 3 3.6 6 3.6 9s-.6 6-3.6 9c-3-3-3.6-6-3.6-9s.6-6 3.6-9z"/></svg>')

ICON_SETTINGS = ('<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/>'
                 '<path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1"/></svg>')

ICON_CHAT = ('<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v11H9l-5 4z"/></svg>')

def nav_status() -> str:
    """The loop's state for the top bar. It only says Running while the loop really holds the lock."""
    if not loop_running():
        text, dot = "Loop off", ""
    elif orchestrator.STOP_FLAG.exists():
        text, dot = "Stopping", "stopping"
    else:
        text, dot = "Running", "on"
    return f'<span class="status {dot}"><span class="dot {dot}" aria-hidden="true"></span>{text}</span>'

def page(title: str, body: str, poll: bool = False) -> HTMLResponse:
    # Refresh itself when the loop changes something, unless the human is typing.
    script = f"""<script>
let sig = "{signature()}";
setInterval(async () => {{
  const busy = [...document.querySelectorAll("textarea, input[type=text]")].some(e => e.value.trim() && e.value !== e.defaultValue)
            ||["TEXTAREA", "INPUT"].includes(document.activeElement.tagName)
            || document.body.classList.contains("chat-open");
  if (busy || document.hidden) return;
  try {{ const s = await (await fetch("/api/signature")).text(); if (s !== sig) location.reload(); }} catch (e) {{}}
}}, 5000);
</script>{ELAPSED_JS}""" if poll else ""
    html = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{escape(title)}</title>{THEME_INIT}<style>{CSS}{UI_CSS}</style></head><body><main>"
            f'<nav class="top"><a class="brand" href="/">Mission Loop</a>'
            f'<span class="links">{nav_status()}<a href="/">{ICON_MISSIONS}Missions</a>'
            f'<a href="/site/" target="_blank">{ICON_SITE}Published site</a>'
            f'<a href="/settings">{ICON_SETTINGS}Settings</a>{THEME_TOGGLE}</span></nav>'
            f"{body}</main>{chat_widget()}{script}</body></html>")
    return HTMLResponse(html)

def flash(request: Request) -> str:
    done, err = request.query_params.get("done"), request.query_params.get("error")
    if err in ERRORS:
        return f'<div class="flash err">{escape(ERRORS[err])}</div>'
    if done in MESSAGES:
        return f'<div class="flash">{escape(MESSAGES[done])}</div>'
    return ""

def doing(e: dict) -> str:
    """What one working agent is doing, e.g. "Source finder is working on <topic> · 2m 3s"."""
    since = f' · <span data-since="{float(e["since"])}">{elapsed(e["since"])}</span>'
    if not e["slug"]:                      # the mission finder works for the desk, not one mission
        return f'<strong>{activity.AGENTS[e["agent"]]}</strong> is looking for new missions{since}'
    m = store.get_mission(e["slug"])
    return (f'<strong>{activity.AGENTS[e["agent"]]}</strong> is working on '
            f'<a href="/m/{quote(e["slug"])}">{escape(m.topic if m else e["slug"])}</a>{since}')

def agent_strip(now: list[dict]) -> str:
    """Every agent in pipeline order, with the ones working this moment lit up (one per worker)."""
    working = {e["agent"]: e["worker"] for e in now}
    def chip(key: str, name: str) -> str:
        if key not in working:
            return f'<span class="agent">{name}</span>'
        worker = working[key]
        tag = f' data-worker="{worker}"' if worker in settings.WORKERS else ""
        return f'<span class="agent on" aria-current="step"{tag}>{name}</span>'
    chips = [chip(key, name) for key, name in activity.AGENTS.items()
             if key in working or (key != "contacts" or settings.OUTREACH)
             and (key != "1f916" or agents.f916_enabled())
             and (key != "bluesky" or agents.bluesky_enabled())]
    strip = '<span class="arrow" aria-hidden="true">›</span>'.join(chips)
    lines = [(f'<span class="who">{settings.WORKERS[e["worker"]]}</span> ' if e["worker"] in settings.WORKERS
              and len(now) > 1 else "") + doing(e) for e in now]
    status = "<br>".join(lines) or "No agent is working right now."
    stage = " data-lab" if working and settings.animations() else ""   # a pixel researcher walks on a lit pill
    return f'<div class="agents"{stage}>{strip}</div><div class="now">{status}</div>'

def loop_panel(back: str) -> str:
    running, stopping = loop_running(), orchestrator.STOP_FLAG.exists()
    if running and stopping:
        state, dot = "Loop is stopping: saving the current work (2 minutes at most)", "stopping"
        controls = ""
    elif running:
        state, dot = "Loop is running", "on"
        controls = (f'<form method="post" action="/loop/wake"><input type="hidden" name="back" value="{back}">'
                    f'<button>Check for work now</button></form>'
                    f'<form method="post" action="/loop/stop"><input type="hidden" name="back" value="{back}">'
                    f'<button>Stop loop</button></form>')
    else:
        state, dot = "Loop is off. Nothing will be researched, written or published until you start it", ""
        controls = (f'<form method="post" action="/loop/start"><input type="hidden" name="back" value="{back}">'
                    f'<span class="hint">every</span><input type="number" name="every" value="5" min="1" step="1">'
                    f'<span class="hint">min</span><button class="primary">Start loop</button></form>')
    strip = agent_strip(running_now()) if running else ""
    return f'<div class="loop"><span class="dot {dot}"></span><span class="grow">{state}</span>{controls}{strip}</div>'

def finder_panel() -> str:
    line = store.get_inspiration()
    state = "Each time the loop has caught up on its missions, it searches around this line and adds 1 to 3 missions." if line \
        else "Off until you set a line."
    return f"""<form method="post" action="/inspiration" class="actions">
  <label for="inspiration" style="margin-top:0">Mission finder inspiration <span class="hint">{state} Clear it to turn the finder off.</span></label>
  <input type="text" id="inspiration" name="inspiration" maxlength="500" value="{escape(line, quote=True)}" placeholder="e.g. Who really decides what the Federal Reserve does?">
  <p><button>Save inspiration</button></p>
</form>"""

def channel_names(m: Mission) -> str:
    """Where a published mission went, e.g. "Local site, 1F916" or "Local site · 1F916 queued #2"."""
    names = ", ".join(orchestrator.CHANNEL_NAMES.get(k, k) for k in orchestrator.channels(m))
    if agents.f916_enabled() and "1f916" not in m.channels:
        slugs = [e["slug"] for e in orchestrator.queue(f916)]
        names += f" · 1F916 queued #{slugs.index(m.slug) + 1}" if m.slug in slugs else ""
    if agents.bluesky_enabled() and "bluesky" not in m.channels:
        names += " · Bluesky queued"
    return names

def f916_status() -> str:
    """One line on the 1F916 channel: who we post as, how many wait, when the next post can go."""
    if not agents.f916_enabled():
        return ('1F916 is off. Join on the <a href="/settings">Settings</a> page, '
                "or once with <code>python run.py join-1f916 &lt;handle&gt;</code>.")
    queued, until, state = len(orchestrator.queue(f916)), f916.spent_until(), f916.load_state()
    if until and until > clock.now():
        when = f"today's post is used; next one after {until:%b %-d %H:%M} UTC"
    else:
        when = "can post now" if queued else "nothing waiting"
    problem = state.get("note", "") if not state.get("spent_until") else ""
    line = (f'1F916: posting as <strong>{escape(f916.key()["handle"])}</strong> on {escape(f916.api())}, one article a day, newest first · '
            f"{queued} queued · {when}")
    return line + (f'<br><span class="hint">Last try failed: {escape(problem)}</span>' if problem else "")

def bluesky_status() -> str:
    """One line on the Bluesky channel: who we post as, how many wait, and the last failure if any."""
    if not agents.bluesky_enabled():
        return ('Bluesky is off. Add your handle and an app password on the <a href="/settings">Settings</a> page, '
                "or set <code>BLUESKY_HANDLE</code> and <code>BLUESKY_APP_PASSWORD</code> in .env and run "
                "<code>python run.py check-bluesky</code>.")
    queued, problem = len(orchestrator.queue(bluesky)), bluesky.load_state().get("note", "")
    server = "" if bluesky._service() == settings.BLUESKY_DEFAULT else f" on {escape(bluesky._service())}"
    line = (f"Bluesky: posting as <strong>{escape(bluesky.handle())}</strong>{server}, at most one article every 10 minutes, "
            f"newest first · {queued} queued")
    if not settings.site_url():
        line += ('<br><span class="hint">No site address, so posts link to the 1F916 copy when there '
                 "is one and otherwise carry no link.</span>")
    return line + (f'<br><span class="hint">Last try failed: {escape(problem)}</span>' if problem else "")


# ---------- settings: the two workers and the LLM connection ----------

WORKER_BLURBS = {
    "assistant": "Young, quick and tireless. Does the legwork: searching and reading pages, with the small model.",
    "pi": "Tenured, slow and thorough. Does the thinking and the writing, with the big model.",
}
ROLE_STEPS = {                             # where each worker's model is used; the rest of the pipeline is code
    "assistant": ["<strong>Source finder</strong>: searches the web and reads pages for each mission (up to 15 minutes)",
                  "<strong>Mission finder</strong>: its searches around your inspiration line",
                  "<strong>Contact finder</strong>: looks for people to interview (only while outreach is on)"],
    "pi": ["<strong>Brief writer</strong>: turns the gathered sources into the research brief",
           "<strong>Article writer</strong>: writes the article from the approved brief",
           "<strong>Mission finder</strong>: picks the new missions from what the searches found"],
}

def gb(size: int) -> str:
    return f"{size / 1e9:.1f} GB" if size >= 1e8 else f"{size / 1e6:.0f} MB"

def model_options(current: str, conn: dict) -> str:
    names = [m["name"] for m in conn["models"]]
    opts = [f'<option value="{escape(m["name"], quote=True)}"{" selected" if m["name"] == current else ""}>'
            f'{escape(m["name"])} · {gb(m["size"])}{" · loaded" if m["name"] in conn["loaded"] else ""}</option>'
            for m in conn["models"]]
    if not current:                        # nothing chosen on this machine yet: the code has no default
        opts.insert(0, '<option value="" selected disabled>Choose a model</option>')
    elif current not in names:             # saved before, or Ollama is closed: still show what it is set to
        missing = "not pulled" if conn["ok"] else "Ollama not reachable"
        opts.insert(0, f'<option value="{escape(current, quote=True)}" selected>{escape(current)} ({missing})</option>')
    return "".join(opts)

def worker_card(worker: str, conn: dict, now: list[dict]) -> str:
    on, model, other = settings.worker_on(worker), settings.worker_model(worker), settings.other(worker)
    name, other_name = settings.WORKERS[worker], settings.WORKERS[other]
    steps = "".join(f"<li>{s}</li>" for s in ROLE_STEPS[worker])
    if not on:
        note = (f'<p class="note hint">Off: the {other_name} does these steps with '
                f"<code>{escape(settings.worker_model(other) or 'its model')}</code>, one task at a time.</p>")
    elif not model:
        note = '<p class="note"><strong>No model chosen yet.</strong> The loop waits until this worker has one.</p>'
    else:
        extra = "" if settings.worker_on(other) else (f'<li>and, while the {other_name} is off, every one of '
                                                      "its steps too</li>")
        steps += extra
        busy = next((e for e in now if e["worker"] == worker), None)
        note = f'<p class="note">Now: {doing(busy)}</p>' if busy else ""
    checked = " checked" if on else ""
    crew = f'<div class="crew" data-crew="{worker}" aria-hidden="true"></div>' if settings.animations() else ""
    return f"""<form method="post" action="/settings/worker" class="worker {"on" if on else "off"}">
  <input type="hidden" name="worker" value="{worker}">
  <div class="head"><h3>{name}</h3>
    <label class="switch"><input type="checkbox" role="switch" name="on" value="on"{checked} onchange="this.form.submit()" aria-label="{name} on"></label></div>
  <p class="tagline hint">{WORKER_BLURBS[worker]}</p>
  <label for="{worker}_model">Model</label>
  <select id="{worker}_model" name="model" onchange="this.form.submit()">{model_options(model, conn)}</select>
  <label>Uses this model for</label>
  <ul>{steps}</ul>
  {note}<div class="grow"></div>
  <noscript><p><button>Save</button></p></noscript>
  {crew}
</form>"""

def workers_section(conn: dict) -> str:
    now = running_now()
    a, p = (f"<code>{escape(settings.worker_model(w) or 'no model yet')}</code>" for w in settings.WORKERS)
    if settings.parallel():
        lead = (f"Both workers are on, so they work at the same time whenever each has a task: the Research "
                f"Assistant searches with {a} while the Principal Investigator writes with {p}.")
    else:
        w = next(w for w in settings.WORKERS if settings.worker_on(w))
        lead = (f"Only the {settings.WORKERS[w]} is on: it does every step with "
                f"<code>{escape(settings.worker_model(w) or 'no model yet')}</code>, one task at a time.")
    if settings.missing_models():          # the code has no models of its own: this machine picks them
        lead = ("<strong>Choose a model for each worker that is on; the loop won't start until you do.</strong> "
                "Both on: they work at the same time whenever each has a task.")
    else:
        lead += " Models are this machine's choice, saved in <code>data/settings.json</code>; nothing is built in."
    cards = "".join(worker_card(w, conn, now) for w in settings.WORKERS)
    return (f'<h2 class="group">Workers</h2><p>{lead} The source checker, article checker and publisher are '
            f'code and use no model.</p><div class="workers">{cards}</div>')

def ollama_form() -> str:
    saved, env = settings.saved().get("ollama_host", ""), settings._env("OLLAMA_HOST")
    source = ("from OLLAMA_HOST in .env" if env and not saved else
              "saved on this page" if saved else "the standard local address")
    return f"""<form method="post" action="/settings/ollama" class="inline">
  <label for="ollama_host" class="sr">Ollama address</label>
  <input type="text" id="ollama_host" name="ollama_host" maxlength="300" value="{escape(saved, quote=True)}" placeholder="{escape(env or settings.OLLAMA_DEFAULT, quote=True)}">
  <button>Save address</button><span class="hint">now {source}; empty goes back to .env or {settings.OLLAMA_DEFAULT}</span>
</form>"""

def connection_section(conn: dict) -> str:
    host = escape(conn["host"])
    if not conn["ok"]:
        return (f'<h2 class="group">LLM connection</h2><p>Ollama at <code>{host}</code> is not reachable '
                f'({escape(conn["error"])}). Start the Ollama app, or point this at the machine running it; its '
                "models show here and in the workers' model lists once it answers.</p>" + ollama_form())
    users: dict[str, list[str]] = {}
    for w in settings.WORKERS:
        if settings.worker_on(w):
            users.setdefault(settings.worker_model(w), []).append(settings.WORKERS[w])
    rows = "".join(
        f'<tr><td><code>{escape(m["name"])}</code></td><td class="size">{gb(m["size"])}</td>'
        f'<td class="size">{"in memory, " + gb(conn["loaded"][m["name"]]) if m["name"] in conn["loaded"] else ""}</td>'
        f'<td>{", ".join(users.get(m["name"], []))}</td></tr>' for m in conn["models"])
    count = len(conn["models"])
    return (f'<h2 class="group">LLM connection</h2><p>Ollama at <code>{host}</code> · connected · '
            f'{count} model{"" if count == 1 else "s"}. The workers can use any of them; '
            f'<code>ollama pull &lt;model&gt;</code> adds more.</p>{ollama_form()}'
            f'<table class="events models"><tr><td class="when">Model</td><td class="when">On disk</td>'
            f'<td class="when">Loaded</td><td class="when">Used by</td></tr>{rows}</table>')

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

def settings_body(request: Request, error: str = "") -> str:
    url, env = settings.site_url(), settings.SITE_URL
    count = len(publisher.entries())
    feeds = ('<a href="/site/feed.xml" target="_blank">site/feed.xml</a> (RSS) · '
             '<a href="/site/feed.json" target="_blank">site/feed.json</a> (JSON Feed)')
    hosted = escape(url.rstrip("/") + "/")
    links = (f"Links in the feeds are full addresses under {hosted}; once the site is there, its feeds are "
             f"{hosted}feed.xml and {hosted}feed.json." if url else
             "No site address yet, so links in the feeds are relative to the feed file. "
             "That works wherever the site folder is served.")
    source = (' <span class="hint">(from MISSION_LOOP_SITE_URL in .env; saving here overrides it)</span>'
              if env and not settings.saved().get("site_url") else "")
    if agents.f916_enabled():
        f916_row = f"<p>{f916_status()}</p>"
    elif os.environ.get("MISSION_LOOP_STUBS") == "1":
        f916_row = "<p>1F916 is off while MISSION_LOOP_STUBS=1, so a test run never posts for real.</p>"
    else:
        f916_row = f"""<p>Off. Joining makes a permanent, public citizen on <a href="{escape(settings.f916_url(), quote=True)}" target="_blank" rel="noopener">{escape(settings.f916_url())}</a>.
Its secret key is saved to <code>data/1f916.json</code> on this machine only; there is no recovery if it is lost.</p>
<form method="post" action="/settings/join-1f916" class="actions">
  <label for="handle" style="margin-top:0">Handle <span class="hint">2 to 32 letters, digits, _ or -; it can't be changed later</span></label>
  <input type="text" id="handle" name="handle" required maxlength="32" pattern="[A-Za-z0-9_-]{{2,32}}" placeholder="e.g. loop-desk">
  <label for="f916_url">Server <span class="hint">the 1F916 to join; the key only works there, so it is saved with the key</span></label>
  <input type="text" id="f916_url" name="f916_url" maxlength="300" value="{escape(settings.f916_url(), quote=True)}">
  <p><button>Join 1F916</button></p>
</form>"""
    if agents.bluesky_enabled():
        forget = ("""<form method="post" action="/settings/bluesky" class="actions"><input type="hidden" name="forget" value="1">
  <p><button>Disconnect Bluesky</button></p></form>""" if bluesky.saved_credentials() else
                  '<p class="hint">Set in .env; remove BLUESKY_HANDLE and BLUESKY_APP_PASSWORD there to turn it off.</p>')
        bluesky_row = f"<p>{bluesky_status()}</p>{forget}"
    elif os.environ.get("MISSION_LOOP_STUBS") == "1":
        bluesky_row = "<p>Bluesky is off while MISSION_LOOP_STUBS=1, so a test run never posts for real.</p>"
    else:
        bluesky_row = f"""<p>Off. Each published article gets a short post (title and dek) linking to the full text.
Use an app password from Bluesky's Settings &gt; Privacy and security &gt; App passwords, never your account password.
It is checked with Bluesky, then saved to <code>data/bluesky.json</code> on this machine only.</p>
<form method="post" action="/settings/bluesky" class="actions">
  <label for="bsky_handle" style="margin-top:0">Handle <span class="hint">e.g. yourname.bsky.social</span></label>
  <input type="text" id="bsky_handle" name="handle" required maxlength="253" autocomplete="username" placeholder="yourname.bsky.social">
  <label for="bsky_password">App password</label>
  <input type="password" id="bsky_password" name="app_password" required maxlength="64" autocomplete="off" placeholder="xxxx-xxxx-xxxx-xxxx">
  <label for="bsky_service">Server <span class="hint">optional: only for an account on another Bluesky server</span></label>
  <input type="text" id="bsky_service" name="service" maxlength="300" placeholder="{escape(settings.bluesky_service(), quote=True)}">
  <p><button>Check and turn on Bluesky</button></p>
</form>"""
    err = f'<div class="flash err">{escape(error)}</div>' if error else flash(request)
    checked = " checked" if settings.animations() else ""
    conn = llm.connection()
    sprites = RESEARCHERS if settings.animations() else ""
    return f"""{err}<h1>Settings</h1>
{workers_section(conn)}
{connection_section(conn)}
<h2 class="group">Display</h2>
<form method="post" action="/settings/animations" class="actions">
  <label class="switch"><input type="checkbox" role="switch" name="animations" value="on"{checked} onchange="this.form.submit()">
  Animations <span class="hint">little pixel researchers who drop by in-progress missions on the Missions page, and stand by their worker here</span></label>
  <noscript><p><button>Save</button></p></noscript>
</form>
<h2 class="group">Feed</h2>
<p>Every published article goes to the site and to its feeds, side by side. Channels post from the feed.</p>
<p>{feeds} · {count} article{"" if count == 1 else "s"}</p>
<form method="post" action="/settings/site-url" class="actions">
  <label for="site_url" style="margin-top:0">Site address{source} <span class="hint">optional: where the site is hosted, e.g. https://example.com/news/. Leave empty while it is only on this machine.</span></label>
  <input type="text" id="site_url" name="site_url" maxlength="300" value="{escape(settings.saved().get("site_url", ""), quote=True)}" placeholder="{escape(env or "https://example.com/news/", quote=True)}">
  <p class="hint">{links}</p>
  <p><button>Save site address</button></p>
</form>
<h2 class="group">Channels</h2>
<h3>Local site and feeds</h3><p>Always on.</p>
<h3>1F916</h3>{f916_row}
<h3>Bluesky</h3>{bluesky_row}
{sprites}"""

async def settings_page(request: Request) -> HTMLResponse:
    return page("Settings", settings_body(request))

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
        return page("Settings", settings_body(request, f"1F916 did not join: {e}"))
    if loop_running():
        wake()
    return go("/settings", done="joined")

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
        return page("Settings", settings_body(request, f"Bluesky did not accept that login: {e}"))
    bluesky.save_credentials(handle, password, service)
    if loop_running():
        wake()
    return go("/settings", done="bluesky_on")


# ---------- epics ----------

def mission_pill(m: Mission, epics: dict[str, Epic]) -> str:
    e = epics.get(m.epic)
    return epic_pill(e.title, proposed=e.status == "proposed") if e else ""

def epic_datalist() -> str:
    """Suggestions for every epic field: pick an approved epic, or type a new name."""
    options = "".join(f'<option value="{escape(e.title, quote=True)}">' for e in store.all_epics("active"))
    return f'<datalist id="epic-names">{options}</datalist>'

def epic_proposals(missions: list[Mission]) -> str:
    """The finder's proposed epics, each waiting for the human to approve, rename, redirect or reject it."""
    active = store.all_epics("active")
    epics = {e.slug: e for e in store.all_epics()}
    out = []
    for e in store.all_epics("proposed"):
        ms = [m for m in missions if m.epic == e.slug]
        slug = quote(e.slug)
        items = "".join(f'<li><a href="/m/{quote(m.slug)}">{escape(m.topic)}</a>'
                        + (f' <span class="hint">· if rejected: {escape(epics[m.epic_fallback].title)}</span>'
                           if m.epic_fallback in epics else ' <span class="hint">· if rejected: no epic</span>')
                        + "</li>" for m in ms)
        move = ""
        if active:
            options = "".join(f'<option value="{escape(a.slug, quote=True)}">{escape(a.title)}</option>' for a in active)
            move = (f'<form method="post" action="/epics/{slug}/merge"><select name="into" aria-label="Existing epic">'
                    f'{options}</select><button>Move them there</button></form>')
        out.append(
            f'<div class="actions epic-review"><p style="margin-top:0">{epic_pill(e.title, proposed=True)} '
            f'The mission finder proposes a new epic for {len(ms)} missions:</p><ul>{items}</ul>'
            f'<div class="buttons"><form method="post" action="/epics/{slug}/approve">'
            f'<input type="text" name="title" value="{escape(e.title, quote=True)}" maxlength="48" aria-label="Epic name">'
            f'<button class="primary">Approve epic</button></form>{move}'
            f'<form method="post" action="/epics/{slug}/reject"><button>Reject</button></form></div>'
            f'<p class="hint">Edit the name before approving to rename it. Reject puts each mission back in the '
            f'existing epic the finder picked for it.</p></div>')
    return "".join(out)

def epic_list(missions: list[Mission]) -> str:
    active = store.all_epics("active")
    if not active:
        return ""
    rows = "".join(
        f'<form method="post" action="/epics/{quote(e.slug)}/rename" class="inline">'
        f'<input type="text" name="title" value="{escape(e.title, quote=True)}" maxlength="48" aria-label="Epic name">'
        f'<span class="hint">{sum(m.epic == e.slug for m in missions)} missions</span><button>Rename</button></form>'
        for e in active)
    return f'<details><summary>Epics ({len(active)})</summary>{rows}</details>'

def lab_mood(m: Mission, now: list[dict]) -> str:
    """How the pixel researcher on an in-progress card behaves: busy while an agent works on this
    mission, waiting while the loop runs other work, idle (mostly napping) while the loop is off."""
    if working_on(m, now):
        return "busy"
    return "waiting" if loop_running() else "idle"

def card(m: Mission, now: list[dict] | None = None, epics: dict[str, Epic] | None = None, lab: str = "") -> str:
    epics = {e.slug: e for e in store.all_epics()} if epics is None else epics
    color = tone(m)
    working = working_on(m, now)
    sub = working or next_step(m)
    if m.state == State.DONE and m.published_at:
        sub = f"Published {m.published_at:%b %-d, %Y} · {channel_names(m)}"
    classes = " ".join(c for c in ("card", color, "active" if working else "") if c)
    stage = f' data-lab="{lab}"' if lab else ""
    role = orchestrator.upcoming_role(m) if lab else None
    if role:                               # visitors come from the crew of the worker that does its next step
        stage += f' data-worker="{settings.worker_for(role)}"'
    return (f'<a class="{classes}" href="/m/{quote(m.slug)}"{stage}><div class="row"><span class="topic">{escape(m.topic)}</span>'
            f'<span class="badge {color}">{escape(label(m))}</span></div>'
            f'<div class="sub">{mission_pill(m, epics)}{escape(m.slug)}{" · " + escape(sub) if sub else ""}</div></a>')

# ---------- pages ----------

async def home(request: Request) -> HTMLResponse:
    missions = store.all_missions()
    groups = [("Needs you", [m for m in missions if needs_you(m)]),
              ("In progress", [m for m in missions if not needs_you(m) and m.state != State.DONE]),
              ("Published", [m for m in missions if m.state == State.DONE])]
    now = running_now()
    epics = {e.slug: e for e in store.all_epics()}
    body = [flash(request), loop_panel("/"), finder_panel()]
    proposals = epic_proposals(missions)
    if proposals:
        body.append('<h2 class="group">New epic proposed</h2>' + proposals)
    if not missions:
        body.append('<p class="muted">No missions yet. Add one below.</p>')
    for title, ms in groups:
        if ms:
            body.append(f'<h2 class="group">{title} ({len(ms)})</h2>')
            if title == "Published":
                body.append(f'<p class="muted">{f916_status()}</p><p class="muted">{bluesky_status()}</p>')
            lab = settings.animations() and title == "In progress"
            body.append("".join(card(m, now, epics, lab_mood(m, now) if lab else "") for m in ms))
    body.append(epic_list(missions))
    body.append(epic_datalist() + """<h2 class="group">New mission</h2>
<form method="post" action="/missions" class="actions">
  <label for="topic">Topic <span class="hint">what to research, in one line</span></label>
  <input type="text" id="topic" name="topic" required placeholder="e.g. Did the new transit levy pass, and who paid for the campaign?">
  <label for="objective">Objective <span class="hint">optional: the question to answer, what counts as evidence, any hypothesis to test</span></label>
  <textarea id="objective" name="objective" rows="4"></textarea>
  <label for="slug">Short name <span class="hint">optional; made from the topic if left blank</span></label>
  <input type="text" id="slug" name="slug" placeholder="transit-levy">
  <label for="epic">Epic <span class="hint">optional: pick one, or type a new name of 1 to 4 words</span></label>
  <input type="text" id="epic" name="epic" list="epic-names" maxlength="48" placeholder="e.g. Fed Policy Capture">
  <p><button class="primary">Add mission</button></p>
</form>""")
    tail = log_tail()
    if tail:
        body.append('<details><summary>Loop log (data/loop.log)</summary><pre class="notes">'
                    + escape("\n".join(tail)) + "</pre></details>")
    if settings.animations():
        body.append(RESEARCHERS)
    return page("Mission Loop", "\n".join(body), poll=True)

def actions_for(m: Mission) -> str:
    slug = quote(m.slug)
    def send_back(what: str, placeholder: str) -> str:
        return (f'<details><summary>Send it back with notes</summary>'
                f'<form method="post" action="/m/{slug}/reject"><label for="notes">What should the {what} fix?</label>'
                f'<textarea id="notes" name="notes" rows="4" required placeholder="{placeholder}"></textarea>'
                f'<p><button>Send back to {what}</button></p></form></details>')
    if m.state == State.BRIEF_READY and not m.approved:
        return (f'<div class="actions"><div class="buttons">'
                f'<form method="post" action="/m/{slug}/approve"><button class="primary">Approve brief</button></form>'
                f'</div><p class="hint">Approving sends it to the writer.</p>'
                + send_back("researcher", "e.g. Find the FAA's own incident report; the thesis overstates the denial.")
                + "</div>")
    if m.state == State.ARTICLE_READY and not m.reviewed:
        return (f'<div class="actions"><div class="buttons">'
                f'<form method="post" action="/m/{slug}/publish"><button class="primary">Publish article</button></form>'
                f'<a class="btn" href="/m/{slug}/preview" target="_blank">Open full-page preview</a>'
                f'</div><p class="hint">Publishing writes it to the local site (site/{escape(m.slug)}/)'
                + (" and queues it for 1F916, which takes one post a day, newest first" if agents.f916_enabled()
                   else "")
                + (" and announces it on Bluesky" if agents.bluesky_enabled() else "") + ".</p>"
                + send_back("writer", "e.g. The second section repeats the first; lead with the FAA findings.")
                + "</div>")
    if m.state == State.PARKED:
        what = "article" if orchestrator.parked_while_writing(m) else "brief"
        if orchestrator.parked_draft(m):
            push = f'<form method="post" action="/m/{slug}/push"><button>Push through anyway</button></form>'
            push_hint = (f"Push through sends the last failed {what} on to your review. It stays marked as "
                         f"failing its check, including on the published page.")
        else:
            push = ""
            push_hint = f"It can't be pushed through because no attempt left a finished {what}."
        return (f'<div class="actions"><div class="buttons">'
                f'<form method="post" action="/m/{slug}/requeue"><button class="primary">Requeue</button></form>{push}'
                f'</div><p class="hint">Requeue gives it a fresh set of {m.max_attempts} attempts and keeps the notes '
                f'below so the agent can learn from them. {push_hint}</p></div>')
    if m.state == State.DONE:
        return (f'<div class="actions"><div class="buttons">'
                f'<a class="btn" href="/site/{slug}/" target="_blank">View on the published site</a></div>'
                f'{channels_html(m)}</div>')
    return ""

def channels_html(m: Mission) -> str:
    rows = []
    for key, post in orchestrator.channels(m).items():
        name = escape(orchestrator.CHANNEL_NAMES.get(key, key))
        where = escape(post.where)
        link = (f'<a href="{where}" target="_blank" rel="noopener">{where}</a>' if post.where.startswith("https://")
                else f'<a href="/site/{quote(m.slug)}/" target="_blank">{where}</a>')
        rows.append(f"<li>{name}: {link} · {post.at:%b %-d, %Y %H:%M} UTC</li>")
    if agents.f916_enabled() and "1f916" not in m.channels:
        slugs = [e["slug"] for e in orchestrator.queue(f916)]
        place = f"#{slugs.index(m.slug) + 1} of {len(slugs)}" if m.slug in slugs else "queued"
        rows.append(f"<li>1F916: waiting, {place} in the queue (one post a day, newest first)</li>")
    if m.channel_errors.get("1f916"):
        rows.append(f'<li>1F916 refused the last try: {escape(m.channel_errors["1f916"])}. '
                    "It stays queued behind the others.</li>")
    if agents.bluesky_enabled() and "bluesky" not in m.channels:
        rows.append("<li>Bluesky: waiting (at most one post every 10 minutes, newest first)</li>")
    if m.channel_errors.get("bluesky"):
        rows.append(f'<li>Bluesky refused the last try: {escape(m.channel_errors["bluesky"])}. '
                    "It stays queued behind the others.</li>")
    return f'<p class="hint" style="margin-bottom:.2rem">Published to</p><ul>{"".join(rows)}</ul>'

async def mission(request: Request) -> Response:
    slug = request.path_params["slug"]
    m = store.get_mission(slug)
    if not m:
        return page("Not found", '<h1>No such mission</h1><p><a href="/">Back to missions</a></p>')
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
    ask = escape(f"Where is the mission {slug} at, and what is taking so long?", quote=True)
    body.append(f'<p><button type="button" class="btn" data-ask="{ask}">Ask the assistant about this mission</button></p>')
    body.append(actions_for(m))
    current = epics.get(m.epic)
    hint = " (proposed; approve it on the home page)" if current and current.status == "proposed" else ""
    body.append(f'{epic_datalist()}<form method="post" action="/m/{quote(slug)}/epic" class="inline">'
                f'<label for="epic" style="margin:0">Epic<span class="hint">{hint}</span></label>'
                f'<input type="text" id="epic" name="epic" list="epic-names" maxlength="48" '
                f'value="{escape(current.title if current else "", quote=True)}" placeholder="none">'
                f'<button>Save epic</button></form>')

    if m.validator_notes:
        title = "Why it was parked" if m.state == State.PARKED else \
                "Feedback being worked on" if m.validator_notes.startswith("Human reviewer feedback") else \
                "Last check's notes"
        body.append(f'<h2>{title}</h2><pre class="notes">{escape(m.validator_notes)}</pre>')
    if m.unchecked_brief:
        body.append('<h2>Brief pushed through despite failing the source check</h2>'
                    f'<pre class="notes">{escape(m.unchecked_brief)}</pre>')
    if m.unchecked_article:
        body.append('<h2>Article pushed through despite failing its check</h2>'
                    f'<pre class="notes">{escape(m.unchecked_article)}</pre>')

    brief, article = _brief(slug), _article(slug)
    checked = not m.unchecked_brief
    preview = Path("missions") / slug / "preview.html"
    if article and preview.exists() and m.state in (State.ARTICLE_READY, State.DONE, State.WRITING):
        body.append(f'<h2>Article</h2><iframe class="article" src="/m/{quote(slug)}/preview"></iframe>')
    if brief and m.state == State.BRIEF_READY:
        body.append(f'<h2>Research brief</h2><div class="doc">{brief_html(brief, checked)}</div>')
    elif brief:
        body.append(f'<details><summary>Research brief</summary><div class="doc">{brief_html(brief, checked)}</div></details>')
    rejected = Path("missions") / slug / "brief.rejected.md"
    if rejected.exists() and m.state in (State.PARKED, State.RESEARCHING, State.QUEUED):
        body.append('<details><summary>Last brief that failed the source check</summary>'
                    f'<pre class="notes">{escape(rejected.read_text())}</pre></details>')
    rejected = Path("missions") / slug / "article.rejected.md"
    if rejected.exists() and m.state in (State.PARKED, State.WRITING):
        body.append('<details><summary>Last article that failed its check</summary>'
                    f'<pre class="notes">{escape(rejected.read_text())}</pre></details>')

    events = store.events(slug)
    if events:
        rows = "".join(f'<tr><td class="when">{escape(e["at"][:16].replace("T", " "))}</td>'
                       f'<td>{escape(LABELS.get(State(e["to_state"]), e["to_state"]))}</td>'
                       f'<td>{escape(e["note"] or "")}</td></tr>' for e in reversed(events))
        body.append(f'<h2>History</h2><table class="events">{rows}</table>')
    return page(m.topic, "\n".join(body), poll=True)

async def preview(request: Request) -> Response:
    slug = request.path_params["slug"]
    path = Path("missions") / slug / "preview.html"
    if not SLUG.match(slug) or not store.get_mission(slug) or not path.exists():
        return PlainTextResponse("No preview yet.", status_code=404)
    return FileResponse(path, media_type="text/html")

# ---------- chat: ask the assistant what the loop is doing ----------
# A bubble on every page opens a panel over the left third. The conversation is kept in this browser tab
# (sessionStorage) so the page's own refreshes don't wipe it; the assistant itself remembers nothing.
# It needs only Ollama, not the loop: its tools read what the loop leaves on disk.

SUGGESTIONS = ["What's going on right now?", "What needs me?", "What's taking so long?", "What got parked, and why?"]

CHAT_JS = """<script>
(() => {
  const panel = document.getElementById("chat-panel"), bubbleBtn = document.getElementById("chat-bubble");
  const log = document.getElementById("chat-log"), form = document.getElementById("chat-form");
  const box = document.getElementById("chat-q"), pickModel = document.getElementById("chat-model");
  const hint = document.getElementById("chat-hint"), suggest = document.getElementById("chat-suggest");
  const store = (k, v) => { try { v === undefined ? null : sessionStorage.setItem(k, JSON.stringify(v));
                                  return JSON.parse(sessionStorage.getItem(k)); } catch (e) { return null; } };
  let history = store("chat-history") || [], loaded = false;

  function show(role, text, model, cls = "") {
    const div = document.createElement("div");
    div.className = `msg ${role} ${cls}`; div.textContent = text;
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
    pickModel.replaceChildren(...data.models.map(m => new Option(m === data.default ? `${m} (default, shared with the loop)` : m, m)));
    if (data.models.includes(saved)) pickModel.value = saved;
    if (data.error) hint.textContent = data.error;
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
    const wait = show("assistant", "Looking…", "", "wait");
    let answer;
    try {
      const r = await fetch("/chat/ask", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({question, model, history: history.slice(-4).map(({role, content}) => ({role, content}))})});
      answer = (await r.json()).answer;
    } catch (e) { answer = "The assistant didn't answer. Is the UI still running?"; }
    wait.remove(); show("assistant", answer, model);
    history.push({role: "user", content: question}, {role: "assistant", content: answer, model});
    history = history.slice(-20); store("chat-history", history);
    box.disabled = false; box.focus();
  }
  bubbleBtn.addEventListener("click", () => setOpen(panel.hidden));
  document.getElementById("chat-close").addEventListener("click", () => setOpen(false));
  document.getElementById("chat-clear").addEventListener("click", () => { history = []; store("chat-history", history); redraw(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !panel.hidden) setOpen(false); });
  pickModel.addEventListener("change", () => { try { localStorage.setItem("chat-model", pickModel.value); } catch (e) {} });
  form.addEventListener("submit", e => { e.preventDefault(); ask(box.value); });
  box.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(box.value); } });
  suggest.querySelectorAll("button").forEach(b => b.addEventListener("click", () => ask(b.textContent)));
  document.querySelectorAll("[data-ask]").forEach(b => b.addEventListener("click", () => ask(b.dataset.ask)));
  redraw();
  if (store("chat-open")) setOpen(true);
})();
</script>"""

def chat_widget() -> str:
    chips = "".join(f'<button type="button">{escape(q)}</button>' for q in SUGGESTIONS)
    return f"""<button type="button" id="chat-bubble" class="chat-bubble" aria-label="Ask the loop" aria-expanded="false"
 aria-controls="chat-panel">{ICON_CHAT}</button>
<aside id="chat-panel" class="chat-panel" aria-label="Ask the loop" hidden>
<header><strong>Ask the loop</strong><span><button type="button" id="chat-clear" title="Start over">Clear</button>
 <button type="button" id="chat-close" aria-label="Close">×</button></span></header>
<label class="model">Model <select id="chat-model" aria-label="Model"></select></label>
<p class="hint" id="chat-hint">It only reads, so it can't change anything, and it works with the loop off.</p>
<div class="chat-log" id="chat-log" aria-live="polite"></div>
<div class="suggest" id="chat-suggest">{chips}</div>
<form id="chat-form" class="chat-form"><textarea id="chat-q" rows="2" maxlength="600"
 placeholder="Where is the grid outage mission at?" aria-label="Your question"></textarea>
<button class="primary">Ask</button></form>
</aside>{CHAT_JS}"""

async def chat_models(request: Request) -> JSONResponse:
    """Installed models for the picker. Ollama being closed is reported, not an error page."""
    MODEL = settings.chat_model()
    try:
        names = await run_in_threadpool(agents.chat_models)
    except Exception:
        return JSONResponse({"models": [MODEL], "default": MODEL,
                             "error": "Ollama isn't reachable, so the assistant can't answer. Start the Ollama app."})
    return JSONResponse({"models": names or [MODEL], "default": names[0] if names else MODEL})

async def chat_ask(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    try:
        data = await request.json()
        question, history = str(data.get("question") or ""), data.get("history") or []
        model = str(data.get("model") or "")[:100] or None
        history = history if isinstance(history, list) else []
    except (ValueError, AttributeError):
        return JSONResponse({"answer": "That question didn't arrive in one piece. Try again."}, status_code=400)
    try:
        answer = await run_in_threadpool(agents.chat, question, [h for h in history if isinstance(h, dict)], model)
    except Exception as e:                 # Ollama closed, model missing: say so instead of a blank page
        answer = f"I couldn't answer just now ({type(e).__name__}: {str(e)[:200]}). Is Ollama running?"
    return JSONResponse({"answer": answer})

async def api_signature(request: Request) -> PlainTextResponse:
    return PlainTextResponse(signature())

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
    if store.get_mission(slug):
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
            every = max(1.0, float(form.get("every") or 5))
        except ValueError:
            every = 5.0
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
    Route("/settings", settings_page),
    Route("/chat/models", chat_models),
    Route("/chat/ask", chat_ask, methods=["POST"]),
    Route("/settings/site-url", save_site_url, methods=["POST"]),
    Route("/settings/animations", save_animations, methods=["POST"]),
    Route("/settings/worker", save_worker, methods=["POST"]),
    Route("/settings/ollama", save_ollama, methods=["POST"]),
    Route("/settings/join-1f916", join_1f916, methods=["POST"]),
    Route("/settings/bluesky", save_bluesky, methods=["POST"]),
    Route("/m/{slug}", mission),
    Route("/m/{slug}/preview", preview),
    Route("/m/{slug}/{action}", act, methods=["POST"]),
    Route("/epics/{slug}/{action}", epic_action, methods=["POST"]),
    Route("/loop/{action}", loop_action, methods=["POST"]),
    Route("/api/signature", api_signature),
    Mount("/assets", StaticFiles(directory=ASSETS)),
    Mount("/site", StaticFiles(directory="site", html=True, check_dir=False)),
])
