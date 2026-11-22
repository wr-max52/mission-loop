"""Local MCP server that lets the chat assistant see what the loop is doing, without touching it.

Every tool only reads: the database, data/activity.json, the agents' trails (trail.py) and the loop log.
None of them changes a mission, takes the loop's lock or talks to a model, so asking never slows or
redirects the work. Answers are short on purpose: the assistant has a small context.
The chat assistant starts this through the "loop" entry in mcp.json; any other MCP client can too.
"""
import time
from datetime import datetime, timezone
from pathlib import Path
from mcp.server.mcpserver import MCPServer
import activity, orchestrator, store, trail
from contracts import State, words

LOG = Path("data/loop.log")
MAX_LIST = 25
MAX_NOTES = 300

LABELS = {
    State.QUEUED: "queued", State.RESEARCHING: "researching", State.BRIEF_READY: "brief ready for human review",
    State.AWAITING_COMMENTS: "waiting for comments", State.WRITING: "writing",
    State.ARTICLE_READY: "article ready for human review", State.DONE: "published", State.PARKED: "parked (gave up)",
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

def _working() -> dict | None:
    """The agent working this moment. The record only counts while the loop holds its lock."""
    return activity.current() if orchestrator.loop_running() else None

def _label(m) -> str:
    if m.state == State.BRIEF_READY and m.approved:
        return "brief approved, article next"
    if m.state == State.ARTICLE_READY and m.reviewed:
        return "approved, publishing next pass"
    return LABELS[m.state]

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
    lines = ["Loop: " + ("stopping after its current task" if running and orchestrator.STOP_FLAG.exists()
                         else "running" if running else "off (no agent is working)")]
    now = _working()
    if now:
        who = activity.AGENTS[now["agent"]]
        if now["slug"]:
            m = store.get_mission(now["slug"])
            lines.append(f"Working now: {who} on {now['slug']} ({m.topic if m else '?'}), "
                         f"for {_ago(time.time() - now['since'])}")
        else:
            lines.append(f"Working now: {who}, looking for new missions, for {_ago(time.time() - now['since'])}")
    elif running:
        lines.append("Working now: nothing; the loop is waiting for its next pass or for a human")
    counts: dict[str, int] = {}
    missions = store.all_missions()
    for m in missions:
        counts[_label(m)] = counts.get(_label(m), 0) + 1
    lines.append("Missions: " + (", ".join(f"{n} {k}" for k, n in counts.items()) or "none"))
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
        missions = [m for m in missions if m.state not in (State.DONE, State.PARKED)]
    elif stage == "waiting":
        missions = [m for m in missions if _needs_human(m)]
    elif stage != "all":
        missions = [m for m in missions if stage in (m.state.value.lower(), LABELS[m.state])
                    or LABELS[m.state].startswith(stage)]
    if not missions:
        return f"No missions match '{stage}'."
    now = _working()
    lines = []
    for m in missions[-MAX_LIST:]:
        events = store.events(m.slug)
        changed = _since(events[-1]["at"]) if events else "?"
        busy = f" [{activity.AGENTS[now['agent']]} working on it now]" if now and now["slug"] == m.slug else ""
        lines.append(f"- {m.slug} | {_label(m)} | {m.topic[:90]} | changed {changed}{busy}")
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
    if m.state in (State.RESEARCHING, State.QUEUED) or m.attempts:
        lines.append(f"Research attempts: {m.attempts} of {m.max_attempts}")
    if m.state == State.WRITING or m.write_attempts:
        lines.append(f"Writing attempts: {m.write_attempts} of {m.max_attempts}")
    now = _working()
    if now and now["slug"] == m.slug:
        lines.append(f"Working on it now: {activity.AGENTS[now['agent']]}, for {_ago(time.time() - now['since'])}")
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
    Use slug "finder" for the mission finder. Read-only: it never interrupts the agent."""
    slug = slug.strip()
    key = "" if slug.lower() in ("finder", "mission finder", "") else slug
    if key and not store.get_mission(key):
        return f"No mission '{slug}'. Use search_missions to find its slug."
    steps = trail.recent(key, max(1, min(int(last), 15)))
    if not steps:
        return f"No recorded steps for {slug or 'the finder'} yet."
    now = time.time()
    return "\n".join(f"- {_ago(now - s['at'])} ago, {activity.AGENTS.get(s['agent'], s['agent'])}, "
                     f"{s['kind']}: {s['text'][:240]}" for s in steps)

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
    return "\n".join(f"- {m.slug} | {_label(m)} | {m.topic[:90]}" for _, m in scored[:8])

if __name__ == "__main__":
    server.run()
