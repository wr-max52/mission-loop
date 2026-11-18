"""Local web UI. Run it with `python run.py ui`, then open http://127.0.0.1:8770.

It is a thin layer over the same store and orchestrator functions the CLI uses: it reads missions,
shows briefs and articles, and records human decisions (approve, reject, publish, requeue). It never
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
import clock, orchestrator, store
from contracts import Mission, State, ResearchBrief, Article
from render import CSS, brief_html

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
    "added": "Mission added. The loop starts researching it on its next pass.",
    "started": "Loop started.",
    "stopping": "Stop requested. The loop finishes its current task, then exits.",
    "woken": "The loop runs a pass now.",
}
ERRORS = {
    "topic": "A mission needs a topic.",
    "slug": "Short name: lowercase letters, numbers and dashes only.",
    "exists": "A mission with that short name already exists.",
    "notes": "Say what needs fixing so the agent can act on it.",
    "state": "That mission isn't waiting for that action any more.",
    "running": "The loop is already running.",
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
        stage = "writing" if m.write_attempts >= m.max_attempts else "research"
        return f"Gave up after {m.max_attempts} {stage} attempts. Read why, then requeue it."
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

def slugify(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words[:5])[:60].strip("-")

def signature() -> str:
    """Changes whenever anything the dashboard shows changes; the page polls it to refresh itself."""
    with store.conn() as c:
        rows = c.execute("SELECT data FROM missions ORDER BY rowid").fetchall()
        last = c.execute("SELECT max(id) FROM events").fetchone()[0]
    raw = "".join(r["data"] for r in rows) + f"|{last}|{loop_running()}|{orchestrator.STOP_FLAG.exists()}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]

# ---------- page shell ----------

UI_CSS = """
main { max-width: 56rem; }
body { font-family: -apple-system, system-ui, sans-serif; font-size: 16px; }
nav.top { display:flex; justify-content:space-between; align-items:center; gap:1rem; flex-wrap:wrap;
          border-bottom:1px solid var(--rule); padding-bottom:1rem; margin-bottom:1.5rem; }
nav.top a.brand { font-weight:700; font-size:1.1rem; color:var(--fg); text-decoration:none; }
nav.top .links a { margin-left:1rem; color:var(--muted); text-decoration:none; }
.loop { display:flex; align-items:center; gap:.6rem; flex-wrap:wrap; padding:.8rem 1rem;
        border:1px solid var(--rule); border-radius:10px; margin-bottom:1.5rem; }
.loop form { display:flex; gap:.4rem; align-items:center; margin:0; }
.dot { width:.6rem; height:.6rem; border-radius:50%; background:var(--muted); display:inline-block; }
.dot.on { background:#2f9e44; } .dot.stopping { background:#e8a33d; }
.loop .grow { flex:1; }
h1 { font-family: Georgia, serif; }
h2.group { font-size:.8rem; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); margin:2rem 0 .5rem; }
.card { display:block; border:1px solid var(--rule); border-radius:10px; padding:.9rem 1rem; margin-bottom:.6rem;
        text-decoration:none; color:var(--fg); }
.card:hover { border-color:var(--muted); }
.card .row { display:flex; justify-content:space-between; gap:1rem; align-items:baseline; }
.card .topic { font-weight:600; }
.card .sub { color:var(--muted); font-size:.9rem; margin-top:.25rem; }
.badge { font-size:.72rem; font-weight:600; text-transform:uppercase; letter-spacing:.05em; white-space:nowrap;
         padding:.2rem .5rem; border-radius:999px; background:var(--quote); color:var(--muted); }
.badge.you { background:#8a2b2b; color:#fff; } .badge.done { background:#2f9e44; color:#fff; }
.badge.parked { background:#e8a33d; color:#1d1d1f; }
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
"""

def page(title: str, body: str, poll: bool = False) -> HTMLResponse:
    # Refresh itself when the loop changes something, unless the human is typing.
    script = f"""<script>
let sig = "{signature()}";
setInterval(async () => {{
  const busy = [...document.querySelectorAll("textarea, input[type=text]")].some(e => e.value.trim())
            || ["TEXTAREA", "INPUT"].includes(document.activeElement.tagName);
  if (busy || document.hidden) return;
  try {{ const s = await (await fetch("/api/signature")).text(); if (s !== sig) location.reload(); }} catch (e) {{}}
}}, 5000);
</script>""" if poll else ""
    html = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{escape(title)}</title><style>{CSS}{UI_CSS}</style></head><body><main>"
            f'<nav class="top"><a class="brand" href="/">Mission Loop</a>'
            f'<span class="links"><a href="/">Missions</a><a href="/site/" target="_blank">Published site</a></span></nav>'
            f"{body}</main>{script}</body></html>")
    return HTMLResponse(html)

def flash(request: Request) -> str:
    done, err = request.query_params.get("done"), request.query_params.get("error")
    if err in ERRORS:
        return f'<div class="flash err">{escape(ERRORS[err])}</div>'
    if done in MESSAGES:
        return f'<div class="flash">{escape(MESSAGES[done])}</div>'
    return ""

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
    return f'<div class="loop"><span class="dot {dot}"></span><span class="grow">{state}</span>{controls}</div>'

def card(m: Mission) -> str:
    cls = "you" if needs_you(m) and m.state != State.PARKED else \
          "parked" if m.state == State.PARKED else "done" if m.state == State.DONE else ""
    sub = next_step(m)
    if m.state == State.DONE and m.published_at:
        sub = f"Published {m.published_at:%b %-d, %Y}"
    return (f'<a class="card" href="/m/{quote(m.slug)}"><div class="row"><span class="topic">{escape(m.topic)}</span>'
            f'<span class="badge {cls}">{escape(label(m))}</span></div>'
            f'<div class="sub">{escape(m.slug)}{" · " + escape(sub) if sub else ""}</div></a>')

# ---------- pages ----------

async def home(request: Request) -> HTMLResponse:
    missions = store.all_missions()
    groups = [("Needs you", [m for m in missions if needs_you(m)]),
              ("In progress", [m for m in missions if not needs_you(m) and m.state != State.DONE]),
              ("Published", [m for m in missions if m.state == State.DONE])]
    body = [flash(request), loop_panel("/")]
    if not missions:
        body.append('<p class="muted">No missions yet. Add one below.</p>')
    for title, ms in groups:
        if ms:
            body.append(f'<h2 class="group">{title} ({len(ms)})</h2>' + "".join(card(m) for m in ms))
    body.append("""<h2 class="group">New mission</h2>
<form method="post" action="/missions" class="actions">
  <label for="topic">Topic <span class="hint">what to research, in one line</span></label>
  <input type="text" id="topic" name="topic" required placeholder="e.g. Did the new transit levy pass, and who paid for the campaign?">
  <label for="objective">Objective <span class="hint">optional: the question to answer, what counts as evidence, any hypothesis to test</span></label>
  <textarea id="objective" name="objective" rows="4"></textarea>
  <label for="slug">Short name <span class="hint">optional; made from the topic if left blank</span></label>
  <input type="text" id="slug" name="slug" placeholder="transit-levy">
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
                f'</div><p class="hint">Publishing writes it to the local site (site/{escape(m.slug)}/).</p>'
                + send_back("writer", "e.g. The second section repeats the first; lead with the FAA findings.")
                + "</div>")
    if m.state == State.PARKED:
        return (f'<div class="actions"><div class="buttons">'
                f'<form method="post" action="/m/{slug}/requeue"><button class="primary">Requeue</button></form>'
                f'</div><p class="hint">It gets a fresh set of {m.max_attempts} attempts and keeps the notes below '
                f'so the agent can learn from them.</p></div>')
    if m.state == State.DONE:
        return (f'<div class="actions"><div class="buttons">'
                f'<a class="btn" href="/site/{slug}/" target="_blank">View on the published site</a></div></div>')
    return ""

async def mission(request: Request) -> Response:
    slug = request.path_params["slug"]
    m = store.get_mission(slug)
    if not m:
        return page("Not found", '<h1>No such mission</h1><p><a href="/">Back to missions</a></p>')
    badge = "you" if needs_you(m) and m.state != State.PARKED else \
            "parked" if m.state == State.PARKED else "done" if m.state == State.DONE else ""
    body = [flash(request), loop_panel(f"/m/{quote(slug)}"),
            f'<p><span class="badge {badge}">{escape(label(m))}</span> <span class="muted">{escape(m.slug)}</span></p>',
            f"<h1>{escape(m.topic)}</h1>"]
    if m.objective:
        body.append(f'<p class="muted">{escape(m.objective)}</p>')
    step = next_step(m)
    if step:
        body.append(f"<p><strong>{escape(step)}</strong></p>")
    body.append(actions_for(m))

    if m.validator_notes:
        title = "Why it was parked" if m.state == State.PARKED else \
                "Feedback being worked on" if m.validator_notes.startswith("Human reviewer feedback") else \
                "Last check's notes"
        body.append(f'<h2>{title}</h2><pre class="notes">{escape(m.validator_notes)}</pre>')

    brief, article = _brief(slug), _article(slug)
    preview = Path("missions") / slug / "preview.html"
    if article and preview.exists() and m.state in (State.ARTICLE_READY, State.DONE, State.WRITING):
        body.append(f'<h2>Article</h2><iframe class="article" src="/m/{quote(slug)}/preview"></iframe>')
    if brief and m.state == State.BRIEF_READY:
        body.append(f'<h2>Research brief</h2><div class="doc">{brief_html(brief)}</div>')
    elif brief:
        body.append(f'<details><summary>Research brief</summary><div class="doc">{brief_html(brief)}</div></details>')
    rejected = Path("missions") / slug / "brief.rejected.md"
    if rejected.exists() and m.state in (State.PARKED, State.RESEARCHING, State.QUEUED):
        body.append('<details><summary>Last brief that failed the source check</summary>'
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
    store.save_mission(Mission(slug=slug, topic=topic, objective=objective))
    store.log_event(slug, clock.now(), "", State.QUEUED.value, "added in the UI")
    if loop_running():
        wake()
    return go(f"/m/{quote(slug)}", done="added")

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
    Route("/m/{slug}", mission),
    Route("/m/{slug}/preview", preview),
    Route("/m/{slug}/{action}", act, methods=["POST"]),
    Route("/loop/{action}", loop_action, methods=["POST"]),
    Route("/api/signature", api_signature),
    Mount("/site", StaticFiles(directory="site", html=True, check_dir=False)),
])
