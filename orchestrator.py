from datetime import timedelta
from pathlib import Path
import agents                              # switchboard: see agents/__init__.py
import store
from contracts import Mission, State, ResearchBrief, OutreachRecord
from render import brief_markdown

STOP_FLAG = Path("data/STOP")

def _brief(m): return ResearchBrief(**store.load_artifact(m.slug, "brief"))
def _outreach(m): return [OutreachRecord(**r) for r in store.load_artifact(m.slug, "outreach") or []]

def _write_brief_md(m: Mission, brief: ResearchBrief) -> Path:
    path = Path("missions") / m.slug / "brief.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(brief_markdown(m.slug, brief))
    return path

def step(m: Mission, now) -> str | None:
    """Run at most one task. Return a note if the mission changed, None if blocked."""
    if m.state == State.QUEUED:
        m.state = State.RESEARCHING
        return "started"

    if m.state == State.RESEARCHING:
        m.attempts += 1
        try:
            brief = agents.research(m)
            ok, notes = agents.validate(m, brief)
        except Exception as e:             # model, tool or network failure counts as a failed attempt
            brief, ok, notes = None, False, f"research crashed: {type(e).__name__}: {e}"
        if ok:
            store.save_artifact(m.slug, "brief", brief)
            path = _write_brief_md(m, brief)
            m.validator_notes = ""
            m.state = State.BRIEF_READY
            return f"brief passed on attempt {m.attempts}; review {path}"
        m.validator_notes = notes
        if m.attempts >= m.max_attempts:
            m.state = State.PARKED
            return f"parked after {m.attempts} attempts"
        return f"attempt {m.attempts} failed validation"

    if m.state == State.BRIEF_READY:
        if not m.approved:
            return None                    # blocked on human
        records = agents.interview(m, _brief(m), now)
        store.save_artifact(m.slug, "outreach", records)
        m.wait_until = now + timedelta(hours=m.wait_hours)
        m.state = State.AWAITING_COMMENTS
        return f"asked {len(records)} POCs; waiting until {m.wait_until:%Y-%m-%d %H:%M} UTC"

    if m.state == State.AWAITING_COMMENTS:
        store.save_artifact(m.slug, "outreach", agents.check_inbox(m, _outreach(m), now))
        if now < m.wait_until:
            return None                    # blocked on clock
        m.state = State.WRITING
        return "wait over"

    if m.state == State.WRITING:
        path = agents.write(m, _brief(m), _outreach(m))
        m.state = State.ARTICLE_READY
        return f"article saved to {path}"

    if m.state == State.ARTICLE_READY:
        if not m.reviewed:
            return None                    # blocked on human
        m.state = State.DONE
        return "done"

    return None

def reject(m: Mission, notes: str, now) -> None:
    """Human says no to a brief: back to research with the feedback, fresh attempt budget."""
    if m.state != State.BRIEF_READY:
        raise ValueError(f"{m.slug} is {m.state.value}; only BRIEF_READY can be rejected")
    before = m.state
    m.state, m.attempts, m.approved = State.RESEARCHING, 0, False
    m.validator_notes = f"Human reviewer feedback: {notes}"
    store.save_mission(m)
    store.log_event(m.slug, now, before.value, m.state.value, "rejected by human")

def requeue(m: Mission, now) -> None:
    """Revive a parked mission. Keeps the last validator notes so research can learn from them."""
    if m.state != State.PARKED:
        raise ValueError(f"{m.slug} is {m.state.value}; only PARKED can be requeued")
    before = m.state
    m.state, m.attempts = State.QUEUED, 0
    store.save_mission(m)
    store.log_event(m.slug, now, before.value, m.state.value, "requeued by human")

def tick(now) -> int:
    changed = 0
    for m in store.active_missions():
        if STOP_FLAG.exists():
            break                          # stop between tasks, never mid-task
        before = m.state
        note = step(m, now)
        if note is not None:
            store.save_mission(m)          # saved after every task = resumable
            store.log_event(m.slug, now, before.value, m.state.value, note)
            changed += 1
    return changed

def run_until_idle(now_fn, max_ticks: int = 100) -> int:
    total = 0
    for _ in range(max_ticks):
        if STOP_FLAG.exists():
            break
        n = tick(now_fn())
        total += n
        if n == 0:
            break
    return total
