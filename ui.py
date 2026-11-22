"""Local web UI. Run it with `python run.py ui`, then open http://127.0.0.1:8770.

It is a thin layer over the same store and orchestrator functions the CLI uses: it reads missions,
shows briefs and articles, and records human decisions (approve, reject, publish, requeue, push). It never
runs agents itself. Work happens in the loop, which the UI can start, stop and wake. Bound to
127.0.0.1 only; nothing here is reachable from another machine.
"""
import fcntl, hashlib, re, subprocess, sys, time
from html import escape
from pathlib import Path
from urllib.parse import urlparse, quote
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, FileResponse, PlainTextResponse, Response
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
import activity, agents, clock, orchestrator, settings, store
from agents import bluesky, f916
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
    "published": "Marked for publishing. The loop publishes it on its next pass.",
    "rejected": "Sent back with your notes.",
    "requeued": "Requeued. It starts again on the next pass.",
    "pushed": "Pushed through. It is marked as failing its check and is waiting for your review.",
    "added": "Mission added. The loop starts researching it on its next pass.",
    "inspired": "Inspiration saved. The mission finder works from it on every pass.",
    "uninspired": "Inspiration cleared. The mission finder is off.",
    "started": "Loop started.",
    "stopping": "Stop requested. The loop finishes its current task, then exits.",
    "woken": "The loop runs a pass now.",
    "epic_set": "Epic saved.",
    "epic_approved": "New epic approved.",
    "epic_moved": "Missions moved to that epic.",
    "epic_rejected": "Proposed epic rejected. Its missions went back to the epics the finder picked for them.",
    "epic_renamed": "Epic renamed. Published pages show the new name.",
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
}

# ---------- loop status ----------

def loop_running() -> bool:
    """True while a run/loop holds the lock. We only test the lock, never keep it."""
    lock = orchestrator.LOCK_FILE
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False

def running_now() -> dict | None:
    """The agent working this moment, or None. The record only counts while the loop is alive."""
    return activity.current() if loop_running() else None

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
        return "Publishing next pass"
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
        return f"Research attempt {m.attempts + 1} of {m.max_attempts} on the next pass." if m.attempts \
            else "Researching on the next pass."
    if m.state == State.WRITING:
        return f"Writing attempt {m.write_attempts + 1} of {m.max_attempts}."
    return ""

def _brief(slug: str) -> ResearchBrief | None:
    data = store.load_artifact(slug, "brief")
    return ResearchBrief(**data) if data else None

def _article(slug: str) -> Article | None:
    data = store.load_artifact(slug, "article")
    return Article(**data) if data else None

def working_on(m: Mission, now: dict | None) -> str:
    """A line saying which agent is on this mission right now, or "" if none is."""
    return f'{activity.AGENTS[now["agent"]]} is working on this now.' if now and now["slug"] == m.slug else ""

def elapsed(since: float) -> str:
    seconds = max(0, int(time.time() - since))
    return f"{seconds // 60}m {seconds % 60}s" if seconds >= 60 else f"{seconds}s"

def signature() -> str:
    """Changes whenever anything the dashboard shows changes; the page polls it to refresh itself."""
    with store.conn() as c:
        rows = c.execute("SELECT data FROM missions ORDER BY rowid").fetchall()
        rows += c.execute("SELECT slug || title || status AS data FROM epics ORDER BY rowid").fetchall()
        last = c.execute("SELECT max(id) FROM events").fetchone()[0]
    now = running_now()
    working = f'{now["slug"]}:{now["agent"]}' if now else ""
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
"""

ELAPSED_JS = """<script>
const showElapsed = () => document.querySelectorAll("[data-since]").forEach(e => {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - Number(e.dataset.since)));
  e.textContent = s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
});
setInterval(showElapsed, 1000);
</script>"""

ICON_MISSIONS = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 21V4"/><path d="M5 4c4-2.5 7 2.5 14 0v10c-7 2.5-10-2.5-14 0"/></svg>'
ICON_SITE = ('<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18"/>'
             '<path d="M12 3c3 3 3.6 6 3.6 9s-.6 6-3.6 9c-3-3-3.6-6-3.6-9s.6-6 3.6-9z"/></svg>')

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
            ||["TEXTAREA", "INPUT"].includes(document.activeElement.tagName);
  if (busy || document.hidden) return;
  try {{ const s = await (await fetch("/api/signature")).text(); if (s !== sig) location.reload(); }} catch (e) {{}}
}}, 5000);
</script>{ELAPSED_JS}""" if poll else ""
    html = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{escape(title)}</title>{THEME_INIT}<style>{CSS}{UI_CSS}</style></head><body><main>"
            f'<nav class="top"><a class="brand" href="/">Mission Loop</a>'
            f'<span class="links">{nav_status()}<a href="/">{ICON_MISSIONS}Missions</a>'
            f'<a href="/site/" target="_blank">{ICON_SITE}Published site</a>{THEME_TOGGLE}</span></nav>'
            f"{body}</main>{script}</body></html>")
    return HTMLResponse(html)

def flash(request: Request) -> str:
    done, err = request.query_params.get("done"), request.query_params.get("error")
    if err in ERRORS:
        return f'<div class="flash err">{escape(ERRORS[err])}</div>'
    if done in MESSAGES:
        return f'<div class="flash">{escape(MESSAGES[done])}</div>'
    return ""

def agent_strip(now: dict | None) -> str:
    """Every agent in pipeline order, with the one working this moment lit up."""
    working = now["agent"] if now else None
    chips = [f'<span class="agent on" aria-current="step">{name}</span>' if key == working
             else f'<span class="agent">{name}</span>'
             for key, name in activity.AGENTS.items()
             if key == working or (key != "contacts" or settings.OUTREACH)
             and (key != "1f916" or agents.f916_enabled())
             and (key != "bluesky" or agents.bluesky_enabled())]
    strip = '<span class="arrow" aria-hidden="true">›</span>'.join(chips)
    if now and not now["slug"]:            # the mission finder works for the desk, not one mission
        status = (f'<strong>{activity.AGENTS[working]}</strong> is looking for new missions'
                  f' · <span data-since="{float(now["since"])}">{elapsed(now["since"])}</span>')
    elif now:
        m = store.get_mission(now["slug"])
        status = (f'<strong>{activity.AGENTS[working]}</strong> is working on '
                  f'<a href="/m/{quote(now["slug"])}">{escape(m.topic if m else now["slug"])}</a>'
                  f' · <span data-since="{float(now["since"])}">{elapsed(now["since"])}</span>')
    else:
        status = "No agent is working right now."
    return f'<div class="agents">{strip}</div><div class="now">{status}</div>'

def loop_panel(back: str) -> str:
    running, stopping = loop_running(), orchestrator.STOP_FLAG.exists()
    if running and stopping:
        state, dot = "Loop is stopping after its current task", "stopping"
        controls = ""
    elif running:
        state, dot = "Loop is running", "on"
        controls = (f'<form method="post" action="/loop/wake"><input type="hidden" name="back" value="{back}">'
                    f'<button>Run a pass now</button></form>'
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
    state = "On every pass it searches around this line and adds 1 to 3 missions." if line \
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
        return ("1F916 is off: no key in data/1f916.json. "
                "Join once with <code>python run.py join-1f916 &lt;handle&gt;</code>.")
    queued, until, state = len(orchestrator.queue(f916)), f916.spent_until(), f916.load_state()
    if until and until > clock.now():
        when = f"today's post is used; next one after {until:%b %-d %H:%M} UTC"
    else:
        when = "can post on the next pass" if queued else "nothing waiting"
    problem = state.get("note", "") if not state.get("spent_until") else ""
    line = (f'1F916: posting as <strong>{escape(f916.key()["handle"])}</strong>, one article a day, newest first · '
            f"{queued} queued · {when}")
    return line + (f'<br><span class="hint">Last try failed: {escape(problem)}</span>' if problem else "")

def bluesky_status() -> str:
    """One line on the Bluesky channel: who we post as, how many wait, and the last failure if any."""
    if not agents.bluesky_enabled():
        return ("Bluesky is off: set <code>BLUESKY_HANDLE</code> and <code>BLUESKY_APP_PASSWORD</code> in .env, "
                "then check them with <code>python run.py check-bluesky</code>.")
    queued, problem = len(orchestrator.queue(bluesky)), bluesky.load_state().get("note", "")
    line = (f"Bluesky: posting as <strong>{escape(bluesky.handle())}</strong>, about one article a pass, "
            f"newest first · {queued} queued")
    if not settings.SITE_URL:
        line += ('<br><span class="hint">No MISSION_LOOP_SITE_URL, so posts link to the 1F916 copy when there '
                 "is one and otherwise carry no link.</span>")
    return line + (f'<br><span class="hint">Last try failed: {escape(problem)}</span>' if problem else "")


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

def card(m: Mission, now: dict | None = None, epics: dict[str, Epic] | None = None) -> str:
    epics = {e.slug: e for e in store.all_epics()} if epics is None else epics
    color = tone(m)
    working = working_on(m, now)
    sub = working or next_step(m)
    if m.state == State.DONE and m.published_at:
        sub = f"Published {m.published_at:%b %-d, %Y} · {channel_names(m)}"
    classes = " ".join(c for c in ("card", color, "active" if working else "") if c)
    return (f'<a class="{classes}" href="/m/{quote(m.slug)}"><div class="row"><span class="topic">{escape(m.topic)}</span>'
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
            body.append("".join(card(m, now, epics) for m in ms))
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
        rows.append("<li>Bluesky: waiting (about one post a pass, newest first)</li>")
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
        orchestrator.STOP_FLAG.parent.mkdir(parents=True, exist_ok=True)
        orchestrator.STOP_FLAG.touch()
        return go(back, done="stopping")
    if action == "wake":
        wake()
        return go(back, done="woken")
    return PlainTextResponse("Unknown action.", status_code=404)

app = Starlette(routes=[
    Route("/", home),
    Route("/missions", add_mission, methods=["POST"]),
    Route("/inspiration", save_inspiration, methods=["POST"]),
    Route("/m/{slug}", mission),
    Route("/m/{slug}/preview", preview),
    Route("/m/{slug}/{action}", act, methods=["POST"]),
    Route("/epics/{slug}/{action}", epic_action, methods=["POST"]),
    Route("/loop/{action}", loop_action, methods=["POST"]),
    Route("/api/signature", api_signature),
    Mount("/site", StaticFiles(directory="site", html=True, check_dir=False)),
])
