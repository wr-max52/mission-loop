"""Local MCP server that lets the chat assistant see what the loop is doing, without touching it.

Most tools only read: the database, data/activity.json, the agents' trails (trail.py), the channels' state files
and the loop log. add_mission queues a new mission exactly like the UI's Add mission box (it still waits for human
brief review) and set_mission_epic puts a mission in an epic like the Epic field on its page. The rest give the chat
assistant everything the Theorist can do, through the same checks (agents/theorist.py) and the same orchestrator
functions: desk_overview reads the desk as the Theorist does, set_inspiration changes the line, propose_epic and
propose_combine propose (they wait for the human), link_missions links two missions, and ask_theorist asks for the
Theorist's turn. None of them changes a mission's stage or work. No tool takes the
loop's lock or talks to a model, so asking never slows or redirects the work. Answers are short on purpose:
the assistant has a small context.
The chat assistant starts this through the "loop" entry in mcp.json; any other MCP client can too.
"""
import time
from datetime import datetime, timezone
from pathlib import Path
from mcp.server.mcpserver import MCPServer
import activity, agents, doctor, orchestrator, settings, store, trail
from agents import bluesky, f916, github, theorist
from contracts import Mission, State, epic_title, words

LOG = Path("data/loop.log")
MAX_LIST = 25
MAX_NOTES = 300

LABELS = {
    State.QUEUED: "queued", State.RESEARCHING: "researching", State.BRIEF_READY: "brief ready for human review",
    State.AWAITING_COMMENTS: "waiting for comments", State.WRITING: "writing",
    State.ARTICLE_READY: "article ready for human review", State.DONE: "published", State.PARKED: "parked (gave up)",
    State.COMBINED: "combined into a bigger mission",
}

server = MCPServer("loop")

def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    return f"{seconds // 86400}d {seconds % 86400 // 3600}h"

def _since(at: str) -> str:
    try:
        when = datetime.fromisoformat(at)
    except (TypeError, ValueError):
        return "?"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return _ago((datetime.now(timezone.utc) - when).total_seconds()) + " ago"

def _working() -> list[dict]:
    """The agents working this moment, at most one per seat (a copy of a worker). The record only counts while the loop holds its lock."""
    return activity.working() if orchestrator.loop_running() else []

def _on(slug: str, now: list[dict]) -> dict | None:
    return next((e for e in now if e["slug"] == slug), None)

def _worker(e: dict) -> str:
    seat = e.get("worker", "")
    name = settings.seat_name(seat)
    return f" ({name}, {settings.worker_model(settings.seat_worker(seat))})" if name else ""

def _label(m) -> str:
    if m.state == State.BRIEF_READY and m.approved:
        return "brief approved, article next"
    if m.state == State.ARTICLE_READY and m.reviewed:
        return "approved, publishing next"
    return LABELS[m.state]

def _epic(m, titles: dict[str, str]) -> str:
    return f" | epic: {titles[m.epic]}" if m.epic in titles else ""

def _needs_human(m) -> bool:
    return ((m.state == State.BRIEF_READY and not m.approved) or (m.state == State.ARTICLE_READY and not m.reviewed)
            or m.state == State.PARKED)

def _log_tail(n: int) -> list[str]:
    try:
        lines = LOG.read_text(errors="replace").splitlines()
    except OSError:
        return []
    return [l[:200] for l in lines if l.strip() and "HTTP Request" not in l][-n:]

@server.tool()
def loop_status() -> str:
    """Is the loop running, which agent is working on which mission right now and for how long,
    how many missions are in each stage, which ones wait for the human, and the last lines of the loop's log."""
    running = orchestrator.loop_running()
    waiting = doctor.waiting() if running else None
    lines = ["Loop: " + ("stopping after its current task" if running and orchestrator.STOP_FLAG.exists()
                         else f"waiting, it can't work: {waiting['reason']}" if waiting
                         else "running" if running else "off (no agent is working)")]
    now = _working()
    for e in now:                          # the two workers can each be busy at once
        who = activity.AGENTS[e["agent"]] + _worker(e)
        if e["slug"]:
            m = store.get_mission(e["slug"])
            lines.append(f"Working now: {who} on {e['slug']} ({m.topic if m else '?'}), "
                         f"for {_ago(time.time() - e['since'])}")
        else:
            task = "looking across the desk for patterns" if e["agent"] == "theorist" else "looking for new missions"
            lines.append(f"Working now: {who}, {task}, for {_ago(time.time() - e['since'])}")
    if not now and running:
        lines.append("Working now: nothing; the loop is waiting for a human or for its next check")
    counts: dict[str, int] = {}
    missions = store.all_missions()
    for m in missions:
        counts[_label(m)] = counts.get(_label(m), 0) + 1
    lines.append("Missions: " + (", ".join(f"{n} {k}" for k, n in counts.items()) or "none"))
    lines.append("Auto mode: " + ("on (briefs and new epics are approved automatically; publishing still waits "
                                  "for the human)" if settings.auto_mode() else "off (every approval waits for the human)"))
    waiting = [m for m in missions if _needs_human(m)]
    lines.append(f"Waiting for the human: {len(waiting) or 'nothing'}")
    lines += [f"- {m.topic[:80]} [{m.slug}]: {_label(m)}" for m in waiting[:MAX_LIST]]
    tail = _log_tail(6)
    if tail:
        lines.append("Last log lines:\n" + "\n".join(tail))
    return "\n".join(lines)

@server.tool()
def list_missions(stage: str = "active") -> str:
    """List missions, one per line: slug, stage, topic, time since it last changed.
    stage is "active" (default: everything not published or parked), "all", "waiting" (needs a human),
    or a stage name such as researching, writing, parked, published."""
    stage = stage.strip().lower() or "active"
    missions = store.all_missions()
    if stage == "active":
        missions = [m for m in missions if m.state not in (State.DONE, State.PARKED, State.COMBINED)]
    elif stage == "waiting":
        missions = [m for m in missions if _needs_human(m)]
    elif stage != "all":
        missions = [m for m in missions if stage in (m.state.value.lower(), LABELS[m.state])
                    or LABELS[m.state].startswith(stage)]
    if not missions:
        return f"No missions match '{stage}'."
    now, titles = _working(), store.epic_titles()
    lines = []
    for m in missions[-MAX_LIST:]:
        events = store.events(m.slug)
        changed = _since(events[-1]["at"]) if events else "?"
        e = _on(m.slug, now)
        busy = f" [{activity.AGENTS[e['agent']]} working on it now]" if e else ""
        lines.append(f"- {m.slug} | {_label(m)} | {m.topic[:90]}{_epic(m, titles)} | changed {changed}{busy}")
    more = f"\n({len(missions) - MAX_LIST} older not shown)" if len(missions) > MAX_LIST else ""
    return "\n".join(lines) + more

@server.tool()
def mission_status(slug: str) -> str:
    """Where one mission is: its stage, attempts used, why its last attempt failed, whether an agent
    is on it now, and its recent history. Use list_missions or search_missions to find the slug."""
    m = store.get_mission(slug.strip())
    if not m:
        return f"No mission '{slug}'. Use search_missions to find its slug."
    lines = [f"{m.slug}: {m.topic}", f"Stage: {_label(m)}"]
    epic = store.get_epic(m.epic) if m.epic else None
    lines.append(f"Epic: {epic.title}" + (" (proposed, waiting for the human)" if epic.status == "proposed" else "")
                 if epic else "Epic: none")
    if m.combined_into:
        lines.append(f"Combined into: {m.combined_into}")
    if m.combines:
        lines.append(f"Combines: {', '.join(m.combines)}")
    for k in store.links_of(m.slug):
        lines.append(f"Linked to {k.other(m.slug)} by the {k.by}" + (f": {k.why}" if k.why else ""))
    if m.state in (State.RESEARCHING, State.QUEUED) or m.attempts:
        lines.append(f"Research attempts: {m.attempts} of {m.max_attempts}")
    if m.state == State.WRITING or m.write_attempts:
        lines.append(f"Writing attempts: {m.write_attempts} of {m.max_attempts}")
    if m.checked_article and m.state == State.WRITING:
        lines.append("The article passed its check; the Research Assistant rates it next")
    if m.article_rating and m.state in (State.ARTICLE_READY, State.DONE):
        lines.append("Rating: " + " ".join(m.article_rating.notes().split()))
    e = _on(m.slug, _working())
    if e:
        lines.append(f"Working on it now: {activity.AGENTS[e['agent']]}{_worker(e)}, for {_ago(time.time() - e['since'])}")
    if m.validator_notes:
        notes = " ".join(m.validator_notes.split())
        lines.append(f"Last problems: {notes[:MAX_NOTES]}{'…' if len(notes) > MAX_NOTES else ''}")
    events = store.events(m.slug)[-6:]
    if events:
        lines.append("Recent history:")
        lines += [f"- {_since(e['at'])}: {e['from_state'] or 'new'} -> {e['to_state']}, {e['note'][:120]}"
                  for e in events]
    return "\n".join(lines)

@server.tool()
def agent_steps(slug: str, last: int = 8) -> str:
    """The last few things the agents did on a mission: their thinking, the searches and pages they asked
    for, what came back. Use it to tell what an agent is doing right now and why it is taking long.
    Use slug "finder" for the mission finder and "theorist" for the Theorist. Read-only: it never interrupts the agent."""
    slug = slug.strip()
    agent = "theorist" if slug.lower() in ("theorist", "the theorist") else "finder"
    key = "" if slug.lower() in ("finder", "mission finder", "theorist", "the theorist", "") else slug
    if key and not store.get_mission(key):
        return f"No mission '{slug}'. Use search_missions to find its slug."
    steps = trail.recent(key, max(1, min(int(last), 15)), agent)
    if not steps:
        return f"No recorded steps for {slug or 'the finder'} yet."
    now = time.time()
    return "\n".join(f"- {_ago(now - s['at'])} ago, {activity.AGENTS.get(s['agent'], s['agent'])}, "
                     f"{s['kind']}: {s['text'][:240]}" for s in steps)

@server.tool()
def theorist_notes() -> str:
    """What the Theorist saw on its last turn (patterns inside and across epics), how it evolved the
    inspiration line and why, the epics, mission and combine it proposed, the missions it linked, and the line's
    recent history. Every third turn only tidies the cork board; then it says what moved, and the turn before."""
    last = store.last_theory()
    lines = [f"Inspiration line now: {store.get_inspiration() or '(none; the finder and the Theorist are off)'}"]
    if last.get("kind") == "board":        # every third turn only arranges the cork board
        if last.get("failed"):
            lines.append(f"Last turn {_since(last['at'])} was a board turn and failed: {last['failed']}")
        else:
            lines.append(f"Last turn {_since(last['at'])} tidied the cork board, moving {len(last.get('moved', []))} "
                         f"note(s). Why: {last.get('why') or '-'}")
            lines += [f"Code dropped: {n}" for n in last.get("notes", [])]
        last = last.get("ideas") or {}
        if last:
            lines.append("The turn before that looked across the desk:")
    if not last:
        lines.append("The Theorist has not had a turn yet.")
    elif last.get("failed"):
        lines.append(f"Last turn {_since(last['at'])} failed: {last['failed']}")
    else:
        lines.append(f"Last turn {_since(last['at'])}. Why: {last.get('why') or '-'}")
        lines += [f"- pattern: {p}" for p in last.get("patterns", [])]
        if last.get("line_before") != last.get("line_after"):
            lines.append(f"Line before: {last.get('line_before')}")
        lines += [f"Proposed epic (waits for the human): {t}" for t in last.get("epics", [])]
        if last.get("mission"):
            lines.append(f"Proposed mission: {last['mission']}")
        c = store.get_combine(last["combine"]) if last.get("combine") else None
        if c:
            lines.append(f"Proposed combining {', '.join(c.members)} into: {c.topic} ({c.status})")
        lines += [f"Linked: {a} <-> {b}" for a, b in last.get("links", [])]
        lines += [f"Searched the library for: {q}" for q in last.get("lookups", [])]
        lines += [f"Code dropped: {n}" for n in last.get("notes", [])]
    history = store.inspiration_history(5)
    if history:
        lines.append("Line history, oldest first:")
        who = {"chat": "chat assistant (you)"}
        lines += [f"- {_since(h['at'])}, by the {who.get(h['by'], h['by'])}: {h['line'] or '(cleared)'}" for h in history]
    return "\n".join(lines)

@server.tool()
def search_missions(query: str) -> str:
    """Find missions (any stage, including published) whose topic or objective shares words with the query.
    Returns slug, stage and topic for the best matches."""
    wanted = words(query)
    if not wanted:
        return "Give at least one word longer than 3 letters."
    scored = []
    for m in store.all_missions():
        score = len(wanted & words(f"{m.topic} {m.objective} {m.slug.replace('-', ' ')}"))
        if score:
            scored.append((score, m))
    if not scored:
        return f"No mission matches '{query}'."
    scored.sort(key=lambda x: -x[0])
    titles = store.epic_titles()
    return "\n".join(f"- {m.slug} | {_label(m)} | {m.topic[:90]}{_epic(m, titles)}" for _, m in scored[:8])

@server.tool()
def publication_status() -> str:
    """What has been published and where: each channel (local site, 1F916, GitHub Pages, Bluesky, custom) with whether it is on,
    how many articles wait for it and when it may post next; articles approved to publish next pass; and the
    latest published articles with the channels each reached and any refusal."""
    missions = store.all_missions()
    done = sorted((m for m in missions if m.state == State.DONE), key=lambda m: str(m.published_at or ""), reverse=True)
    lines = [f"Published: {len(done)} article(s) on the local site (site/index.html, feeds site/feed.xml and site/feed.json)"]
    url = settings.site_url()
    lines.append(f"Site address: {url}" if url else "Site address: not set (Settings page), so links stay relative")
    for c in (f916, github, bluesky):
        if not c.enabled():
            lines.append(f"{c.NAME}: off (not set up on the Settings page)")
            continue
        on = c in agents.CHANNELS
        waiting = len(orchestrator.queue(c))
        until = c.wait_until()
        nxt = f", next post allowed in {_ago((until - datetime.now(timezone.utc)).total_seconds())}" if until and until > datetime.now(timezone.utc) else ""
        lines.append(f"{c.NAME}: {'on' if on else 'set up but off in stub mode'}, {waiting} waiting{nxt}")
    for c in agents.channels():
        if c not in (f916, github, bluesky):   # custom channels, set up on the Settings page
            lines.append(f"{c.NAME} (custom): on, {len(orchestrator.queue(c))} waiting")
    asked = [m for m in done if m.publish_requests]
    if asked:
        lines.append("Asked to publish again, next pass: " + "; ".join(
            f"{m.topic[:60]} [{m.slug}] to {', '.join(orchestrator.channel_name(k) for k in m.publish_requests)}"
            for m in asked[:MAX_LIST]))
    soon = [m for m in missions if m.state == State.ARTICLE_READY and m.reviewed]
    if soon:
        lines.append("Approved, publishing next pass: " + "; ".join(f"{m.topic[:60]} [{m.slug}]" for m in soon[:MAX_LIST]))
    for m in done[:10]:
        reached = ", ".join(orchestrator.channel_name(k) for k in orchestrator.channels(m)) or "nowhere"
        refused = "".join(f"; {orchestrator.channel_name(k)} refused: {v[:100]}" for k, v in m.channel_errors.items())
        when = _since(m.published_at.isoformat()) if m.published_at else "?"
        lines.append(f"- {m.topic[:80]} [{m.slug}], published {when}, on: {reached}{refused}")
    return "\n".join(lines)

@server.tool()
def add_mission(topic: str, objective: str = "", epic: str = "") -> str:
    """Queue a new research mission, like the UI's Add mission box. topic is the question or claim to look
    into (one sentence); objective says what a good answer finds out; epic is an optional 1 to 4 word title
    (an existing epic's title, or a new one). The mission waits in the queue and its brief still needs the
    human's approval. Only call this when the human asks for a new mission."""
    topic, objective = " ".join(topic.split())[:300], " ".join(objective.split())[:600]
    if len(topic) < 10:
        return "Give the mission a topic of at least a few words."
    if epic.strip() and not epic_title(epic):
        return "An epic title is 1 to 4 words."
    missions = store.all_missions()
    same = next((m for m in missions if m.topic.lower() == topic.lower()), None)
    if same:
        return f"That mission already exists: {same.slug} ({_label(same)})."
    slug = orchestrator._new_slug(topic, {m.slug for m in missions})
    store.save_mission(Mission(slug=slug, topic=topic, objective=objective,
                               epic=store.create_epic(epic).slug if epic.strip() else ""))
    store.log_event(slug, datetime.now(timezone.utc), "", State.QUEUED.value, "added by the chat assistant")
    if orchestrator.loop_running():
        orchestrator.WAKE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        orchestrator.WAKE_FLAG.touch()
    return f"Queued {slug}: {topic}. " + ("The loop will pick it up now." if orchestrator.loop_running()
                                          else "The loop is off; it starts when the loop runs.")

@server.tool()
def list_epics(epic: str = "") -> str:
    """Epics are short themes that group missions. With no epic given: every epic with its status and how many
    missions it holds, plus how many missions have none. With an epic's title: every mission in it (any stage,
    published included), with slug, stage and topic."""
    missions = store.all_missions()
    if epic.strip():
        e = store.find_epic(epic)
        if not e:
            return f"No epic '{epic}'. Call list_epics with no epic to see them all."
        inside = [m for m in missions if m.epic == e.slug]
        head = f"{e.title}{' (proposed, waiting for the human)' if e.status == 'proposed' else ''}: {len(inside)} mission(s)"
        more = f"\n({len(inside) - MAX_LIST} more not shown)" if len(inside) > MAX_LIST else ""
        return "\n".join([head] + [f"- {m.slug} | {_label(m)} | {m.topic[:90]}" for m in inside[:MAX_LIST]]) + more
    epics = store.all_epics()
    if not epics:
        return "No epics yet."
    lines = []
    for e in epics:
        inside = [m for m in missions if m.epic == e.slug]
        done = sum(m.state == State.DONE for m in inside)
        status = " (proposed, waiting for the human)" if e.status == "proposed" else ""
        lines.append(f"- {e.title}{status}: {len(inside)} mission(s), {done} published")
    loose = sum(not m.epic for m in missions)
    if loose:
        lines.append(f"- (no epic): {loose} mission(s)")
    return "\n".join(lines)

@server.tool()
def set_mission_epic(slug: str, epic: str) -> str:
    """Put a mission (any stage, published included) in an epic, like the Epic field on its page. epic is an
    existing epic's title, or a new 1 to 4 word title, which makes a new epic; "none" takes the mission out of
    its epic. Published pages are redrawn with the new label. Nothing else about the mission changes. Only
    call this when the human asks to move a mission; find its slug with search_missions first."""
    m = store.get_mission(slug.strip())
    if not m:
        return f"No mission '{slug}'. Use search_missions to find its slug."
    name = "" if epic.strip().lower() in ("", "none", "no epic") else epic
    if name and not epic_title(name):
        return "An epic title is 1 to 4 words."
    new = not name or not store.find_epic(name)
    orchestrator.assign_epic(m, name, datetime.now(timezone.utc))
    if not name:
        return f"{m.topic[:80]} [{m.slug}] is no longer in an epic."
    title = store.get_epic(store.get_mission(m.slug).epic).title
    return f"{m.topic[:80]} [{m.slug}] is now in the epic {title}" + (" (a new epic)." if new else ".")

@server.tool()
def setup_status() -> str:
    """This machine's setup, as the Settings page's Setup section shows it: a model for each worker and whether it
    answers, the web search key, npx and uvx for the research tools, and something to work on. Each item not done
    says how to fix it. Call this when the human asks how to set up, or why nothing is happening."""
    found = doctor.checks()
    lines = [("done: " if c.ok else "TO DO: ") + f"{c.title}: {c.detail}" + (f" Fix: {c.fix}" if c.fix else "")
             for c in found]
    waiting = doctor.waiting() if orchestrator.loop_running() else None
    if waiting:
        lines.append(f"The loop is running but waiting: {waiting['reason']}")
    lines.append("Settings are on the Settings page (/settings); the home page's Get started card walks through them."
                 if not doctor.done(found) else "Everything is set up.")
    return "\n".join(lines)

# ---------- everything the Theorist can do ----------

def _now() -> datetime:
    return datetime.now(timezone.utc)

def _wake() -> None:
    if orchestrator.loop_running():
        orchestrator.WAKE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        orchestrator.WAKE_FLAG.touch()

def _slugs(text: str) -> list[str]:
    return [s for s in dict.fromkeys(p.strip() for p in text.replace(";", ",").split(",")) if s]

def _dropped(notes: list[str]) -> str:
    return "Not done: " + "; ".join(n.split(": ", 1)[-1] for n in notes) + "."

@server.tool()
def desk_overview() -> str:
    """The whole desk as the Theorist reads it: the inspiration line and how it got here, every epic with its latest
    missions, the missions in no epic, the latest published articles with the possibilities they left open, and the
    links and combines so far (and the ones the human turned down). Use it to look for patterns across missions."""
    line = store.get_inspiration() or "(none; the finder and the Theorist are off)"
    return (f"Inspiration line: {line}\nHow it got here:\n{theorist.history_text(store.inspiration_history())}\n\n"
            + theorist.desk_text(store.all_missions()))

@server.tool()
def set_inspiration(line: str, why: str = "") -> str:
    """Change the inspiration line the mission finder searches around and the Theorist evolves, like the box on
    the UI home page. Only when the human asks. Keep it to 1 to 3 questions, at most 500 characters, and evolve the
    human's line rather than replace it unless they ask for a new one. why: one sentence, kept in the line's
    history. An empty line turns the finder and the Theorist off."""
    line = " ".join(line.split())
    if len(line) > theorist.MAX_LINE:
        return f"That line is {len(line)} characters; at most {theorist.MAX_LINE}."
    before = store.get_inspiration()
    if line == before:
        return "The line is already that."
    store.set_inspiration(line, by="chat", why=" ".join(why.split())[:theorist.MAX_WHY])
    _wake()
    advice = theorist.line_problems(line, before) if before and line else []
    note = f" (the Theorist would not have written it: {'; '.join(advice)})" if advice else ""
    return (f"Inspiration line is now: {line}{note}" if line
            else "Inspiration line cleared: the mission finder and the Theorist are off.")

@server.tool()
def ask_theorist() -> str:
    """Ask for the Theorist's turn: it looks across the desk and its library next time the loop works, without
    waiting out its usual 6 hours, like the UI's Ask the Theorist button."""
    if not store.get_inspiration():
        return "The Theorist needs an inspiration line first (set_inspiration)."
    orchestrator.THINK_FLAG.parent.mkdir(parents=True, exist_ok=True)
    orchestrator.THINK_FLAG.touch()
    _wake()
    return ("Asked. The Theorist goes next while the loop runs; read theorist_notes after." if orchestrator.loop_running()
            else "Asked. The loop is off; the Theorist goes first when it starts.")

@server.tool()
def propose_epic(title: str, missions: str, why: str = "") -> str:
    """Propose a new epic, as the Theorist does: a theme emerging across missions or a strand that has grown into a
    line of inquiry of its own. title: 1 to 3 words, not an existing epic's. missions: the slugs of at least 2
    existing missions that belong in it, separated by commas; they move there now, and go back to their old epic if
    the human rejects it. Missions an agent is working on right now stay put. why: one sentence."""
    notes: list[str] = []
    e = theorist.settle_epic(theorist.NewEpic(title=title, why=why, missions=_slugs(missions)),
                             store.all_missions(), notes)
    if not e:
        return _dropped(notes)
    try:
        epic, moved = orchestrator.propose_epic(e.title, e.missions, e.why, _now(), "chat",
                                                {w["slug"] for w in _working()})
    except ValueError as err:
        return f"Not done: {err}."
    orchestrator._drop_empty_proposals()     # as apply_theory does: an epic whose missions just moved away goes
    return (f"Proposed the epic {epic.title} with {moved} mission(s); it waits for the human's approval on the UI "
            f"home page" + (" (auto mode approves it shortly)." if settings.auto_mode() else "."))

@server.tool()
def propose_combine(missions: str, topic: str, objective: str, epic: str = "", why: str = "") -> str:
    """Propose folding 2 to 4 missions that are really one question into one bigger mission, as the Theorist does.
    Only missions queued, researching, with a brief in review, or parked. missions: their slugs, separated by commas.
    topic: one line, as a question. objective: what each brings and what evidence would answer it. epic: optional
    title. why: one sentence. It waits for the human; approving stops the missions and queues the bigger one."""
    notes: list[str] = []
    c = theorist.settle_combine(theorist.NewCombine(missions=_slugs(missions), topic=topic, objective=objective,
                                                    epic=epic, why=why), store.all_missions(), [], notes)
    if not c:
        return _dropped(notes)
    try:
        made = orchestrator.propose_combine(c, _now(), "chat")
    except ValueError as err:
        return f"Not done: {err}."
    return (f"Proposed combining {', '.join(made.members)} into: {made.topic}. It waits for the human under Needs "
            f"you" + (" (auto mode approves it shortly)." if settings.auto_mode() else "."))

@server.tool()
def link_missions(mission: str, to: str, why: str) -> str:
    """Link two missions that should stay separate but bear on each other, as the Theorist does, so each one's
    research sees the other. mission: an open (unpublished) mission's slug. to: another open mission's slug, or a
    published article's. why: one sentence naming the shared thread (actor, mechanism, number, date, money trail).
    The human can unlink it from the mission's page."""
    notes: list[str] = []
    kept = theorist.settle_links([theorist.NewLink(mission=mission, to=to, why=why)], store.all_missions(), None, notes)
    if not kept:
        return _dropped(notes)
    k = kept[0]
    try:
        orchestrator.link(k.mission, k.to, k.why, _now(), by="chat")
    except ValueError as err:
        return f"Not done: {err}."
    return f"Linked {k.mission} <-> {k.to}: {k.why}"

if __name__ == "__main__":
    server.run()
