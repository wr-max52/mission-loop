"""Local web UI. Run it with `python run.py ui`, then open http://127.0.0.1:8770.

It is a thin layer over the same store and orchestrator functions the CLI uses: it reads missions,
shows briefs and articles, and records human decisions (approve, reject, publish, requeue, push). It never
runs agents itself. Work happens in the loop, which the UI can start, stop and wake. Bound to
127.0.0.1 only; nothing here is reachable from another machine.
"""
import asyncio, concurrent.futures, hashlib, json, os, re, subprocess, sys, threading, time
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
import activity, agents, avatars, board, clock, doctor, library, orchestrator, settings, stopping, store
from agents import llm, providers
from agents import bluesky, f916, github, publisher
from contracts import Mission, State, ResearchBrief, Article, Epic, epic_title, is_open, slugify, SITE_FOLDERS
from render import CSS, brief_html, epic_pill
from icons import icon, PATHS as ICON_PATHS, CSS as ICON_CSS
import flowchart
from flowchart import flow_steps

LOG = Path("data/loop.log")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")

LABELS = {
    State.QUEUED: "Queued", State.RESEARCHING: "Researching", State.BRIEF_READY: "Brief ready",
    State.AWAITING_COMMENTS: "Waiting for comments", State.WRITING: "Writing",
    State.ARTICLE_READY: "Article ready", State.DONE: "Published", State.PARKED: "Parked",
    State.COMBINED: "Combined",
}
MESSAGES = {
    "approved": "Approved. The loop writes the article next.",
    "published": "Marked for publishing. The loop publishes it next, before any research or writing.",
    "rejected": "Sent back with your notes.",
    "requeued": "Requeued. It starts again after the missions further along.",
    "pushed": "Pushed through. It is marked as failing its check and is waiting for your review.",
    "added": "Mission added. The loop researches it after the missions further along.",
    "added_next": "Mission added. The Research Assistant takes it next.",
    "added_started": "Mission added and the loop started. The Research Assistant takes it first.",
    "inspired": "Inspiration saved. The mission finder works from it each time the loop has caught up.",
    "uninspired": "Inspiration cleared. The mission finder and the Theorist are off.",
    "thinking": "The Theorist goes next: as the loop's next run starts, or when you start the loop.",
    "started": "Loop started.",
    "stopping": "Stop requested. The agent at work writes out what it has (2 minutes at most), then the loop exits.",
    "woken": "The loop checks for work now.",
    "epic_set": "Epic saved.",
    "epic_approved": "New epic approved.",
    "epic_moved": "Missions moved to that epic.",
    "epic_rejected": "Proposed epic rejected. Its missions went back to the epics they were in before.",
    "epic_renamed": "Epic renamed. Published pages show the new name.",
    "combined": "Combine approved. The bigger mission is queued; the missions it combines have stopped.",
    "combine_rejected": "Combine rejected. The missions carry on separately, and the Theorist won't propose it again.",
    "linked": "Linked. Each mission's research sees the other from its next search.",
    "unlinked": "Link removed. The Theorist won't draw it again.",
    "site_url_saved": "Site address saved. The feeds use full links now.",
    "site_url_cleared": "Site address cleared. The feeds use links relative to the feed file.",
    "bluesky_on": "Bluesky login checked and saved. Published articles are announced there from now on.",
    "bluesky_off": "Bluesky disconnected. Its saved login was deleted from this machine.",
    "every_saved": "Saved. The next loop you start rests that long between looks for work.",
    "library_saved": "Library settings saved. The Theorist and the chat use them on their next search.",
    "library_indexing": "Indexing the library in the background. Refresh in a minute to see how far it got.",
    "auto_on": "Auto mode on. Briefs and new epics are approved for you; articles still wait for you to publish them.",
    "auto_off": "Auto mode off. Every brief and new epic waits for you again.",
    "animations_on": "Animations on. Researchers will drop by the missions a model is working on.",
    "animations_off": "Animations off. The Missions page stays still.",
    "theme_saved": "Appearance saved.",
    "chat_color_saved": "Chat colour saved.",
    "board_style_saved": "Board look saved.",
    "ollama_saved": "Ollama address saved. Workers use the Ollama at that address from their next task.",
    "ollama_connected": "Ollama connected, and each worker without a model got one of its models. Change them any time under Workers.",
    "ollama_ready": "Ollama is connected and every worker has a model. Press play to run the loop.",
    "provider_on": "Provider connected. Its models are now in each worker's model list.",
    "provider_off": "Provider disconnected. Its saved key was deleted from this machine.",
    "worker_saved": "Worker saved. It takes its next task with that model.",
    "worker_on": "Worker on. With both on, they work at the same time whenever each has a task.",
    "worker_off": "Worker off. The other worker does its steps, one task at a time.",
    "worker_count": "Worker count saved. The loop picks it up the next time it looks for work.",
    "joined": "Joined 1F916. Published articles post there from now on, one a day, newest first.",
    "github_connected": "GitHub connected. Now pick the repo the site goes to.",
    "github_linked": "Site linked. The site was pushed to GitHub; GitHub Pages takes a minute or two to put it online.",
    "github_unlinked": "Site unlinked. The repo and the site stay on GitHub; nothing is pushed there any more.",
    "github_off": "GitHub disconnected. Its token was deleted from this machine; the repo and the site stay on GitHub.",
    "github_app_saved": "GitHub OAuth app saved. Connect GitHub to sign in with it.",
    "custom_added": "Custom channel added. Published articles go there from the next publishing pass, newest first.",
    "custom_removed": "Custom channel removed. Nothing more goes there; what it already got stays where it is.",
    "republish": "Publishing requested. The loop does it within seconds, ahead of everything else waiting.",
    "republish_off": "Publishing now. The loop is off, so this page does it; the rest of each channel's backlog waits for the loop.",
    "search_saved": "Web search key checked and saved. Research searches the web from its next task.",
    "search_unchecked": "Web search key saved, but Brave couldn't be asked about it just now. If research can't search, check the key.",
    "search_off": "Web search key removed from this machine.",
    "setup_hidden": "Setup hidden. Settings → Setup shows what is left and brings it back.",
    "setup_shown": "Setup is back at the top of the Missions page.",
    "publication_saved": "Publication saved. The site's pages, About page and feeds use it now.",
}
ERRORS = {
    "combine": "That combine can't go ahead now: a worker is busy with one of its missions, or they moved on.",
    "link": "Those two can't be linked: pick another mission, at least one of them not published yet.",
    "topic": "A mission needs a topic.",
    "theme": "Pick System, Light or Dark.",
    "board_style": "Pick a surface, cards and string from the choices shown.",
    "chat_color": "That colour wasn't a #rrggbb colour.",
    "slug": "Short name: lowercase letters, numbers and dashes only.",
    "exists": "A mission with that short name already exists.",
    "notes": "Say what needs fixing so the agent can act on it.",
    "state": "That mission isn't waiting for that action any more.",
    "running": "The loop is already running.",
    "every": "Give the rest between looks for work in whole minutes.",
    "top_n": "Pick how many passages a library search returns from the list.",
    "epic": "An epic title is 1 to 4 words.",
    "epic_exists": "There is already an epic with that name.",
    "epic_target": "Pick a different, approved epic to move them to.",
    "site_url": "The site address must be a full http:// or https:// address, or empty.",
    "bluesky": "Bluesky needs both a handle and an app password.",
    "worker_model": "Pick a model one of the connected providers offers.",
    "provider": "There is no such model provider.",
    "ollama_empty": "Ollama is running but has no models yet. Pull one first, e.g. ollama pull <model>, then connect again.",
    "ollama_host": "The Ollama address must be a full http:// or https:// address, or empty for the default.",
    "server": "A server address must be a full http:// or https:// address, or empty for the default.",
    "worker_last": "One worker has to stay on: it does the other's steps while that one is off.",
    "worker_count": f"A worker count is a whole number from 1 to {settings.MAX_COUNT}.",
    "ollama": "Ollama isn't reachable, so the model list can't be checked. Start the Ollama app and try again.",
    "handle": "A 1F916 handle is 2 to 32 letters, digits, _ or -.",
    "github_app": "That doesn't look like an OAuth app client ID (it's on the app's page on GitHub).",
    "places": "Tick at least one place to publish it to.",
    "notready": "The loop can't start yet",
    "search_key": "Brave refused that key. Copy it again from your Brave Search API dashboard.",
    "search_format": "That doesn't look like a Brave API key: paste it without spaces.",
    "publication": f"Too long: the name can be {settings.NAME_MAX} characters, the tagline {settings.TAGLINE_MAX} "
                   f"and the About statement {settings.ABOUT_MAX}.",
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
        return "Publishing again"
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
        if m.brief_sources:
            doing = "Writing the brief's claims from its checked sources."
        elif m.brief_pick:
            doing = "Checking the sources picked for the brief."
        elif m.gathered or m.interrupted_brief:
            doing = "Writing the brief from its sources."
        else:
            doing = "Carrying on the paused search." if m.search_paused else "Finding sources."
        same = m.gathered or m.interrupted_brief or m.search_paused   # the attempt already counted carries on
        try_ = m.attempts if same else m.attempts + 1
        return f"{doing} Attempt {try_} of {m.max_attempts}." if m.attempts and try_ > 1 else doing
    if m.state == State.WRITING:
        done = (f" {len(m.article_draft.sections)} of {len(m.article_draft.plan.sections)} sections written."
                if m.article_draft else "")
        return f"Writing attempt {m.write_attempts + 1} of {m.max_attempts}.{done}"
    if m.state == State.DONE and m.publish_requests:
        return "To " + ", ".join(orchestrator.channel_name(k) for k in m.publish_requests) + ", ahead of everything else."
    return ""

def in_progress(m: Mission) -> bool:
    """On the Missions page: everything not published yet, and published articles a human asked to publish again.
    A mission combined into another is not: the bigger one carries its work on."""
    return not needs_you(m) and m.state != State.COMBINED and (m.state != State.DONE or bool(m.publish_requests))

def flow_rank(m: Mission) -> int:
    """Top to bottom on the Missions page, the flow chart read from its end: closest to published first."""
    order = {State.DONE: 0, State.ARTICLE_READY: 1, State.WRITING: 2, State.AWAITING_COMMENTS: 3,
             State.BRIEF_READY: 4, State.RESEARCHING: 5, State.QUEUED: 7, State.PARKED: 8, State.COMBINED: 9}
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
    raw = ("".join(r["data"] for r in rows) + f"|{last}|{loop_running()}|{orchestrator.STOP_FLAG.exists()}|{working}"
           f"|{settings.auto_mode()}|{(doctor.waiting() or {}).get('reason', '')}|{settings.setup_dismissed()}"
           f"|{bool(settings.search_key())}")
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

ICON_BOARD = f'<svg viewBox="0 0 24 24" aria-hidden="true">{ICON_PATHS["board"]}</svg>'   # one drawing, in icons.py

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
        action, ico, text, dot = "start", "play", "Loop off", ""
        title = f"Start the loop. When it has nothing to do it rests {every:g} min (Settings) before looking again."
    elif orchestrator.STOP_FLAG.exists():
        action, ico, text, dot = "", "pause", "Stopping", "stopping"
        title = "Stopping: the agent at work saves what it has (2 minutes at most), then the loop exits."
    elif waiting := doctor.waiting():
        action, ico, text, dot = "stop", "pause", "Waiting", "stopping"
        title = f"Waiting: {waiting['reason']}. It checks again every 30 seconds. Pause to stop it."
    else:
        action, ico, text, dot = "stop", "pause", "Running", "on"
        title = "Pause the loop. The agent at work saves what it has (2 minutes at most), then the loop stops."
    button = (f'<button class="play {dot or "off"}" title="{escape(title)}" aria-label="{escape(title)}"'
              f' onclick="this.form.back.value = location.pathname"{"" if action else " disabled"}>{icon(ico)}</button>')
    return (f'<form class="loop-ctl" method="post" action="/loop/{action or "stop"}">'
            f'<input type="hidden" name="back" value="/"><input type="hidden" name="every" value="{every:g}">'
            f'{button}'
            f'<span class="status {dot}"><span class="dot {dot}" aria-hidden="true"></span>{text}</span></form>')

def auto_switch() -> str:
    """Auto mode, in the top bar on every page: approves every brief and proposed epic, never publishing."""
    on = settings.auto_mode()
    title = ("Auto mode: the loop approves every brief and every proposed epic for you. "
             "Publishing an article always waits for you.")
    return (f'<form class="auto" method="post" action="/auto"><input type="hidden" name="back" value="/">'
            f'<label class="switch" title="{escape(title)}"><input type="checkbox" role="switch" name="auto" value="on"'
            f'{" checked" if on else ""} onchange="this.form.back.value = location.pathname; this.form.submit()">'
            f'<span>Auto</span></label></form>')

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
    html = (f'<!doctype html><html lang="en"{html_attrs()}><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{escape(title)}</title>{avatars.script_tag()}<style>{CSS}{ICON_CSS}{flowchart.CSS}{UI_CSS}</style></head><body><main>"
            f'<nav class="top"><a class="brand" href="/">Mission Research Loop</a>'
            f'<span class="links">{auto_switch()}{loop_button()}<a href="/">{ICON_MISSIONS}Missions</a>'
            f'<a href="/published">{ICON_SITE}Published</a>'
            f'<a href="/board">{ICON_BOARD}Board</a>'
            f'<a href="/settings">{ICON_SETTINGS}Settings</a>'
            f'<a href="/about" target="_blank" title="How the desk works, on the published site">{icon("book")}About</a></span></nav>'
            f"{body}</main>{chat_widget()}{KEEP_SCROLL_JS}{BOARD_LINK_JS}{script}</body></html>")
    return HTMLResponse(html)

def flash(request: Request) -> str:
    done, err = request.query_params.get("done"), request.query_params.get("error")
    if err == "notready":
        why = (doctor.waiting() or {}).get("reason", "")
        return f'<div class="flash err">{escape(ERRORS[err])}{": " + escape(why) if why else ""}.</div>'
    if err in ERRORS:
        return f'<div class="flash err">{escape(ERRORS[err])}</div>'
    if done in MESSAGES:
        return f'<div class="flash">{escape(MESSAGES[done])}</div>'
    return ""

def strip_step(agent: str) -> str:
    """Where an agent sits on the strip: posting to a channel is part of the one Publish step."""
    return "publisher" if agent in ("1f916", "github", "bluesky") else agent

def doing(e: dict) -> str:
    """What one working agent is doing, e.g. "Source finder is working on <topic> · 2m 3s"."""
    since = f' · <span data-since="{float(e["since"])}">{elapsed(e["since"])}</span>'
    if not e["slug"]:                      # the Theorist and the mission finder work for the desk, not one mission
        task = "is looking across the desk for patterns" if e["agent"] == "theorist" else "is looking for new missions"
        return f'<strong>{activity.AGENTS[e["agent"]]}</strong> {task}{since}'
    m = store.get_mission(e["slug"])
    return (f'<strong>{activity.AGENTS[e["agent"]]}</strong> is working on '
            f'<a href="/m/{quote(e["slug"])}">{escape(m.topic if m else e["slug"])}</a>{since}')

def flow_status(now: list[dict], running: bool) -> str:
    """The lines under the strip: who is working on what, and for how long. With more than one worker busy,
    one line each, named."""
    lines = [(f'<span class="who">{settings.seat_name(e["worker"])}</span> ' if settings.seat_name(e["worker"])
              and len(now) > 1 else "") + doing(e) for e in now]
    return "<br>".join(lines) or ("No agent is working right now." if running else "Loop is off, so every step is idle.")

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
                     "name": activity.AGENTS[e["agent"]]} for e in now],
        "waiting": {"epic_review": len(store.all_epics("proposed")) + len(store.all_combines("proposed")),
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
    steps, lit = flow_steps(auto), {e["step"]: settings.seat_worker(e["worker"]) for e in state["working"]}
    track = flowchart.track(lit, state["waiting"], state["parked"], review_href="/#needs-you", auto=auto)
    lab = " data-lab" if settings.animations() else ""
    working = json.dumps([{k: e[k] for k in ("worker", "step", "slug")} for e in state["working"]])
    attrs = f'data-shape="{",".join(k + ("*" if kind == "auto" else "") for k, kind, _ in steps)}" ' \
            f'data-working="{escape(working, quote=True)}"'
    off = "" if state["running"] else " off"
    return (f'<section id="flow" class="flow{off}" {attrs} aria-label="Pipeline">'
            f'<div class="flow-scroll"><div class="flow-track"{lab}>{track}</div></div>'
            f'<div class="now" aria-live="polite">{state["now"]}</div></section>')

def loop_panel(back: str) -> str:
    running, stopping = loop_running(), orchestrator.STOP_FLAG.exists()
    if running and stopping:
        state, dot = "Loop is stopping: saving the current work (2 minutes at most)", "stopping"
        controls = ""
    elif running and (waiting := doctor.waiting()):
        state, dot = (f'Loop is waiting: {escape(waiting["reason"])}. It checks again every 30 seconds. '
                      f'<a href="/settings#setup">Settings → Setup</a> shows what to fix'), "stopping"
        controls = ""
    elif running:
        state, dot = "Loop is running. Pause it with the button at the top of the page", "on"
        controls = (f'<form method="post" action="/loop/wake"><input type="hidden" name="back" value="{back}">'
                    f'<button>Check for work now</button></form>')
    else:
        state, dot = ("Loop is off. Nothing will be researched, written or published until you press play at the "
                      "top of the page"), ""
        controls = ""
    return (f'<div class="loop"><span class="dot {dot}"></span><span class="grow">{state}</span>{controls}'
            f'{flow_strip(flow_state())}</div>{FLOW_JS}')

def finder_panel() -> str:
    line = store.get_inspiration()
    state = ("Each time the loop has caught up on its missions, the finder searches around this line and adds 1 to 3 "
             "missions. The Theorist evolves it as the desk's work grows: questions only, keeping your words.") if line \
        else "Off until you set a line."
    think = ('<button formaction="/theorist" title="Look across the desk and evolve the line on the loop\'s next run">'
             'Ask the Theorist</button>') if line else ""
    return heading("brain", "Inspiration") + f"""<form method="post" action="/inspiration" class="actions">
  <label for="inspiration" style="margin-top:0"><span class="hint">{state} Clear it to turn the finder and the Theorist off.</span></label>
  <input type="text" id="inspiration" name="inspiration" maxlength="500" value="{escape(line, quote=True)}" placeholder="e.g. Who really decides what the Federal Reserve does?">
  <p><button>Save inspiration</button>{think}</p>{theorist_notes()}
</form>"""

def _when(at: str) -> str:
    return escape(at[:16].replace("T", " ")) + " UTC" if at else ""

BY = {"human": "you", "chat": "the chat assistant", "theorist": "the Theorist"}   # who changed the line

def theorist_notes() -> str:
    """The Theorist's last turn and how the line got here, folded away under the inspiration box."""
    last, history = store.last_theory(), store.inspiration_history(6)
    if not last and not history:
        return ""
    parts = []
    if last.get("kind") == "board":        # a board turn, then the turn before it that looked across the desk
        if last.get("failed"):
            parts.append(f'<p class="muted">Last turn {_when(last["at"])} was a <a href="/board">board</a> turn and '
                         f'failed: {escape(last["failed"])}</p>')
        else:
            moved = last.get("moved", [])
            sized = sum(1 for mv in moved if mv.get("size") is not None)
            what = (f"moved {len(moved)} note{'s' * (len(moved) != 1)}" + (f", resizing {sized}" if sized else "")
                    if moved else "left every note where it was")
            parts.append(f'<p class="muted">Last turn {_when(last["at"])} tidied the <a href="/board">board</a>: '
                         f'{what}. {escape(last.get("why", ""))}</p>')
            if last.get("notes"):
                parts.append('<p class="hint">Code held it back: ' + escape("; ".join(last["notes"])) + "</p>")
        last = last.get("ideas") or {}
        if last:
            parts.append('<p class="hint">The turn before that looked across the desk:</p>')
    if last.get("failed"):
        parts.append(f'<p class="muted">Last turn {_when(last["at"])} failed: {escape(last["failed"])}</p>')
    elif last:
        parts.append(f'<p class="muted">Last turn {_when(last["at"])}. {escape(last.get("why", ""))}</p>')
        if last.get("patterns"):
            parts.append("<p>Patterns it saw:</p><ul>" + "".join(f"<li>{escape(p)}</li>" for p in last["patterns"])
                         + "</ul>")
        made = [f"proposed the epic <strong>{escape(t)}</strong> (waiting for you below)" for t in last.get("epics", [])]
        m = store.get_mission(last["mission"]) if last.get("mission") else None
        if m:
            made.append(f'proposed the mission <a href="/m/{quote(m.slug)}">{escape(m.topic)}</a>')
        c = store.get_combine(last["combine"]) if last.get("combine") else None
        if c:
            made.append(f"proposed combining {len(c.members)} missions into <strong>{escape(c.topic)}</strong>"
                        + (" (waiting for you below)" if c.status == "proposed" else f" ({c.status})"))
        for a, b in last.get("links", []):
            made.append(f'linked {mission_link(a)} and {mission_link(b)}')
        if made:
            parts.append(f"<p>It {', and '.join(made)}.</p>")
        if last.get("lookups"):
            parts.append("<p>It searched the library for:</p><ul>" + "".join(f"<li>{escape(q)}</li>"
                                                                            for q in last["lookups"]) + "</ul>")
        if last.get("notes"):
            parts.append('<p class="hint">Code held it back: ' + escape("; ".join(last["notes"])) + "</p>")
    if history:
        rows = "".join(f'<li><span class="hint">{_when(h.get("at", ""))}, '
                       f'{BY.get(h.get("by"), "the Theorist")}:</span> '
                       f'{escape(h.get("line") or "(cleared)")}</li>' for h in reversed(history))
        parts.append(f"<p>The line over time, newest first:</p><ul>{rows}</ul>")
    return f'<details><summary>{icon("bulb")}Theorist\'s notes</summary>{"".join(parts)}</details>'


def f916_status() -> str:
    """One line on the 1F916 channel: who we post as, how many wait, when the next post can go."""
    if not agents.f916_enabled():
        return ('1F916 is off. Add it under Channels on the <a href="/settings#publishing">Settings</a> page, '
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
        return ('Bluesky is off. Add it under Channels on the <a href="/settings#publishing">Settings</a> page, '
                "or set <code>BLUESKY_HANDLE</code> and <code>BLUESKY_APP_PASSWORD</code> in .env and run "
                "<code>python run.py check-bluesky</code>.")
    queued, problem = len(orchestrator.queue(bluesky)), bluesky.load_state().get("note", "")
    server = "" if bluesky._service() == settings.BLUESKY_DEFAULT else f" on {escape(bluesky._service())}"
    line = (f"Bluesky: posting as <strong>{escape(bluesky.handle())}</strong>{server}, at most one article every 10 minutes, "
            f"newest first · {queued} queued")
    if not settings.site_url():
        line += ('<br><span class="hint">No hosted address for the site, so posts link to the 1F916 copy when there '
                 "is one and otherwise carry no link.</span>")
    return line + (f'<br><span class="hint">Last try failed: {escape(problem)}</span>' if problem else "")

def _link(url: str, text: str = "") -> str:
    return f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener">{escape(text or url)}</a>'

def github_status() -> str:
    """One line on the GitHub Pages channel: which repo, where the site is, the last push or failure."""
    if not agents.github_enabled():
        return ('GitHub Pages is off. Connect GitHub on the <a href="/settings#publishing">Settings</a> page to put the site '
                "online, so Bluesky posts link to each article.")
    state, repo = github.load_state(), github.repo()
    line = (f"GitHub Pages: {_link(github.site_address())} from "
            f"{_link(settings.github_url() + '/' + repo, repo)} · {len(orchestrator.queue(github))} queued")
    if state.get("at"):
        line += f" · last push {escape(state['at'][:16].replace('T', ' '))}"
    if state.get("pages_note"):
        line += f'<br><span class="hint">To do: {escape(state["pages_note"])}</span>'
    if state.get("note"):
        line += f'<br><span class="hint">Last push failed: {escape(state["note"])}</span>'
    return line
def custom_status(c) -> str:
    """One line on a custom channel: what it sends where, and how many wait."""
    cfg = c.cfg
    where = (f"POSTs to <code>{escape(urlparse(cfg['target']).netloc)}</code>" if cfg["kind"] == "webhook"
             else f"writes files into <code>{escape(cfg['target'])}</code>")
    fmt = settings.CUSTOM_FORMATS.get(cfg["format"], cfg["format"])
    return f"{escape(c.NAME)}: {where} as {escape(fmt)}, newest first · {len(orchestrator.queue(c))} queued"

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
            bits.append(f"{'written' if key == 'site' else 'posted'} {post.at:%b %-d, %Y %H:%M} UTC · {link}")
        if key in m.publish_requests:
            bits.append(f'<strong>publishing requested {m.publish_requests[key]:%b %-d %H:%M} UTC</strong>, ahead of '
                        "everything else")
        elif not post and key in queues:
            slugs = queues[key]
            bits.append(f"waiting, #{slugs.index(m.slug) + 1} of {len(slugs)} in the queue" if m.slug in slugs
                        else "waiting for the feed to carry its text")
        if m.channel_errors.get(key):
            bits.append(f"refused the last try: {escape(m.channel_errors[key])}")
        out.append({"key": key, "name": orchestrator.channel_name(key), "posted": bool(post),
                    "on": key == "site" or key in queues, "status": " · ".join(bits) or "not posted"})
    return out

def channel_names(m: Mission) -> str:
    """Where a published mission went, e.g. "Local site, 1F916"."""
    return ", ".join(orchestrator.channel_name(k) for k in orchestrator.channels(m))

def publish_form(m: Mission, rows: list[dict], back: str) -> str:
    """Publish a published article again: tick where. Places it hasn't reached yet are ticked already."""
    boxes = "".join(
        f'<label class="tick"><input type="checkbox" name="to" value="{escape(r["key"], quote=True)}"'
        f'{"" if r["posted"] and r["key"] != "site" else " checked"}> {escape(r["name"])}'
        f'<span class="hint">{" (rewrite the page)" if r["key"] == "site" else " (again)" if r["posted"] else ""}</span></label>'
        for r in rows if r["on"])
    return (f'<form method="post" action="/m/{quote(m.slug)}/republish" class="republish">'
            f'<input type="hidden" name="back" value="{escape(back, quote=True)}">{boxes}'
            f'<button class="primary">Publish</button></form>')

def places_html(m: Mission, rows: list[dict]) -> str:
    return "".join(f'<li><strong>{escape(r["name"])}</strong>: {r["status"]}</li>' for r in rows)

# ---------- settings: the two workers and the model providers ----------

WORKER_BLURBS = {
    "assistant": "Young, quick and tireless. Does the legwork: searching and reading pages, with the small model.",
    "pi": "Tenured, slow and thorough. Does the thinking and the writing, with the big model.",
}
ROLE_STEPS = {                             # where each worker's model is used; the rest of the pipeline is code
    "assistant": ["<strong>Source finder</strong>: searches the web and reads pages for each mission (up to 15 minutes)",
                  "<strong>Mission finder</strong>: its searches around your inspiration line",
                  "<strong>Contact finder</strong>: looks for people to interview (only while outreach is on)"],
    "pi": ["<strong>Theorist</strong>: looks across every epic, post and mission and evolves your inspiration line",
           "<strong>Brief writer</strong>: turns the gathered sources into the research brief",
           "<strong>Article writer</strong>: writes the article from the approved brief",
           "<strong>Mission finder</strong>: picks the new missions from what the searches found"],
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
            bits.append("loaded")
        groups.setdefault(m["provider"], []).append(
            f'<option value="{escape(m["ref"], quote=True)}"{" selected" if m["ref"] == current else ""}>'
            f'{" · ".join(bits)}</option>')
    opts = [f'<optgroup label="{escape(settings.PROVIDERS[pid]["name"], quote=True)}">{"".join(o)}</optgroup>'
            for pid, o in groups.items()]
    if not current:                        # nothing chosen on this machine yet: the code has no default
        opts.insert(0, '<option value="" selected disabled>Choose a model</option>')
    elif current not in {m["ref"] for m in models}:   # saved before, or its provider is closed: still show it
        pid, name = settings.split_model(current)
        if pid == settings.LOCAL:
            missing = "not pulled" if conn["ok"] else "Ollama not reachable"
        else:
            missing = (f"{settings.PROVIDERS[pid]['name']} not connected" if not settings.provider_on(pid)
                       else f"not offered by {settings.PROVIDERS[pid]['name']}")
        opts.insert(0, f'<option value="{escape(current, quote=True)}" selected>{escape(current)} ({missing})</option>')
    return "".join(opts)

def plural(worker: str) -> str:
    """"a Research Assistant" or "3 Research Assistants"."""
    n, name = settings.worker_count(worker), settings.WORKERS[worker]
    return f"{n} {name}s" if n > 1 else f"the {name}"

def count_stepper(worker: str) -> str:
    """How many of this worker run at once: − and + (assets: WORKER_COUNT_JS, which waits for the clicks to settle
    before saving) or a number typed in."""
    n, top, name = settings.worker_count(worker), settings.MAX_COUNT, settings.WORKERS[worker]
    hint = "at a time" if n == 1 else f"at a time, each on its own mission"
    return f"""<label for="{worker}_count">How many</label>
  <div class="stepper">
    <span class="steps"><button type="button" data-step="-1" aria-label="One fewer {name}"{" disabled" if n <= 1 else ""}>&minus;</button><input type="number" id="{worker}_count" name="count" value="{n}" min="1" max="{top}" step="1" inputmode="numeric" onchange="this.form.requestSubmit()"><button type="button" data-step="1" aria-label="One more {name}"{" disabled" if n >= top else ""}>+</button></span>
    <span class="hint">{hint}</span>
  </div>"""

def worker_card(worker: str, conn: dict, now: list[dict], models: list[dict] | None = None) -> str:
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
        busy = [e for e in now if settings.seat_worker(e["worker"]) == worker]
        many = settings.worker_count(worker) > 1
        note = "".join(f'<p class="note">Now{" (" + escape(settings.seat_name(e["worker"])) + ")" if many else ""}: '
                       f'{doing(e)}</p>' for e in busy)
    checked = " checked" if on else ""
    crew = (f'<div class="crew-wrap"><div class="crew" data-crew="{worker}" aria-hidden="true"></div>'
            f'<a class="crew-edit" href="/avatars">{icon("pencil")}Customize</a></div>' if settings.animations() else "")
    return f"""<form method="post" action="/settings/worker" class="worker {"on" if on else "off"}">
  <input type="hidden" name="worker" value="{worker}">
  <div class="head"><h3>{name}</h3>
    <label class="switch"><input type="checkbox" role="switch" name="on" value="on"{checked} onchange="this.form.requestSubmit()" aria-label="{name} on"></label></div>
  <p class="tagline hint">{WORKER_BLURBS[worker]}</p>
  <label for="{worker}_model">Model</label>
  <select id="{worker}_model" name="model" onchange="this.form.requestSubmit()">{model_options(model, conn, models)}</select>
  {count_stepper(worker)}
  <label>Uses this model for</label>
  <ul>{steps}</ul>
  {note}<div class="grow"></div>
  <noscript><p><button>Save</button></p></noscript>
  {crew}
</form>"""

def workers_section(conn: dict, models: list[dict] | None = None) -> str:
    now = running_now()
    a, p = (f"<code>{escape(settings.worker_model(w) or 'no model yet')}</code>" for w in settings.WORKERS)
    if settings.parallel():
        ra, pi = plural("assistant"), plural("pi")
        lead = (f"Both workers are on, so they work at the same time whenever each has a task: {ra} "
                f"search{'' if ra[0].isdigit() else 'es'} with {a} while {pi} write{'' if pi[0].isdigit() else 's'} "
                f"with {p}.")
    else:
        w = next(w for w in settings.WORKERS if settings.worker_on(w))
        n = settings.worker_count(w)
        pace = "one task at a time" if n == 1 else f"{n} tasks at a time"
        lead = (f"Only the {settings.WORKERS[w]} is on: it does every step with "
                f"<code>{escape(settings.worker_model(w) or 'no model yet')}</code>, {pace}.")
    if settings.missing_models():          # the code has no models of its own: this machine picks them
        lead = ("<strong>Choose a model for each worker that is on; the loop won't start until you do.</strong> "
                "Both on: they work at the same time whenever each has a task.")
    else:
        lead += " Models are this machine's choice, saved in <code>data/settings.json</code>; nothing is built in."
    models = models if models is not None else llm.catalog(conn)
    cards = "".join(worker_card(w, conn, now, models) for w in settings.WORKERS)
    return (f'{heading("people", "Workers", anchor="workers")}<p>{lead} The source checker, article checker and publisher are '
            f'code and use no model.</p><div class="workers">{cards}</div>{WORKER_COUNT_JS}')

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
    source = ("from OLLAMA_HOST in .env" if env and not saved else
              "saved on this page" if saved else "the standard local address")
    return f"""<form method="post" action="/settings/ollama" class="inline">
  <label for="ollama_host" class="sr">Ollama address</label>
  <input type="text" id="ollama_host" name="ollama_host" maxlength="300" value="{escape(saved, quote=True)}" placeholder="{escape(env or settings.OLLAMA_DEFAULT, quote=True)}">
  <button>Save address</button><span class="hint">now {source}; empty goes back to .env or {settings.OLLAMA_DEFAULT}</span>
</form>"""

def model_users() -> dict[str, list[str]]:
    """Model ref -> the workers that are on and use it."""
    users: dict[str, list[str]] = {}
    for w in settings.WORKERS:
        if settings.worker_on(w):
            users.setdefault(settings.worker_model(w), []).append(settings.WORKERS[w])
    return users

def ollama_card(conn: dict) -> str:
    host = escape(conn["host"])
    connect = """<form method="post" action="/settings/ollama-connect" class="actions"><button class="primary">Connect Ollama</button>
  <span class="hint">checks that Ollama answers and gives each worker without a model one of its models: the biggest to the
  Principal Investigator, the smallest to the Research Assistant</span></form>"""
    if not conn["ok"]:
        return (f'<div class="pubbox channel"><div class="head"><h3>Ollama</h3><span class="badge">On this machine</span>'
                f'</div><p>Not reachable at <code>{host}</code> ({escape(conn["error"])}). Install it from '
                f'<a href="https://ollama.com/download" target="_blank" rel="noopener">ollama.com</a>, open the app and '
                f'pull a model or two (<code>ollama pull &lt;model&gt;</code>), then connect. Running on another machine? '
                f'Put its address below.</p>{connect}{ollama_form()}</div>')
    users = model_users()
    rows = "".join(
        f'<tr><td><code>{escape(m["name"])}</code></td><td class="size">{gb(m["size"])}</td>'
        f'<td class="size">{"in memory, " + gb(conn["loaded"][m["name"]]) if m["name"] in conn["loaded"] else ""}</td>'
        f'<td>{", ".join(users.get(m["name"], []))}</td></tr>' for m in conn["models"])
    count = len(conn["models"])
    table = (f'<table class="events models"><tr><td class="when">Model</td><td class="when">On disk</td>'
             f'<td class="when">Loaded</td><td class="when">Used by</td></tr>{rows}</table>' if rows else "")
    lead = (f"Connected at <code>{host}</code> · {count} model{'' if count == 1 else 's'}. "
            "<code>ollama pull &lt;model&gt;</code> adds more.")
    return (f'<div class="pubbox channel"><div class="head"><h3>Ollama</h3><span class="badge">On this machine</span>'
            f'</div><p>{lead}</p>{connect if settings.missing_models() else ""}{ollama_form()}{table}</div>')

def cloud_card(pid: str) -> str:
    spec = settings.PROVIDERS[pid]
    names, error = providers.models(pid)
    users = model_users()
    used = sorted({w for ref, ws in users.items() if settings.split_model(ref)[0] == pid for w in ws})
    if error:
        status = f"Connected, but not answering just now: {escape(error)}"
    else:
        status = f"Connected · {len(names)} model{'' if len(names) == 1 else 's'}"
    if pid == "custom":
        status += f" at <code>{escape(settings.provider_url(pid))}</code>"
    if used:
        status += f" · used by the {' and the '.join(used)}"
    if settings.provider_saved(pid):
        remove = (f'<form method="post" action="/settings/provider"><input type="hidden" name="do" value="forget">'
                  f'<input type="hidden" name="provider" value="{pid}"><button>Disconnect {escape(spec["name"])}</button></form>')
    else:
        remove = f'<p class="hint">Set in .env; remove {escape(spec["key_env"])} there to turn it off.</p>'
    return (f'<div class="pubbox channel"><div class="head"><h3>{escape(spec["name"])}</h3><span class="badge">Cloud</span>'
            f'</div><p>{status}. Its models are in each worker\'s list.</p>{remove}</div>')

def add_provider_forms() -> str:
    out = []
    for pid in settings.PROVIDERS:
        if pid == settings.LOCAL or settings.provider_on(pid):
            continue
        spec = settings.PROVIDERS[pid]
        get_key = (f' Get a key at <a href="{escape(spec["keys"], quote=True)}" target="_blank" rel="noopener">'
                   f'{escape(spec["keys"].split("//")[-1].split("/")[0])}</a>.' if spec["keys"] else "")
        env = f' Or put it in <code>.env</code> as <code>{escape(spec["key_env"])}</code>.' if spec["key_env"] else ""
        url = ""
        if pid == "custom":
            url = f"""<label for="url_{pid}">Address <span class="hint">the API's base, ending before /chat/completions, e.g. http://127.0.0.1:1234/v1</span></label>
  <input type="text" id="url_{pid}" name="url" required maxlength="300" placeholder="http://127.0.0.1:1234/v1">"""
        key_label = "API key" + (' <span class="hint">optional: only if the server asks for one</span>'
                                  if spec.get("optional_key") else "")
        required = "" if spec.get("optional_key") else " required"
        out.append(f"""<details class="pubbox add"><summary><strong>{escape(spec["name"])}</strong> · {escape(spec["blurb"])}</summary>
<p>Pay as you go on your own account: each brief, article and search turn is billed by {escape(spec["name"])}.{get_key}
The key is checked with {escape(spec["name"])}, then saved to <code>data/providers.json</code> on this machine only.{env}</p>
<form method="post" action="/settings/provider">
  <input type="hidden" name="do" value="connect"><input type="hidden" name="provider" value="{pid}">
  {url}
  <label for="key_{pid}">{key_label}</label>
  <input type="password" id="key_{pid}" name="key"{required} maxlength="500" autocomplete="off">
  <p><button>Check and connect</button></p>
</form></details>""")
    return "".join(out)

def connection_section(conn: dict) -> str:
    cards = ollama_card(conn) + "".join(cloud_card(p) for p in settings.cloud_providers())
    return (f'{heading("plug", "Model providers", anchor="providers")}<p>The workers can use any model from the '
            "providers connected here, and each worker can use a different one. Ollama runs free on this machine; a "
            "cloud provider needs only its API key. Keys stay on this machine, in <code>data/providers.json</code> "
            "or <code>.env</code>, and are never committed.</p>"
            f'{cards}<h3 class="sub">Add a provider</h3>{add_provider_forms()}')

EMBEDDY = re.compile(r"embed|bge|minilm|e5-|gte|arctic|mxbai|nomic", re.I)   # an Ollama embedding model's name

def library_section(conn: dict) -> str:
    """The desk's library (library.py): which provider and model embed its passages, and how many a search returns."""
    current, env = settings.embed_model(), settings._env(settings.EMBED_ENV)
    pid, name = settings.split_model(current) if current else ("none", "")
    choices = [("none", "No embedding model: rank by words only")] + [
        (p, settings.PROVIDERS[p]["name"]) for p in settings.embed_providers()]
    if pid not in dict(choices):           # set to a provider since disconnected: keep it visible
        choices.append((pid, settings.PROVIDERS.get(pid, {}).get("name", pid) + " (not connected)"))
    opts = "".join(f'<option value="{p}"{" selected" if p == pid else ""}>{escape(label)}</option>' for p, label in choices)
    names = [(m["name"], "Ollama") for m in conn["models"] if EMBEDDY.search(m["name"])]
    for p in settings.embed_providers()[1:]:
        names += [(n, settings.PROVIDERS[p]["name"]) for n in providers.models(p, embed=True)[0]]
    suggestions = "".join(f'<option value="{escape(n, quote=True)}" label="{escape(label, quote=True)}">'
                          for n, label in names)
    tops = "".join(f'<option value="{n}"{" selected" if n == settings.library_top_n() else ""}>{n}</option>'
                   for n in settings.TOP_N_CHOICES)
    source = (" (from MISSION_LOOP_EMBED_MODEL in .env)" if env and current == env and settings.saved().get("embed_model") in (None, "")
              else "")
    now = f" Embedding model: <code>{escape(current)}</code>{source}." if current else ""
    return f"""{heading("book", "Library", anchor="library")}
<p>The Theorist and Researcher Chat search the desk's library before they reason: every mission's objective, each
brief's claims with the quotes they cite, counterpoints and sources, and every article section. It always ranks by
words; an embedding model adds ranking by meaning. It keeps itself up to date and lives in <code>data/library.db</code>
on this machine.</p>
<div class="pubbox channel"><div class="head"><h3>Index</h3><span class="badge">On this machine</span></div>
<p>{escape(library.summary())}.{now}</p>
<form method="post" action="/settings/library" class="inline"><input type="hidden" name="do" value="index">
  <button>Index now</button><span class="hint">embeds every new passage now instead of a minute's worth per search</span></form></div>
<form method="post" action="/settings/library">
  <input type="hidden" name="do" value="save">
  <label for="embed_provider">Embedding provider <span class="hint">Ollama, or a connected provider with embeddings (Anthropic, DeepSeek and Groq have none)</span></label>
  <select id="embed_provider" name="provider">{opts}</select>
  <label for="embed_model">Embedding model <span class="hint">e.g. an Ollama embedding model you pulled, or the provider's own; checked before it is saved</span></label>
  <input type="text" id="embed_model" name="model" maxlength="200" list="embed_models" value="{escape(name, quote=True)}" autocomplete="off">
  <datalist id="embed_models">{suggestions}</datalist>
  <label for="top_n">Passages per search (top N) <span class="hint">more gives the agents more to reason from and costs more of their context</span></label>
  <select id="top_n" name="top_n">{tops}</select>
  <p><button>Save</button></p>
</form>"""

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
    name = settings.PROVIDERS[pid]["name"]
    if do == "forget":
        settings.forget_provider(pid)
        providers.forget_models(pid)
        return go("/settings", done="provider_off")
    key, url = str(form.get("key") or "").strip()[:500], str(form.get("url") or "").strip()[:300]
    try:
        url = settings._http_address(url) if url else ""
        if not key and not settings.PROVIDERS[pid].get("optional_key"):
            raise ValueError("paste the API key")
        if any(c.isspace() for c in key):
            raise ValueError("that doesn't look like an API key")
        if not url and not settings.PROVIDERS[pid]["url"]:
            raise ValueError("say where the server is")
        found = await run_in_threadpool(providers.list_models, pid, key, url or settings.PROVIDERS[pid]["url"])
        settings.save_provider(pid, key, url)
    except (ValueError, providers.ProviderError) as e:
        return page("Settings", settings_body(request, f"{name} was not connected: {e}"))
    providers.forget_models(pid)
    if not found:
        return page("Settings", settings_body(request, f"{name} is connected, but offered no chat models to this key."))
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
        return "<p>GitHub Pages is off while MISSION_LOOP_STUBS=1, so a test run never publishes for real.</p>"
    gh = escape(settings.github_url(), quote=True)
    if github.enabled():
        return f"""<p>{github_status()}</p>
<p class="hint">Each time the loop publishes, whatever changed in <code>site/</code> is pushed to the repo as one commit.
The repo holds the site and nothing else.</p>
<form method="post" action="/settings/github" class="actions inline-buttons">
  <button name="do" value="unlink">Use a different repo</button> <button name="do" value="forget">Disconnect GitHub</button>
</form>"""
    if github.connected():
        name = escape(github.suggested_name(), quote=True)
        return f"""<p>Connected as <strong>{escape(github.login_name())}</strong>. Last step: the repo the site goes to.
Everything in it is replaced by the site, so use a new or empty public repo.</p>
<form method="post" action="/settings/github" class="actions">
  <input type="hidden" name="do" value="create">
  <label for="gh_name" style="margin-top:0">Create my site <span class="hint">a new public repo; <code>{escape(github.suggested_name())}</code> is served at {escape(github.pages_address(github.login_name() + "/" + github.suggested_name()))}</span></label>
  <input type="text" id="gh_name" name="name" maxlength="100" value="{name}">
  <p><button>Create the repo and publish</button></p>
</form>
<form method="post" action="/settings/github" class="actions">
  <input type="hidden" name="do" value="link">
  <label for="gh_repo">Or use a repo you already made <span class="hint">paste its link, e.g. {gh}/{escape(github.login_name())}/my-site</span></label>
  <input type="text" id="gh_repo" name="repo" maxlength="300" required placeholder="{gh}/{escape(github.login_name())}/my-site">
  <p><button>Link this repo and publish</button></p>
</form>
<form method="post" action="/settings/github" class="actions"><input type="hidden" name="do" value="forget">
  <p><button>Disconnect GitHub</button></p></form>"""
    if GITHUB_LOGIN:
        code, where = escape(GITHUB_LOGIN["user_code"]), escape(GITHUB_LOGIN["verification_uri"], quote=True)
        return f"""<div class="gh-code" data-interval="{GITHUB_LOGIN['interval']}">
<p>Open <a href="{where}" target="_blank" rel="noopener">{where}</a>, sign in, enter this code and approve:</p>
<p class="code-big"><code>{code}</code> <button type="button" class="copy" data-code="{code}">Copy</button></p>
<p class="hint gh-wait">Waiting for GitHub… this page carries on by itself once you approve.</p>
</div>
<form method="post" action="/settings/github" class="actions"><input type="hidden" name="do" value="cancel">
  <p><button>Cancel</button></p></form>
<script>
(() => {{
  const box = document.querySelector(".gh-code"), wait = box.querySelector(".gh-wait");
  let every = +box.dataset.interval * 1000;
  box.querySelector(".copy").onclick = e => navigator.clipboard?.writeText(e.target.dataset.code);
  const poll = async () => {{
    try {{
      const r = await (await fetch("/settings/github/poll", {{method: "POST"}})).json();
      if (r.state === "done") return location.assign("/settings?done=github_connected");
      if (r.state === "error") {{ wait.textContent = r.message + " Cancel and connect again."; return; }}
      if (r.state === "slow_down") every += 5000;
    }} catch (e) {{}}
    setTimeout(poll, every);
  }};
  setTimeout(poll, every);
}})();
</script>"""
    if not settings.github_client_id():
        return f"""<p>Off. This copy of mission-loop has no GitHub OAuth app to sign in with yet. Register one once
(on GitHub: Settings &gt; Developer settings &gt; OAuth Apps &gt; New OAuth App, or <a href="{gh}/settings/applications/new" target="_blank" rel="noopener">here</a>):
any name, homepage and callback URL (e.g. http://127.0.0.1:8770), then tick <strong>Enable Device Flow</strong>
and paste its client ID below. A client ID isn't a secret; there is no client secret to make.</p>
<form method="post" action="/settings/github" class="actions">
  <input type="hidden" name="do" value="client_id">
  <label for="gh_client" style="margin-top:0">OAuth app client ID</label>
  <input type="text" id="gh_client" name="client_id" maxlength="64" required placeholder="Ov23li…">
  <p><button>Save</button></p>
</form>"""
    return f"""<p>Off. Puts the site online for free with GitHub Pages, so Bluesky posts link to the full article,
and keeps it up to date each time something is published. Connecting signs you in on GitHub itself; no password or
token is typed here. The app can then reach your public repos only, and its token is saved to
<code>data/github.json</code> on this machine.</p>
<form method="post" action="/settings/github" class="actions"><input type="hidden" name="do" value="connect">
  <p><button>Connect GitHub</button></p></form>"""

STUB_RUN = "Off while MISSION_LOOP_STUBS=1, so a test run never posts for real."

def local_site_box() -> str:
    """The foundation: the static site in site/ on this machine, which every channel posts from."""
    url, env = settings.site_url(), settings.SITE_URL
    count = len(publisher.entries())
    feeds = ('<a href="/site/feed.xml" target="_blank">site/feed.xml</a> (RSS) · '
             '<a href="/site/feed.json" target="_blank">site/feed.json</a> (JSON Feed)')
    hosted = escape(url.rstrip("/") + "/")
    links = (f"Links in the feeds are full addresses under {hosted}; once the site is there, its feeds are "
             f"{hosted}feed.xml and {hosted}feed.json." if url else
             "No hosted address yet, so links in the feeds are relative to the feed file. "
             "That works wherever the site folder is served.")
    source = (' <span class="hint">(from MISSION_LOOP_SITE_URL in .env; saving here overrides it)</span>'
              if env and not settings.saved().get("site_url") else
              ' <span class="hint">(the GitHub Pages address until you type one)</span>'
              if url and not settings.saved().get("site_url") else "")
    return f"""<div class="pubbox">
<div class="head"><h3>Local site</h3><span class="badge">Foundation · always on</span></div>
<p>Every published article is written here first, as files on this machine: a page per article in <code>site/</code>,
an index page and two feeds. It is the record of what is published, and every channel posts from it.</p>
<p>{count} article{"" if count == 1 else "s"} · {feeds} · <a href="/published">See what's published</a></p>
<form method="post" action="/settings/site-url">
  <label for="site_url">Hosted address{source} <span class="hint">optional: where a copy of <code>site/</code> is served,
  e.g. https://example.com/news/. Feed links and Bluesky link cards use it. Empty: the GitHub Pages address once that
  channel is set up, else none while the site is only on this machine.</span></label>
  <input type="text" id="site_url" name="site_url" maxlength="300" value="{escape(settings.saved().get("site_url", ""), quote=True)}" placeholder="{escape(url or "https://example.com/news/", quote=True)}">
  <p class="hint">{links}</p>
  <p><button>Save hosted address</button></p>
</form></div>"""

def channel_card(c) -> str:
    """One channel that is set up: what it is, how it is doing, and how to take it away."""
    if c.KEY == f916.KEY:
        kind, remove = "Built in", ('<p class="hint">Joined for good: <code>data/1f916.json</code> is the citizen\'s '
                                    "whole identity, with no recovery, so it can't be removed here.</p>")
    elif c.KEY == github.KEY:               # its status and its unlink / disconnect buttons
        return (f'<div class="pubbox channel"><div class="head"><h3>{escape(c.NAME)}</h3><span class="badge">Built in</span>'
                f'</div>{github_row()}</div>')
    elif c.KEY == bluesky.KEY:
        kind = "Built in"
        remove = ("""<form method="post" action="/settings/bluesky"><input type="hidden" name="forget" value="1">
  <button>Disconnect Bluesky</button></form>""" if bluesky.saved_credentials() else
                  '<p class="hint">Set in .env; remove BLUESKY_HANDLE and BLUESKY_APP_PASSWORD there to turn it off.</p>')
    else:
        kind = "Custom · " + settings.CUSTOM_KINDS.get(c.cfg["kind"], c.cfg["kind"])
        remove = (f'<form method="post" action="/settings/custom-channel/remove">'
                  f'<input type="hidden" name="id" value="{escape(c.cfg["id"], quote=True)}">'
                  f'<button>Remove channel</button></form>')
    return (f'<div class="pubbox channel"><div class="head"><h3>{escape(c.NAME)}</h3><span class="badge">{escape(kind)}</span>'
            f'</div><p>{channel_status(c)}</p>{remove}</div>')

def add_channel_forms() -> str:
    """Ways to add a channel: the built-in ones not set up yet, and a custom one."""
    out = []
    if not agents.f916_enabled():
        body = (f"<p>{STUB_RUN}</p>" if os.environ.get("MISSION_LOOP_STUBS") == "1" else f"""<p>Posts each article's full text,
one a day, newest first. Joining makes a permanent, public citizen on <a href="{escape(settings.f916_url(), quote=True)}" target="_blank" rel="noopener">{escape(settings.f916_url())}</a>.
Its secret key is saved to <code>data/1f916.json</code> on this machine only; there is no recovery if it is lost.</p>
<form method="post" action="/settings/join-1f916">
  <label for="handle">Handle <span class="hint">2 to 32 letters, digits, _ or -; it can't be changed later</span></label>
  <input type="text" id="handle" name="handle" required maxlength="32" pattern="[A-Za-z0-9_-]{{2,32}}" placeholder="e.g. loop-desk">
  <label for="f916_url">Server <span class="hint">the 1F916 to join; the key only works there, so it is saved with the key</span></label>
  <input type="text" id="f916_url" name="f916_url" maxlength="300" value="{escape(settings.f916_url(), quote=True)}">
  <p><button>Join 1F916</button></p>
</form>""")
        out.append(f'<details class="pubbox add"><summary><strong>1F916</strong> · built in</summary>{body}</details>')
    if not agents.github_enabled():         # connect, then pick the repo: open while a step is under way
        under_way = " open" if (GITHUB_LOGIN or github.connected()) and os.environ.get("MISSION_LOOP_STUBS") != "1" else ""
        out.append(f'<details class="pubbox add"{under_way}><summary><strong>GitHub Pages</strong> · built in</summary>'
                   f'{github_row()}</details>')
    if not agents.bluesky_enabled():
        body = (f"<p>{STUB_RUN}</p>" if os.environ.get("MISSION_LOOP_STUBS") == "1" else f"""<p>Announces each article with a
3-post thread (its title, boldest possibility and hardest fact), every post linking to the full text, at most
one article every 10 minutes. Use an app password from Bluesky's
Settings &gt; Privacy and security &gt; App passwords, never your account password. It is checked with Bluesky, then
saved to <code>data/bluesky.json</code> on this machine only.</p>
<form method="post" action="/settings/bluesky">
  <label for="bsky_handle">Handle <span class="hint">e.g. yourname.bsky.social</span></label>
  <input type="text" id="bsky_handle" name="handle" required maxlength="253" autocomplete="username" placeholder="yourname.bsky.social">
  <label for="bsky_password">App password</label>
  <input type="password" id="bsky_password" name="app_password" required maxlength="64" autocomplete="off" placeholder="xxxx-xxxx-xxxx-xxxx">
  <label for="bsky_service">Server <span class="hint">optional: only for an account on another Bluesky server</span></label>
  <input type="text" id="bsky_service" name="service" maxlength="300" placeholder="{escape(settings.bluesky_service(), quote=True)}">
  <p><button>Check and turn on Bluesky</button></p>
</form>""")
        out.append(f'<details class="pubbox add"><summary><strong>Bluesky</strong> · built in</summary>{body}</details>')
    kinds = "".join(f'<option value="{k}">{escape(v)}</option>' for k, v in settings.CUSTOM_KINDS.items())
    formats = "".join(f'<option value="{k}">{escape(v)}</option>' for k, v in settings.CUSTOM_FORMATS.items())
    out.append(f"""<details class="pubbox add"><summary><strong>Custom channel</strong> · anything else</summary>
<p>Sends every published article somewhere you choose, in a format you choose, newest first and once each, like the
other channels. Point a webhook at a service (a chat room, an automation tool, your own server), or have it drop a
file per article into a folder another tool picks up (a clone of a site repository, a synced folder).</p>
<form method="post" action="/settings/custom-channel">
  <label for="cc_name">Name <span class="hint">what the Published page calls it</span></label>
  <input type="text" id="cc_name" name="name" required maxlength="40" placeholder="e.g. Team chat">
  <label for="cc_kind">Send by</label>
  <select id="cc_kind" name="kind">{kinds}</select>
  <label for="cc_target">Where <span class="hint">a webhook's full http(s) address, or a folder's full path on this machine</span></label>
  <input type="text" id="cc_target" name="target" required maxlength="500" placeholder="https://hooks.example.com/abc or /Users/me/outbox">
  <label for="cc_format">Format</label>
  <select id="cc_format" name="format">{formats}</select>
  <details class="formats"><summary>What each format looks like</summary>
  <p><strong>JSON</strong>: one JSON Feed 1.1 item, the same as the article's entry in <code>site/feed.json</code>:
  <code>id</code> (the short name), <code>url</code>, <code>title</code>, <code>summary</code> (the dek),
  <code>content_text</code> (the full article with its sources), <code>date_published</code> and, in an epic,
  <code>tags</code>. Sent as <code>application/json</code>; a folder gets <code>&lt;short-name&gt;.json</code>.</p>
  <p><strong>Markdown</strong>: <code># Title</code>, the dek in italics, the link to the hosted site when there is
  one, then the full text. A folder gets <code>&lt;short-name&gt;.md</code>.</p>
  <p><strong>Plain text</strong>: the same without the markup, as <code>&lt;short-name&gt;.txt</code>.</p>
  <p>A webhook counts as posted on any 2xx reply; a JSON reply with a <code>url</code> is shown as where the article
  can be read. 429 means try later, other 4xx refuse that article, and anything else is tried again next pass.</p>
  </details>
  <p><button>Add custom channel</button></p>
</form></details>""")
    return "".join(out)

def publishing_section() -> str:
    live = live_channels()
    if os.environ.get("MISSION_LOOP_STUBS") == "1":
        cards = f'<p class="muted">{STUB_RUN}</p>'
    else:
        cards = "".join(channel_card(c) for c in live) or \
            '<p class="muted">No channels yet, so articles stay on the local site. Add one below.</p>'
    return heading("broadcast", "Publishing", anchor="publishing") + f"""
<p>Publishing has two parts: the <strong>local site</strong>, the foundation every article is written to first, and
<strong>channels</strong>, everywhere else the articles go. Each channel posts from the local site, newest first, and
remembers what it has posted. Publish any article again from the <a href="/published">Published</a> page.</p>
{local_site_box()}
<h3 class="sub">Channels ({len(live)})</h3>
{cards}
<h3 class="sub">Add a channel</h3>
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
        f'onchange="this.form.requestSubmit()">{icon(ico)}{label}</label>' for v, ico, label in THEME_CHOICES)
    current = settings.chat_color()
    known = set(settings.CHAT_COLORS.values())
    def swatch(value: str, name: str, color: str) -> str:
        on = " checked" if value == current else ""
        ink = settings.ink_on(color) if color.startswith("#") else "#ffffff"
        return (f'<label class="swatch" style="--c:{color};--ink:{ink}"><input type="radio" name="choice" value="{value}"'
                f'{on} onchange="this.form.requestSubmit()"><span class="dab"></span>{escape(name)}</label>')
    swatches = swatch("", "Library red", "var(--accent)")
    swatches += "".join(swatch(c, n, c) for n, c in settings.CHAT_COLORS.items())
    mine = current if current and current not in known else ""
    own_on = " checked" if mine else ""
    own_style = f' style="--c:{mine};--ink:{settings.ink_on(mine)}"' if mine else ""
    swatches += (f'<label class="swatch own{" set" if mine else ""}"{own_style}>'
                 f'<input type="radio" name="choice" value="own"{own_on} onchange="this.form.requestSubmit()">'
                 f'<span class="dab"><input type="color" id="own-color" name="own" value="{mine or OWN_DEFAULT}" '
                 f'aria-label="Choose your own colour"></span>Your own</label>')
    return f"""<form method="post" action="/settings/theme" class="actions display">
  <h3>{icon("sun")}Appearance</h3>
  <p class="hint">System follows your computer's light or dark setting, and changes with it. The article previews here
  follow it too; the published site keeps its own switch for readers.</p>
  <div class="choice" role="radiogroup" aria-label="Appearance">{themes}</div>
  <noscript><p><button>Save</button></p></noscript>
</form>
<form method="post" action="/settings/chat-color" class="actions display">
  <h3>{icon("palette")}Chat colour</h3>
  <p class="hint">The ink for the chat bubble and your questions in the chat panel.</p>
  <div class="swatches" role="radiogroup" aria-label="Chat colour">{swatches}</div>
  <noscript><p><button>Save</button></p></noscript>
</form>{CHAT_COLOR_JS}"""

def settings_body(request: Request, error: str = "") -> str:
    err = f'<div class="flash err">{escape(error)}</div>' if error else flash(request)
    checked = " checked" if settings.animations() else ""
    conn = llm.connection()
    models = llm.catalog(conn)
    sprites = RESEARCHERS if settings.animations() else ""
    return f"""{err}<h1>Settings</h1>
{setup_section(conn)}
{workers_section(conn, models)}
{connection_section(conn)}
{search_section()}
{library_section(conn)}
{heading("clock", "Loop")}
<form method="post" action="/settings/loop-every" class="inline">
  <label for="every" style="margin:0">When it has nothing to do, the loop rests</label>
  <input type="number" id="every" name="every" value="{settings.loop_every():g}" min="1" step="1" style="width:5rem">
  <span class="hint">minutes before looking again (a loop already running keeps its own until you start it again)</span>
  <button>Save</button>
</form>
{heading("monitor", "Display")}
{display_forms()}
{board_style_form()}
<form method="post" action="/settings/animations" class="actions">
  <label class="switch"><input type="checkbox" role="switch" name="animations" value="on"{checked} onchange="this.form.requestSubmit()">
  Animations <span class="hint">little pixel researchers who drop by the missions a model is working on, and stand by their worker here</span></label>
  <noscript><p><button>Save</button></p></noscript>
</form>
{avatar_link()}
{publication_section()}
{publishing_section()}
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

async def save_theme(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    try:
        settings.set_theme(str((await request.form()).get("theme") or ""))
    except ValueError:
        return go("/settings", error="theme")
    return go("/settings", done="theme_saved")

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
            raise ValueError("pick a provider")
        if pid != "none" and not model:
            raise ValueError("name the embedding model")
        ref = settings.model_ref(pid, model) if pid != "none" else ""
        if ref != settings.embed_model():                 # unchanged: store nothing, so .env keeps applying
            if ref:
                await run_in_threadpool(library.check, ref)
            settings.set_embed_model(ref or settings.WORDS_ONLY)
    except (ValueError, library.LibraryError) as e:
        return page("Settings", settings_body(request, f"The embedding model was not saved: {e}"))
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
        return page("Settings", settings_body(request, f"1F916 did not join: {e}"))
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
        return page("Settings", settings_body(request, f"Custom channel not added: {e}."))
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
        return page("Settings", settings_body(request, f"Bluesky did not accept that login: {e}"))
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
            return page("Settings", settings_body(request, f"GitHub did not start the sign-in: {e}"))
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
            return page("Settings", settings_body(request, f"The site wasn't linked: {e}"))
        orchestrator.refresh_feed()        # the feeds use the site's address from now on
        try:
            await run_in_threadpool(github.sync, clock.now())   # files only; the loop records each article's link
        except Exception as e:
            return page("Settings", settings_body(request, f"The site is linked, but the first push failed: {e}. "
                                                            "The loop tries again each time it posts."))
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

ASK_ACTION = {"review": "Read the brief", "publish": "Read the article", "parked": "See why it stopped"}

def ask(color: str, title: str, badge: str, body: str, actions: str, link: str = "") -> str:
    """A decision waiting on the human, the same for a brief, an article, a parked mission or a proposed epic:
    the finger, what it is, a colored badge, what to know, and the buttons that decide it."""
    name = f'<a class="topic" href="{link}">{escape(title)}</a>' if link else f'<span class="topic">{escape(title)}</span>'
    tag = f'<span class="badge {color}">{escape(badge)}</span>' if badge else ""
    return (f'<div class="ask {color}"><div class="ask-head">{icon("touch")}{name}{tag}</div>'
            f'{body}<div class="ask-actions">{actions}</div></div>')

def mission_ask(m: Mission, epics: dict[str, Epic]) -> str:
    color, href = tone(m), f"/m/{quote(m.slug)}"
    return ask(color, m.topic, label(m),
               f'<div class="sub">{mission_pill(m, epics)}{escape(m.slug)} · {escape(next_step(m))}</div>',
               f'<a class="btn primary" href="{href}">{ASK_ACTION[color]}</a>', href)

def epic_proposals(missions: list[Mission]) -> list[str]:
    """Proposed epics (the finder's or the Theorist's), each waiting for the human to approve, rename, redirect
    or reject it."""
    active = store.all_epics("active")
    theorist = {t.lower() for t in store.last_ideas().get("epics", [])}
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
        who = "The Theorist" if e.title.lower() in theorist else "The mission finder"
        out.append(ask(
            "propose", e.title, "New epic",
            f'<div class="sub">{who} proposes this epic for {len(ms)} missions. Edit the name before approving to '
            f'rename it; reject puts each mission back in the epic shown beside it.</div><ul>{items}</ul>',
            f'<form method="post" action="/epics/{slug}/approve">'
            f'<input type="text" name="title" value="{escape(e.title, quote=True)}" maxlength="48" aria-label="Epic name">'
            f'<button class="primary">Approve epic</button></form>{move}'
            f'<form method="post" action="/epics/{slug}/reject"><button>Reject</button></form>'))
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
        epic = f" It goes in the epic {escape(c.epic)}." if c.epic else ""
        why = f'<div class="sub">{escape(c.why)}</div>' if c.why else ""
        out.append(ask(
            "propose", c.topic, "Combine missions",
            f'<div class="sub">{"The chat assistant" if c.by == "chat" else "The Theorist"} proposes combining these {len(c.members)} missions into one bigger '
            f'mission. Approving queues it (its brief still comes to you) and stops these; any that reached writing '
            f'meanwhile carry on.{epic}</div>{why}<ul>{items}</ul>'
            f'<details><summary>Objective</summary><p>{escape(c.objective)}</p></details>',
            f'<form method="post" action="/combines/{quote(c.slug)}/approve"><button class="primary">Combine them</button></form>'
            f'<form method="post" action="/combines/{quote(c.slug)}/reject"><button>Keep them separate</button></form>'))
    return out

def connections(m: Mission) -> str:
    """The mission page's Connections: what it was combined from or into, and its links, each with why and a way
    to remove it, plus a form to link it to another mission by hand."""
    slug, rows = quote(m.slug), []
    if m.combined_into:
        rows.append(f"<li>Combined into {mission_link(m.combined_into)}, which carries its work on.</li>")
    if m.combines:
        rows.append("<li>Combines " + ", ".join(mission_link(s) for s in m.combines) + ".</li>")
    for k in store.links_of(m.slug):
        other = store.get_mission(k.other(m.slug))
        state = f' <span class="hint">· {escape(label(other))}</span>' if other else ""
        who = {"theorist": "the Theorist", "human": "you", "chat": "the chat assistant"}.get(k.by, k.by)
        why = f": {escape(k.why)}" if k.why else ""
        rows.append(f'<li class="link-row">Linked to {mission_link(k.other(m.slug))}{state} '
                    f'<span class="hint">by {escape(who)}</span>{why}'
                    f'<form method="post" action="/m/{slug}/unlink" class="inline">'
                    f'<input type="hidden" name="other" value="{escape(k.other(m.slug), quote=True)}">'
                    f'<button>Unlink</button></form></li>')
    form = ""
    if m.state != State.COMBINED:
        options = "".join(f'<option value="{escape(o.slug, quote=True)}">{escape(o.topic)}</option>'
                          for o in store.all_missions() if o.slug != m.slug and o.state != State.COMBINED
                          and (is_open(m) or is_open(o)) and not store.get_link(m.slug, o.slug))
        if options:
            form = (f'<details><summary>Link it to another mission</summary>'
                    f'<form method="post" action="/m/{slug}/link" class="actions">'
                    f'<label for="link-to" style="margin-top:0">Mission <span class="hint">an open mission, or a '
                    f'published article</span></label><select id="link-to" name="to">{options}</select>'
                    f'<label for="link-why">Why <span class="hint">the shared thread; research sees it</span></label>'
                    f'<input type="text" id="link-why" name="why" maxlength="300">'
                    f'<p><button>Link</button></p></form></details>')
    if not rows and not form:
        return ""
    body = f'<ul class="places">{"".join(rows)}</ul>' if rows else \
        '<p class="hint">Not connected to any other mission yet.</p>'
    return heading("link", "Connections", len(rows) or None, cls="sec") + body + form

def epic_list(missions: list[Mission]) -> str:
    active = store.all_epics("active")
    if not active:
        return ""
    rows = "".join(
        f'<form method="post" action="/epics/{quote(e.slug)}/rename" class="inline">'
        f'<input type="text" name="title" value="{escape(e.title, quote=True)}" maxlength="48" aria-label="Epic name">'
        f'<span class="hint">{sum(m.epic == e.slug for m in missions)} missions</span><button>Rename</button></form>'
        for e in active)
    return heading("layers", "Epics", len(active)) + f'<div class="epics">{rows}</div>'

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
        working, idle = "", f"Published {m.published_at:%b %-d, %Y} · {channel_names(m)}"
    sub = working or idle
    classes = " ".join(c for c in ("card", color, "active" if working else "") if c)
    live = next((e for e in now or [] if e["slug"] == m.slug), None) if lab else None
    stage = ""
    if live:                               # researchers only visit while a model works on it, mostly its worker's crew
        crew = settings.seat_worker(live["worker"])
        stage = ' data-lab="busy"' + (f' data-worker="{crew}"' if crew in settings.WORKERS else "")
    return (f'<a class="{classes}" href="/m/{quote(m.slug)}"{stage}><div class="row"><span class="topic">{escape(m.topic)}</span>'
            f'<span class="badge {color}">{escape(label(m))}</span></div>'
            f'<div class="sub">{mission_pill(m, epics)}{escape(m.slug)}<span class="doing" data-idle="{escape(idle, quote=True)}">'
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
    out = [("site", "Local site", site)] if site else []
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
            f'{count} article{"" if count == 1 else "s"} queued</span><span class="badge publish">Publishing</span></div>'
            f'<div class="sub">{places} · see the queue</div></a>')

def queue_section(queued: list) -> str:
    """The Published page's queue: each place, then its articles in the order they go out."""
    if not queued:
        return (heading("clock", "Queue", 0, "queue")
                + '<p class="muted">Nothing waiting. Every article has reached every place it is going.</p>')
    parts = []
    for key, name, entries in queued:
        items = "".join(
            f'<li><a href="{f"#a-{escape(slug)}" if key != "site" or asked else f"/m/{quote(slug)}"}">{escape(title)}</a>'
            + (" · <strong>requested</strong>" if asked else "") + "</li>"
            for slug, title, asked in entries)
        parts.append(f'<h3 class="stage"><span>{escape(name)}</span><span class="n">{len(entries)}</span></h3>'
                     f'<ol class="queue">{items}</ol>')
    total = len({slug for _, _, entries in queued for slug, _, _ in entries})
    return heading("clock", "Queue", total, "queue") + "".join(parts)

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
    return (f'<span class="mark ok" title="Done">{icon("check")}</span>' if ok
            else '<span class="mark todo" title="To do"></span>')

def _step(n: int, title: str, ok: bool, body: str) -> str:
    return (f'<li class="setup-step{" done" if ok else ""}"><div class="setup-head">{_mark(ok)}'
            f'<strong>{n}. {escape(title)}</strong></div><div class="setup-body">{body}</div></li>')

def _fixes(found: list) -> str:
    return "".join(f'<p class="hint">{escape(c.detail)} {escape(c.fix)}'
                   + (f' <a href="{escape(c.link)}" target="_blank" rel="noopener">Open</a>' if c.link.startswith("http") else
                      f' <a href="{escape(c.link)}">Open</a>' if c.link else "") + "</p>" for c in found if not c.ok)

def search_form(back: str) -> str:
    return (f'<form method="post" action="/settings/search" class="inline setup-form">'
            f'<input type="hidden" name="back" value="{back}">'
            f'<input type="password" name="key" autocomplete="off" placeholder="Brave Search API key" aria-label="Brave Search API key" required>'
            f'<button>Check and save</button></form>')

def setup_card(found: list | None = None) -> str:
    """The home page's first run: connect a model, web search, something to work on, start. Shown until every
    check passes or the human hides it; it never blocks anything (Settings → Setup brings it back)."""
    if settings.setup_dismissed():
        return ""
    conn = {"ok": True, "models": []} if doctor.stubs() else llm.connection()
    found = found if found is not None else doctor.checks(conn=conn)
    if doctor.done(found):
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
                     f'<button class="primary">Connect Ollama</button><span class="hint">gives each worker one of your '
                     f'{len(conn["models"])} installed models</span></form>')
        elif settings.missing_models() and not conn["ok"]:
            body += (f'<p><a class="btn" href="{doctor.OLLAMA_DOWNLOAD}" target="_blank" rel="noopener">Get Ollama</a> '
                     f'<span class="hint">free, runs on this computer; open it, pull a model, then come back</span></p>')
        body += '<p class="hint">Or use a cloud model (Claude, GPT, Gemini and more): <a href="/settings#providers">add its API key</a>.</p>'
    tool_checks = [c for c in found if c.key in ("search", "npx", "uvx")]
    tools_ok = all(c.ok for c in tool_checks)
    tools = (f'<p class="hint">Research can search the web and read pages.</p>' if tools_ok else
             '<p class="hint">Research searches the web with Brave Search (there is a free tier).</p>'
             + (search_form("/") + f'<p class="hint"><a href="{doctor.BRAVE_KEYS}" target="_blank" rel="noopener">Get a key</a>. '
                'It stays on this machine.</p>' if not by["search"].ok else "")
             + _fixes([c for c in tool_checks if c.key != "search"]))
    work_ok = by["work"].ok
    chips = "".join(f'<button type="button" class="chip" data-line="{escape(line, quote=True)}">{escape(line)}</button>'
                    for line in INSPIRATIONS)
    work = (f'<p class="hint">{escape(by["work"].detail)}</p>' if work_ok else
            '<p class="hint">Give the mission finder a line to explore, and it finds its own missions. Or add one yourself.</p>'
            f'<form method="post" action="/inspiration" class="inline setup-form">'
            f'<input type="text" id="setup-line" name="inspiration" maxlength="500" placeholder="A question worth digging into" aria-label="A line for the mission finder to explore">'
            f'<button>Save inspiration</button></form><div class="chips">{chips}</div>'
            f'<p><button type="button" class="btn" onclick="document.getElementById(\'add-mission\').click()">'
            f'{icon("plus")}Add a mission instead</button></p>')
    can = all(c.ok for c in found if c.blocks_loop)
    start = ('<p class="hint">Press play at the top of the page. While it runs, briefs and articles wait for your '
             'review below; the Auto switch approves briefs for you, never publishing.</p>' if can else
             '<p class="hint">Play works once a model is connected.</p>')
    steps = (_step(1, "Connect a model", models_ok, body) + _step(2, "Web search", tools_ok, tools)
             + _step(3, "Give it something to work on", work_ok, work) + _step(4, "Start the loop", loop_running(), start))
    left = sum(not ok for ok in (models_ok, tools_ok, work_ok))
    return (f'<section class="setup actions" id="setup" aria-label="Setup">'
            f'<div class="setup-top"><h2>{icon("compass")}Get started</h2>'
            f'<form method="post" action="/setup/dismiss"><button class="link" title="Hide setup; Settings → Setup brings it back">Hide</button></form></div>'
            f'<p class="hint">{left} step{"" if left == 1 else "s"} left. Nothing here is required to look around.</p>'
            f'<ol class="setup-steps">{steps}</ol>'
            f'<p class="setup-more">New here? <a href="/about" target="_blank">See how the desk works</a> · '
            f'<button type="button" class="link" data-ask="How do I get Mission Loop set up, and what do I still need?">'
            f'{icon("message")}Ask the Researcher Chat</button></p></section>{SETUP_JS}')

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
    found = doctor.checks(conn=conn)
    rows = "".join(f'<li class="{"ok" if c.ok else "todo"}">{_mark(c.ok)}<span><strong>{escape(c.title)}</strong>: '
                   f'{escape(c.detail)}' + (f' <span class="hint">{escape(c.fix)}</span>' if c.fix else "")
                   + (f' <a href="{escape(c.link)}"{" target=_blank rel=noopener" if c.link.startswith("http") else ""}>Open</a>'
                      if c.link and not c.ok else "") + "</span></li>" for c in found)
    waiting = doctor.waiting() if loop_running() else None
    note = (f'<p class="hint"><strong>The loop is waiting:</strong> {escape(waiting["reason"])}.</p>' if waiting else "")
    again = ('<form method="post" action="/setup/show" class="inline"><button>Show setup on the Missions page</button></form>'
             if settings.setup_dismissed() and not doctor.done(found) else "")
    summary = ("Everything is set up." if doctor.done(found) else
               "The loop can start; the rest helps it do its job." if all(c.ok for c in found if c.blocks_loop) else
               "The loop can't start until a model is connected.")
    return (f'{heading("compass", "Setup", anchor="setup")}<div class="actions"><p>{summary} The same checks: '
            f'<code>python run.py doctor</code>.</p>{note}<ul class="checks">{rows}</ul>{again}</div>')

def search_section() -> str:
    key = settings.search_key()
    if key and settings.search_key_saved():
        state = ('A Brave Search key is saved on this machine (<code>data/search.json</code>).'
                 '<form method="post" action="/settings/search" class="inline"><input type="hidden" name="remove" value="1">'
                 '<button>Remove key</button></form>')
    elif key:
        state = "A Brave Search key is set in <code>.env</code>. Paste one here to use it instead."
    else:
        state = (f'No key yet, so research can\'t search the web (it can still read pages). '
                 f'<a href="{doctor.BRAVE_KEYS}" target="_blank" rel="noopener">Get a free key from Brave</a>.')
    status = toolbox_status()
    return (f'{heading("search", "Web search", anchor="search")}<div class="actions"><p>{state}</p>'
            f'{search_form("/settings") if not settings.search_key_saved() else ""}{status}</div>')

def toolbox_status() -> str:
    from agents import toolbox
    found = toolbox.server_status(["search", "fetch"])
    bad = [f"{name}: {why}" for name, why in found.items() if why and "not installed" in why]
    return (f'<p class="hint">{escape("; ".join(bad))}. Install with <code>brew install node uv</code>.</p>' if bad else "")

def publication_section() -> str:
    name, tag, about_text = settings.publication_name(), settings.tagline(), settings.about_statement()
    return (f'{heading("newspaper", "Publication", anchor="publication")}'
            f'<form method="post" action="/settings/publication" class="actions">'
            f'<p class="hint">How the published site introduces itself: its pages, About page and feeds. '
            f'Saving redraws every published page.</p>'
            f'<label for="pub-name">Name</label><input type="text" id="pub-name" name="name" maxlength="{settings.NAME_MAX}" '
            f'value="{escape(name, quote=True)}" placeholder="{settings.PUBLICATION_DEFAULT}">'
            f'<label for="pub-tagline">Tagline <span class="hint">optional: one line under the name on the article list</span></label>'
            f'<input type="text" id="pub-tagline" name="tagline" maxlength="{settings.TAGLINE_MAX}" value="{escape(tag, quote=True)}">'
            f'<label for="pub-about">About statement <span class="hint">optional: your words at the top of the About page; '
            f'a blank line starts a new paragraph. Empty: the built-in description.</span></label>'
            f'<textarea id="pub-about" name="about" rows="5" maxlength="{settings.ABOUT_MAX}">{escape(about_text)}</textarea>'
            f'<p><button>Save publication</button></p></form>')

async def save_search(request: Request) -> Response:
    if not same_origin(request):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    form = await request.form()
    back = _back(form) if form.get("back") else "/settings"
    if form.get("remove"):
        settings.forget_search_key()
        return go(back, done="search_off")
    key = str(form.get("key") or "").strip()
    if not key or len(key) > 200 or any(c.isspace() for c in key):
        return go(back, error="search_format")
    ok, note = await run_in_threadpool(doctor.check_search_key, key)
    if not ok:
        return go(back, error="search_key")
    settings.save_search_key(key)
    return go(back, done="search_unchecked" if note else "search_saved")

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
    asks += [(6, a) for a in epic_proposals(missions) + combine_proposals()]
    body.append(heading("touch", "Needs you", len(asks), "needs-you"))
    body.append("".join(a for _, a in sorted(asks, key=lambda a: a[0]))
                or '<p class="muted empty">Nothing is waiting on you.</p>')

    # In progress, by stage. assets/flow.js relights cards in place; researchers may only visit data-lab-ok groups.
    going = flow(m for m in missions if in_progress(m) and stage_of(m) != "publishing")
    queued = publish_queue(missions)
    waiting = {slug for _, _, entries in queued for slug, _, _ in entries}
    body.append(heading("activity", "In progress", len(going) + len(waiting)))
    if not going and not waiting:
        body.append('<p class="muted empty">Nothing in progress. Add a mission above, or set an inspiration line '
                    'and start the loop.</p>')
    for key, name, title in STAGES:
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
    tail = log_tail()
    if tail:
        body.append(f'<details><summary>{icon("terminal")}Loop log (data/loop.log)</summary><pre class="notes">'
                    + escape("\n".join(tail)) + "</pre></details>")
    done = sum(m.state == State.DONE for m in missions)   # the very last thing, for anyone looking for it
    body.append(f'<a class="card site-link" href="/published"><div class="row"><span class="topic">{ICON_SITE}Published</span>'
                f'<span class="badge">{done} article{"" if done == 1 else "s"}</span></div>'
                f'<div class="sub">Everything published, where each article went, and publishing any of them again</div></a>')
    if lab:
        body.append(RESEARCHERS)
    return page("Mission Loop", "\n".join(body), poll=True)

def new_mission() -> str:
    """A long slim card right under the pipeline strip; clicking it opens a three-step wizard (topic, objective,
    epic and short name) in a dialog that posts to /missions like the CLI's add. The page's own refresh holds off
    while it is open."""
    steps = [
        ("What should the loop research?", """<label for="topic">Topic <span class="hint">what to research, in one line</span></label>
  <input type="text" id="topic" name="topic" required placeholder="e.g. Did the new transit levy pass, and who paid for the campaign?">"""),
        ("What would answer it?", """<label for="objective">Objective <span class="hint">optional: the question to answer, what counts as evidence, any hypothesis to test</span></label>
  <textarea id="objective" name="objective" rows="5"></textarea>"""),
        ("Where does it belong?", """<label for="epic">Epic <span class="hint">optional: pick one, or type a new name of 1 to 4 words</span></label>
  <input type="text" id="epic" name="epic" list="epic-names" maxlength="48" placeholder="e.g. Fed Policy Capture">
  <label for="slug">Short name <span class="hint">optional; made from the topic if left blank</span></label>
  <input type="text" id="slug" name="slug" placeholder="transit-levy">
  <p class="hint wiz-sum"></p>"""),
    ]
    panes = "".join(f'<fieldset class="wiz-step" data-step="{i}"{"" if i == 0 else " hidden"}>'
                    f'<legend>Step {i + 1} of {len(steps)}: {title}</legend>{fields}</fieldset>'
                    for i, (title, fields) in enumerate(steps))
    if loop_running():
        go_label, go_title = "Add and research next", "The Research Assistant takes it as soon as it finishes its current task."
    else:
        go_label, go_title = "Add and start loop", "Starts the loop with the Research Assistant on this mission first."
    return (epic_datalist()
            + f'<button type="button" class="card add-mission" id="add-mission" aria-haspopup="dialog" '
              f'aria-controls="wizard">{icon("plus")}{icon("pencil")}<span>Add a mission</span></button>'
            + f'''<dialog id="wizard" class="wizard" aria-labelledby="wizard-title">
<form method="post" action="/missions">
  <header><strong id="wizard-title">{icon("pencil")}New mission</strong>
    <button type="button" class="wiz-close" aria-label="Close">&times;</button></header>
  <ol class="wiz-dots" aria-hidden="true">{"".join("<li></li>" for _ in steps)}</ol>
  {panes}
  <p class="wiz-nav"><button type="button" class="wiz-back" hidden>Back</button>
    <button type="button" class="primary wiz-next">Next</button>
    <span class="wiz-add" hidden><button class="primary">Add mission</button>
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
      if (i === steps.length - 1) dlg.querySelector(".wiz-sum").textContent = "Adding: " + topic.value.trim();
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
        return (f'<details class="send-back"><summary>Send it back with notes</summary>'
                f'<form method="post" action="/m/{slug}/reject"><label for="notes">What should the {what} fix?</label>'
                f'<textarea id="notes" name="notes" rows="4" required placeholder="{placeholder}"></textarea>'
                f'<p><button>Send back to {what}</button></p></form></details>')
    if m.state == State.BRIEF_READY and not m.approved:
        return ask("review", "Approve the brief, or send it back", "Brief ready",
                   '<p class="hint">Read it below. Approving sends it to the writer.</p>',
                   f'<form method="post" action="/m/{slug}/approve"><button class="primary">Approve brief</button></form>'
                   + send_back("researcher", "e.g. Find the FAA's own incident report; the thesis overstates the denial."))
    if m.state == State.ARTICLE_READY and not m.reviewed:
        where = (f"Publishing writes it to the local site (site/{escape(m.slug)}/)"
                 + (" and queues it for 1F916, which takes one post a day, newest first" if agents.f916_enabled() else "")
                 + (" and announces it on Bluesky" if agents.bluesky_enabled() else "") + ".")
        return ask("publish", "Publish the article, or send it back", "Article ready", f'<p class="hint">{where}</p>',
                   f'<form method="post" action="/m/{slug}/publish"><button class="primary">Publish article</button></form>'
                   f'<a class="btn" href="/m/{slug}/preview" target="_blank">Open full-page preview</a>'
                   + send_back("writer", "e.g. The second section repeats the first; lead with the FAA findings."))
    if m.state == State.PARKED:
        what = "article" if orchestrator.parked_while_writing(m) else "brief"
        if orchestrator.parked_draft(m):
            push = f'<form method="post" action="/m/{slug}/push"><button>Push through anyway</button></form>'
            push_hint = (f"Push through sends the last failed {what} on to your review. It stays marked as "
                         f"failing its check, including on the published page.")
        else:
            push = ""
            push_hint = f"It can't be pushed through because no attempt left a finished {what}."
        return ask("parked", "Requeue it" + (", or push it through" if push else ""), "Parked",
                   f'<p class="hint">Requeue gives it a fresh set of {m.max_attempts} attempts and keeps the notes '
                   f'below so the agent can learn from them. {push_hint}</p>',
                   f'<form method="post" action="/m/{slug}/requeue"><button class="primary">Requeue</button></form>{push}')
    if m.state == State.DONE:
        rows = places(m)
        return (f'<div class="actions"><div class="buttons">'
                f'<a class="btn" href="/site/{slug}/" target="_blank">View on the local site</a>'
                f'<a class="btn" href="/published">All published articles</a></div>'
                f'<p class="hint" style="margin-bottom:.2rem">Published to</p><ul class="places">{places_html(m, rows)}</ul>'
                f'<p class="hint" style="margin-bottom:.2rem">Publish it again</p>{publish_form(m, rows, f"/m/{slug}")}</div>')
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
    body = [flash(request), '<p><a class="back" href="/">&larr; Back to missions</a></p>', "<h1>Published</h1>",
            f'<p>{len(missions)} article{"" if len(missions) == 1 else "s"} on the local site. '
            f'<a class="btn" href="/site/" target="_blank">Open the local site</a> '
            f'<a class="btn" href="/settings#publishing">Publishing settings</a></p>']
    rows = ['<li><strong>Local site</strong>: the foundation, always on · files in <code>site/</code> · '
            '<a href="/site/feed.xml" target="_blank">RSS</a> · <a href="/site/feed.json" target="_blank">JSON Feed</a></li>']
    rows += [f"<li>{channel_status(c)}</li>" for c in live]
    if not live:
        rows.append('<li class="muted">No channels yet. Add one on the <a href="/settings#publishing">Settings</a> page.</li>')
    body.append(f'{heading("send", "Where articles go")}<ul class="places">{"".join(rows)}</ul>')
    body.append(queue_section(queued))
    body.append(heading("newspaper", "Articles"))
    if not missions:
        body.append('<p class="muted">Nothing published yet. Articles show up here once you publish them from the Missions page.</p>')
    for m in missions:
        rows = places(m, queues)
        when = f"{m.published_at:%b %-d, %Y}" if m.published_at else ""
        body.append(
            f'<div class="card pub{" publish" if m.publish_requests else ""}" id="a-{escape(m.slug)}">'
            f'<div class="row"><a class="topic" href="/site/{quote(m.slug)}/" target="_blank">{escape(titles.get(m.slug, m.topic))}</a>'
            f'<span class="badge{" publish" if m.publish_requests else ""}">{escape(label(m))}</span></div>'
            f'<div class="sub">{mission_pill(m, epics)}{escape(when)} · <a href="/m/{quote(m.slug)}">mission page</a></div>'
            f'<ul class="places">{places_html(m, rows)}</ul>{publish_form(m, rows, "/published")}</div>')
    return page("Published", "\n".join(body), poll=True)

async def mission(request: Request) -> Response:
    slug = request.path_params["slug"]
    m = store.get_mission(slug)
    if not m:
        return page("Not found", '<h1>No such mission</h1><p><a href="/">Back to missions</a></p>')
    epics = {e.slug: e for e in store.all_epics()}
    body = [flash(request),
            f'<p><span class="badge {tone(m)}">{escape(label(m))}</span> {mission_pill(m, epics)} '
            f'<span class="muted">{escape(m.slug)}</span> {board_link(m.slug, real=True)}</p>',
            f"<h1>{escape(m.topic)}</h1>"]
    if m.objective:
        body.append(f'<p class="muted objective">{escape(m.objective)}</p>')
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
    body.append(connections(m))

    if m.validator_notes:
        title = "Why it was parked" if m.state == State.PARKED else \
                "Feedback being worked on" if m.validator_notes.startswith("Human reviewer feedback") else \
                "Last check's notes"
        name = "alert" if m.state == State.PARKED or not title.startswith("Feedback") else "message"
        body.append(f'{heading(name, title, cls="sec")}<pre class="notes">{escape(m.validator_notes)}</pre>')
    if m.unchecked_brief:
        body.append(heading("alert", "Brief pushed through despite failing the source check", cls="sec")
                    + f'<pre class="notes">{escape(m.unchecked_brief)}</pre>')
    if m.unchecked_article:
        body.append(heading("alert", "Article pushed through despite failing its check", cls="sec")
                    + f'<pre class="notes">{escape(m.unchecked_article)}</pre>')

    brief, article = _brief(slug), _article(slug)
    checked = not m.unchecked_brief
    preview = Path("missions") / slug / "preview.html"
    if article and preview.exists() and m.state in (State.ARTICLE_READY, State.DONE, State.WRITING):
        body.append(f'{heading("newspaper", "Article", cls="sec")}<iframe class="article" src="/m/{quote(slug)}/preview"></iframe>')
    if brief and m.state == State.BRIEF_READY:
        body.append(f'{heading("search", "Research brief", cls="sec")}<div class="doc">{brief_html(brief, checked)}</div>')
    elif brief:
        body.append(f'<details><summary>{icon("search")}Research brief</summary><div class="doc">{brief_html(brief, checked)}</div></details>')
    rejected = Path("missions") / slug / "brief.rejected.md"
    if rejected.exists() and m.state in (State.PARKED, State.RESEARCHING, State.QUEUED):
        body.append(f'<details><summary>{icon("cross")}Last brief that failed the source check</summary>'
                    f'<pre class="notes">{escape(rejected.read_text())}</pre></details>')
    rejected = Path("missions") / slug / "article.rejected.md"
    if rejected.exists() and m.state in (State.PARKED, State.WRITING):
        body.append(f'<details><summary>{icon("cross")}Last article that failed its check</summary>'
                    f'<pre class="notes">{escape(rejected.read_text())}</pre></details>')

    events = store.events(slug)
    if events:
        rows = "".join(f'<tr><td class="when">{escape(e["at"][:16].replace("T", " "))}</td>'
                       f'<td>{escape(LABELS.get(State(e["to_state"]), e["to_state"]))}</td>'
                       f'<td>{escape(e["note"] or "")}</td></tr>' for e in reversed(events))
        body.append(f'{heading("history", "History", cls="sec")}<table class="events">{rows}</table>')
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
    pickModel.replaceChildren(...data.models.map(m => new Option(m === data.default ? `${m} (default, shared with the loop)` : m, m)));
    if (data.models.includes(saved)) pickModel.value = saved;
    crews = data.crews || {};
    if (data.error) hint.textContent = data.error;
    else if (data.web === false) hint.textContent += " Web search needs a Brave key (Settings → Web search); it can still read pages you link.";
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
    const note = show("assistant", "Looking…", "", "wait");
    let answer, model = pending.model;
    try {
      const r = await request();
      const data = await r.json();
      answer = data.lost ? "That answer was lost when the UI restarted. Ask again?" : data.answer;
    } catch (e) {
      if (leaving) return;                     // the page is going; the next one picks it up
      answer = "Researcher Chat didn't answer. Is the UI still running?";
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
    return history.map(h => h.role === "user" ? `You: ${h.content}`
                                              : `Researcher Chat${h.model ? ` (${h.model})` : ""}: ${h.content}`).join("\\n\\n");
  }
  async function copy() {
    const text = transcript(), was = "Copy";
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
    copyBtn.textContent = !text ? "Nothing yet" : ok ? "Copied" : "Copy failed";
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
    chips = "".join(f'<button type="button">{escape(q)}</button>' for q in SUGGESTIONS)
    guide = (f'<div class="chat-guide" aria-hidden="true" data-sprites="{RESEARCHERS_SRC}"></div>'
             if settings.animations() else "")
    return f"""<button type="button" id="chat-bubble" class="chat-bubble" aria-label="Researcher Chat" aria-expanded="false"
 aria-controls="chat-panel">{ICON_CHAT}</button>
<aside id="chat-panel" class="chat-panel" aria-label="Researcher Chat" hidden>{guide}
<header><strong>Researcher Chat</strong><span><button type="button" id="chat-copy" title="Copy the conversation">Copy</button>
 <button type="button" id="chat-clear" title="Start over">Clear</button>
 <button type="button" id="chat-close" aria-label="Close">×</button></span></header>
<label class="model">Model <select id="chat-model" aria-label="Model"></select></label>
<p class="hint" id="chat-hint">It reads what the loop is doing, searches the web, can add a mission or move one to an epic, and works with the loop off.</p>
<div class="chat-log" id="chat-log" aria-live="polite"></div>
<div class="suggest" id="chat-suggest">{chips}</div>
<form id="chat-form" class="chat-form"><textarea id="chat-q" rows="2" maxlength="600"
 placeholder="Where is the grid outage mission at?" aria-label="Your question"></textarea>
<button class="primary">Ask</button></form>
</aside>{CHAT_JS}"""

async def chat_models(request: Request) -> JSONResponse:
    """Installed models for the picker. Ollama being closed is reported, not an error page."""
    MODEL = settings.chat_model()
    web = agents.chat_web()
    try:
        found = await run_in_threadpool(agents.chat_catalog)
    except Exception:
        return JSONResponse({"models": [MODEL], "default": MODEL, "crews": {MODEL: chat_crew(MODEL)}, "web": web,
                             "error": "Ollama isn't reachable, so Researcher Chat can't answer. Start the Ollama app."})
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
        return f"I couldn't answer just now ({type(e).__name__}: {str(e)[:200]}). Is Ollama running?"

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
        return JSONResponse({"answer": "That question didn't arrive in one piece. Try again."}, status_code=400)
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
        return f'<a class="onboard" href="/board?focus={quote(slug)}">{ICON_BOARD}On the board</a>'
    return (f'<span class="onboard" role="link" tabindex="0" data-board="{escape(slug, quote=True)}" '
            f'title="Show it on the board">{ICON_BOARD}On the board</span>')

PAPER = {State.QUEUED: "queued", State.RESEARCHING: "research", State.AWAITING_COMMENTS: "writing",
         State.WRITING: "writing", State.ARTICLE_READY: "publish", State.DONE: "done", State.PARKED: "parked",
         State.COMBINED: "queued"}

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
            meta = [f"{sources} source{'s' * (sources != 1)}" if sources else "",
                    f"{links} connection{'s' * (links != 1)}" if links else "",
                    f"attempt {m.attempts} of {m.max_attempts}" if m.state == State.RESEARCHING and m.attempts else "",
                    f"combined into {m.combined_into}" if m.combined_into else ""]
            n.update(title=article.title if article else m.topic, short=_cut(article.title if article else m.topic, 90),
                     thesis=_cut(thesis or m.objective or m.topic, board.PREVIEW_CHARS),
                     thesis_label="Thesis" if thesis else "Objective", stage=label(m), paper=paper(m),
                     epic=epics[m.epic].title if m.epic in epics else "", url=f"/m/{quote(m.slug)}",
                     open="Open article" if article else "Open brief" if brief else "Open mission",
                     meta=" · ".join(x for x in meta if x), live=m.slug in live, faded=m.state == State.COMBINED,
                     worker=live.get(m.slug) if live.get(m.slug) in settings.WORKERS else "")
        else:
            count = members.get(n["slug"], 0)
            e = epics.get(n["slug"])
            n.update(title=e.title if e else "Unsorted", proposed=bool(e and e.status == "proposed"),
                     count_text=f"{count} mission{'s' * (count != 1)}")
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
            f'<button type="button" data-value="{v}" class="{"on" if style[key] == v else ""}">{escape(n)}</button>'
            for v, n in options.items()) + "</div>")
    dots = "".join(f'<button type="button" class="dot{" on" if style["color"] == c else ""}" data-kind="{kind}" '
                   f'data-value="{c}" style="--c:{c}" title="{escape(name)}" aria-label="{escape(name)}"'
                   f'{"" if kind == style["string"] else " hidden"}></button>'
                   for kind, colors in settings.BOARD_STRINGS.items() for name, c in colors.items())
    return (f'<div class="board-style" hidden><div class="lbl">Surface</div>{seg("surface", settings.BOARD_SURFACES)}'
            f'<div class="lbl">Cards</div>{seg("cards", settings.BOARD_CARDS)}'
            f'<div class="lbl">Joined by</div>{seg("string", settings.BOARD_STRING_NAMES)}'
            f'<div class="lbl">Colour</div><div class="dots">{dots}'
            f'<label class="dot own" title="Your own colour"><input type="color" value="{style["color"]}" '
            f'aria-label="Your own colour"></label></div>'
            f'<a class="more" href="/settings#board">Also on the Settings page</a></div>')

def board_style_form() -> str:
    """The same choices on the Settings page; each change saves at once."""
    style = settings.board_style()
    def choice(key: str, options: dict) -> str:
        return "".join(f'<label><input type="radio" name="{key}" value="{v}"{" checked" if style[key] == v else ""} '
                       f'onchange="this.form.requestSubmit()">{escape(n)}</label>' for v, n in options.items())
    colors = settings.BOARD_STRINGS[style["string"]]
    swatches = "".join(
        f'<label class="swatch" style="--c:{c};--ink:{settings.ink_on(c)}"><input type="radio" name="color" value="{c}"'
        f'{" checked" if style["color"] == c else ""} onchange="this.form.requestSubmit()"><span class="dab"></span>'
        f'{escape(n)}</label>' for n, c in colors.items())
    presets = {c for kind in settings.BOARD_STRINGS.values() for c in kind.values()}
    own = style["color"] not in presets
    swatches += (f'<label class="swatch own{" set" if own else ""}" style="--c:{style["color"]};--ink:{settings.ink_on(style["color"])}">'
                 f'<span class="dab"></span><input type="color" name="own" value="{style["color"]}" aria-label="Your own colour" '
                 f'onchange="this.form.querySelectorAll(\'input[name=color]\').forEach(r => r.checked = false); '
                 f'this.form.requestSubmit()">Your own</label>')
    return f"""<form method="post" action="/settings/board-style" class="actions display" id="board">
  <h3>{ICON_BOARD}Board</h3>
  <p class="hint">How the <a href="/board">board</a> looks: its surface, its cards, and what joins them. The gear on the
  board changes the same thing.</p>
  <div class="choice" role="radiogroup" aria-label="Surface">{choice("surface", settings.BOARD_SURFACES)}</div>
  <p></p><div class="choice" role="radiogroup" aria-label="Cards">{choice("cards", settings.BOARD_CARDS)}</div>
  <p></p><div class="choice" role="radiogroup" aria-label="Joined by">{choice("string", settings.BOARD_STRING_NAMES)}</div>
  <p></p><div class="swatches" role="radiogroup" aria-label="Colour">{swatches}</div>
  <noscript><p><button>Save</button></p></noscript>
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
    empty = "" if data["notes"] else ('<p class="board-empty">Nothing on the board yet. '
                                      '<a href="/">Add a mission</a> and it shows up here.</p>')
    style = data["style"]
    looks = (f'surface-{style["surface"]} cards-{style["cards"]} str-{style["string"]}'
             + (" animate" if data["animations"] else ""))
    body = (f'<style>{BOARD_CSS}</style>'
            f'<div class="board-wrap {looks}" id="board" style="--string:{style["color"]}" '
            f'data-focus="{escape(focus, quote=True)}">'
            f'<div class="world"><svg class="strings" aria-hidden="true"></svg></div>{empty}'
            f'<div class="board-crumb" hidden></div>'
            f'<div class="board-pop" hidden></div>'
            f'<div class="board-legend"><span><i class="e"></i>Epic</span><span><i class="l"></i>Link</span>'
            f'<span><i class="m"></i>Same source or mention</span></div>'
            f'<div class="board-zoom"></div>'
            f'<div class="board-tools"><button type="button" data-tool="out" title="Zoom out" aria-label="Zoom out">−</button>'
            f'<button type="button" data-tool="in" title="Zoom in" aria-label="Zoom in">+</button>'
            f'<button type="button" data-tool="fit" title="Fit everything" aria-label="Fit everything">'
            f'<svg viewBox="0 0 24 24"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg></button>'
            f'<button type="button" data-tool="style" title="Board style" aria-label="Board style" '
            f'aria-expanded="false">{ICON_SETTINGS}</button></div>{board_style_panel(style)}'
            f'</div><script type="application/json" id="board-data">{raw}</script>'
            f'<script src="/assets/board.js" defer></script>{RESEARCHERS if data["animations"] else ""}')
    return page("Board", body)

BOARD_CSS = """
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
.board-wrap svg.strings { position:absolute; left:0; top:0; width:1px; height:1px; overflow:visible; }
.board-wrap .str path { fill:none; stroke-linecap:round; }
.board-wrap .str .line { stroke:var(--string); stroke-width:2.4; filter:drop-shadow(1px 3px 1.5px rgba(0,0,0,.35)); }
.board-wrap .str.link .line { stroke-width:1.8; }
.board-wrap .str.mention .line { stroke-width:1.3; stroke-dasharray:5 4; opacity:.8; }
.board-wrap .str .hit { stroke:transparent; stroke-width:12; pointer-events:stroke; }
.board-wrap .str.epic .hit { pointer-events:none; }
.board-wrap .faded { opacity:.2; filter:saturate(.3); transition:opacity .3s; }
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
.board-tools button { width:38px; height:38px; border:none; display:grid; place-items:center; font:600 18px system-ui, sans-serif;
  color:var(--fg); cursor:pointer; }
.board-tools svg { width:18px; height:18px; stroke:var(--fg); fill:none; stroke-width:1.8; }
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
.board-wrap:is(.surface-paper, .surface-grid, .surface-fridge) .board-empty, .board-wrap:is(.surface-paper, .surface-grid, .surface-fridge) .board-empty a { color:#555; text-shadow:none; }
.board-wrap:is(.surface-paper, .surface-grid, .surface-fridge) .note.unsorted { color:#6b6b70; }

/* ---------- what joins them: string (default), laser, wire ---------- */
.board-wrap.str-laser .str .line { stroke-width:1.7; filter:drop-shadow(0 0 2px var(--string)) drop-shadow(0 0 7px var(--string)); }
.board-wrap.str-laser .str .core { stroke:#fff; stroke-width:.6; opacity:.9; }
.board-wrap.str-laser .str.mention .line, .board-wrap.str-laser .str.mention .core { stroke-dasharray:6 6; }
.board-wrap.str-laser.animate .str .line { animation:laser 2.6s ease-in-out infinite alternate; }
@keyframes laser { from { opacity:.65; } to { opacity:1; } }
.board-wrap.str-wire .str .line { stroke-width:2; filter:drop-shadow(1px 2px 1px rgba(0,0,0,.4)); }
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
  border:1px solid color-mix(in srgb, var(--holo) 80%, transparent); border-radius:6px; backdrop-filter:blur(2px);
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
@media (prefers-reduced-motion: reduce) { .board-wrap .note, .board-wrap .str .line { animation:none !important; } }

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
.board-tools button svg { width:18px; height:18px; fill:none; stroke:var(--fg); stroke-width:1.8; }
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
.board-wrap.str-pixel .str .line { stroke-width:3; filter:drop-shadow(2px 2px 0 rgba(0,0,0,.45)); }
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
    bits = [f"{n} of your own" if n else "", f"{off} staying home" if off else ""]
    now = " · ".join(b for b in bits if b) or "just the twelve built-in researchers"
    return f"""<div class="actions avatar-link">
  <h3>{icon("person")}Avatars</h3>
  <p class="hint">Dress a researcher from pieces, or ask a model to draw one. They join their crew and drop by like
  everyone else; switch anyone off to keep them home. Right now: {escape(now)}.</p>
  <p><a class="btn" href="/avatars">Open the avatar studio</a></p>
</div>"""

async def avatars_page(request: Request) -> HTMLResponse:
    body = (f'<style>{AVATAR_CSS}</style><h1>Avatar studio</h1>'
            f'<p class="hint lede">Build a researcher from pieces, like dressing a paper doll, or tell a model what you want. '
            f'Saved avatars join their crew and visit the missions with everyone else. They stay on this machine '
            f'(<code>data/sprites.json</code>).</p>'
            f'<div id="studio" data-next="{escape(avatars.next_key(), quote=True)}"><noscript>The studio needs JavaScript.</noscript></div>'
            f'<script src="{RESEARCHERS_SRC}" defer></script><script src="/assets/avatars.js" defer></script>')
    return page("Avatar studio", body)

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
        return JSONResponse({"error": "That avatar didn't arrive in one piece; try again."}, status_code=400)
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
        return JSONResponse({"error": "The model took too long; try a smaller model or a simpler ask."}, status_code=504)
    except Exception as e:                 # Ollama closed, model missing
        return JSONResponse({"error": f"The model couldn't answer ({type(e).__name__}: {str(e)[:160]}). Is Ollama running?"},
                            status_code=502)
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
                return JSONResponse({"error": f"There is no avatar called {key}."}, status_code=404)
        else:
            return JSONResponse({"error": "Unknown action."}, status_code=404)
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
    Route("/settings/publication", save_publication, methods=["POST"]),
    Route("/setup/{action}", setup_action, methods=["POST"]),
    Route("/about", about),
    Route("/settings/animations", save_animations, methods=["POST"]),
    Route("/settings/theme", save_theme, methods=["POST"]),
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
    Route("/board", board_page),
    Route("/board/move", board_move, methods=["POST"]),
    Route("/board/style", board_style_save, methods=["POST"]),
    Route("/m/{slug}", mission),
    Route("/m/{slug}/preview", preview),
    Route("/m/{slug}/{action}", act, methods=["POST"]),
    Route("/epics/{slug}/{action}", epic_action, methods=["POST"]),
    Route("/combines/{slug}/{action}", combine_action, methods=["POST"]),
    Route("/loop/{action}", loop_action, methods=["POST"]),
    Route("/api/signature", api_signature),
    Route("/api/flow", api_flow),
    Route("/api/board", api_board),
    Mount("/assets", StaticFiles(directory=ASSETS)),
    Mount("/site", StaticFiles(directory="site", html=True, check_dir=False)),
])
