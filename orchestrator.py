from datetime import timedelta
from pathlib import Path
import activity
import agents                              # switchboard: see agents/__init__.py
import store
from contracts import Mission, State, ResearchBrief, RejectedBrief, RejectedArticle, OutreachRecord, Article
from render import brief_markdown, article_markdown, article_html

STOP_FLAG = Path("data/STOP")
WAKE_FLAG = Path("data/WAKE")             # the UI touches this so a sleeping loop runs a pass now
LOCK_FILE = Path("data/loop.lock")         # held by whichever run/loop is working

def _brief(m): return ResearchBrief(**store.load_artifact(m.slug, "brief"))
def _outreach(m): return [OutreachRecord(**r) for r in store.load_artifact(m.slug, "outreach") or []]
def _article(m): return Article(**store.load_artifact(m.slug, "article"))

def _write_brief_md(m: Mission, brief: ResearchBrief) -> Path:
    path = Path("missions") / m.slug / "brief.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(brief_markdown(m.slug, brief, checked=not m.unchecked_brief))
    return path

def _write_article_review(m: Mission, brief: ResearchBrief, article: Article) -> Path:
    """article.md plus preview.html: what the human reads before `review`."""
    folder = Path("missions") / m.slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "article.md").write_text(article_markdown(m.slug, brief, article))
    (folder / "preview.html").write_text(article_html(brief, article, published_at=None,
                                                      brief_checked=not m.unchecked_brief,
                                                      article_checked=not m.unchecked_article))
    return folder / "preview.html"

def step(m: Mission, now) -> str | None:
    """Run at most one task. Return a note if the mission changed, None if blocked."""
    if m.state == State.QUEUED:
        m.state = State.RESEARCHING
        return "started"

    if m.state == State.RESEARCHING:
        m.attempts += 1
        try:
            activity.begin(m.slug, "sources")
            brief = agents.research(m, now)
            activity.begin(m.slug, "source_check")
            ok, notes = agents.validate(m, brief)
        except Exception as e:             # model, tool or network failure counts as a failed attempt
            brief, ok, notes = None, False, f"research crashed: {type(e).__name__}: {e}"
        if ok:
            store.save_artifact(m.slug, "brief", brief)
            m.unchecked_brief = ""
            path = _write_brief_md(m, brief)
            m.validator_notes = ""
            m.rejected_brief = None
            m.state = State.BRIEF_READY
            return f"brief passed on attempt {m.attempts}; review {path}"
        m.validator_notes = notes
        if brief is not None:              # keep the failed brief for the next attempt and for humans
            m.rejected_brief = RejectedBrief(brief=brief, notes=notes)
            path = Path("missions") / m.slug / "brief.rejected.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(brief_markdown(m.slug, brief) + f"\n## Validator notes\n{notes}\n")
        if m.attempts >= m.max_attempts:
            m.state = State.PARKED
            return f"parked after {m.attempts} attempts"
        return f"attempt {m.attempts} failed validation"

    if m.state == State.BRIEF_READY:
        if not m.approved:
            return None                    # blocked on human
        records = agents.interview(m, _brief(m), now)
        store.save_artifact(m.slug, "outreach", records)
        # The fixed wait only applies when someone was actually asked; no outreach, nothing to wait for.
        m.wait_until = now + timedelta(hours=m.wait_hours) if records else now
        m.state = State.AWAITING_COMMENTS
        return f"asked {len(records)} POCs; waiting until {m.wait_until:%Y-%m-%d %H:%M} UTC"

    if m.state == State.AWAITING_COMMENTS:
        store.save_artifact(m.slug, "outreach", agents.check_inbox(m, _outreach(m), now))
        if now < m.wait_until:
            return None                    # blocked on clock
        m.state = State.WRITING
        return "wait over"

    if m.state == State.WRITING:
        m.write_attempts += 1
        brief, records = _brief(m), _outreach(m)
        try:
            activity.begin(m.slug, "writer")
            article = agents.write(m, brief, records)
            activity.begin(m.slug, "article_check")
            ok, notes = agents.check_article(m, brief, article, records)
        except Exception as e:             # same rule as research: a crash is a failed attempt
            article, ok, notes = None, False, f"writer crashed: {type(e).__name__}: {e}"
        if ok:
            store.save_artifact(m.slug, "article", article)
            m.unchecked_article = ""
            path = _write_article_review(m, brief, article)
            m.validator_notes = ""
            m.rejected_article = None
            m.state = State.ARTICLE_READY
            return f"article passed check on attempt {m.write_attempts}; review {path}"
        m.validator_notes = notes
        if article is not None:            # keep the failed article for humans, who may push it through
            m.rejected_article = RejectedArticle(article=article, notes=notes)
            path = Path("missions") / m.slug / "article.rejected.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(article_markdown(m.slug, brief, article) + f"\n## Article check notes\n{notes}\n")
        if m.write_attempts >= m.max_attempts:
            m.state = State.PARKED
            return f"parked after {m.write_attempts} writing attempts"
        return f"writing attempt {m.write_attempts} failed check"

    if m.state == State.ARTICLE_READY:
        if not m.reviewed:
            return None                    # blocked on human
        activity.begin(m.slug, "publisher")
        location = agents.publish(m, _brief(m), _article(m), now)
        m.published_at = now
        m.state = State.DONE
        return f"published to {location}"

    return None

def reject(m: Mission, notes: str, now) -> None:
    """Human says no. A brief goes back to research; an article goes back to the writer.
    Either way the feedback is passed on and the attempt budget is fresh."""
    before = m.state
    if m.state == State.BRIEF_READY:
        m.state, m.attempts, m.approved = State.RESEARCHING, 0, False
    elif m.state == State.ARTICLE_READY:
        m.state, m.write_attempts, m.reviewed = State.WRITING, 0, False
    else:
        raise ValueError(f"{m.slug} is {m.state.value}; only BRIEF_READY or ARTICLE_READY can be rejected")
    m.validator_notes = f"Human reviewer feedback: {notes}"
    store.save_mission(m)
    store.log_event(m.slug, now, before.value, m.state.value, "rejected by human")

def parked_while_writing(m: Mission) -> bool:
    return store.load_artifact(m.slug, "outreach") is not None   # brief was approved already

def requeue(m: Mission, now) -> None:
    """Revive a parked mission. Keeps the last notes so the agent can learn from them.
    A mission that parked while writing goes back to WRITING, not back to research."""
    if m.state != State.PARKED:
        raise ValueError(f"{m.slug} is {m.state.value}; only PARKED can be requeued")
    before = m.state
    if parked_while_writing(m):
        m.state, m.write_attempts = State.WRITING, 0
    else:
        m.state, m.attempts = State.QUEUED, 0
    store.save_mission(m)
    store.log_event(m.slug, now, before.value, m.state.value, "requeued by human")

def parked_draft(m: Mission) -> RejectedBrief | RejectedArticle | None:
    """The failed work a human could push through. None if every attempt crashed before producing any."""
    return m.rejected_article if parked_while_writing(m) else m.rejected_brief

def push_through(m: Mission, now) -> None:
    """Human overrides the check: the last failed draft moves on to the next step anyway.
    That step is still the human review, and the draft stays marked as having failed its check."""
    if m.state != State.PARKED:
        raise ValueError(f"{m.slug} is {m.state.value}; only PARKED can be pushed through")
    draft = parked_draft(m)
    if draft is None:
        what = "article" if parked_while_writing(m) else "brief"
        raise ValueError(f"{m.slug} has no failed {what} to push through; requeue it instead")
    before = m.state
    if isinstance(draft, RejectedBrief):
        store.save_artifact(m.slug, "brief", draft.brief)
        m.unchecked_brief, m.rejected_brief, m.approved = draft.notes, None, False
        _write_brief_md(m, draft.brief)
        m.state = State.BRIEF_READY
        note = "failed brief pushed through by human"
    else:
        store.save_artifact(m.slug, "article", draft.article)
        m.unchecked_article, m.rejected_article, m.reviewed = draft.notes, None, False
        _write_article_review(m, _brief(m), draft.article)
        m.state = State.ARTICLE_READY
        note = "failed article pushed through by human"
    m.validator_notes = ""                 # the writer reads these as problems in its own draft
    store.save_mission(m)
    store.log_event(m.slug, now, before.value, m.state.value, note)

def tick(now) -> int:
    changed = 0
    for m in store.active_missions():
        if STOP_FLAG.exists():
            break                          # stop between tasks, never mid-task
        before = m.state
        try:
            note = step(m, now)
        finally:
            activity.clear()
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
