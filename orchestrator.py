import fcntl, hashlib, json, time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from pathlib import Path
import activity, checkpoint
import agents                              # switchboard: see agents/__init__.py
import settings, stopping, store
from agents import bluesky, f916, github, publisher
from agents.channel import NoAllowance, Refused
from contracts import (Evidence, Mission, State, ResearchBrief, RejectedBrief, RejectedArticle, OutreachRecord, Article,
                       ChannelPost, Combine, COMBINABLE, Epic, Link, SITE_FOLDERS, epic_title, is_open, slugify)
from render import brief_markdown, article_markdown, article_html

STOP_FLAG = Path("data/STOP")
WAKE_FLAG = Path("data/WAKE")             # the UI touches this so a resting loop looks for work now
THINK_FLAG = Path("data/THINK")           # the UI or `run.py theorize` touches this: the Theorist's turn comes next
LOCK_FILE = Path("data/loop.lock")         # held by whichever run/loop is working
PUBLISH_LOCK = Path("data/publish.lock")   # held during a publishing pass, the loop's or the UI's, so two never overlap

def loop_running() -> bool:
    """True while a run/loop holds the lock. We only test the lock, never keep it."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False

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
                                                      article_checked=not m.unchecked_article,
                                                      epic=publisher.epic_label(m)))
    return folder / "preview.html"

def role(m: Mission) -> str | None:
    """Which worker role the mission's next task needs: "assistant" (the small model's legwork), "pi" (the
    big model's writing), or None when it needs no model (a quick step) or is blocked."""
    if m.state == State.RESEARCHING:
        return "pi" if m.gathered or m.interrupted_brief else "assistant"
    if m.state == State.WRITING:
        return "pi"
    return None

def upcoming_role(m: Mission) -> str | None:
    """The role of the next model step for a mission in progress, even while it waits (for the UI's visitors)."""
    if m.state == State.QUEUED:
        return "assistant"
    if m.state in (State.BRIEF_READY, State.AWAITING_COMMENTS):
        return "pi"
    return role(m)

def _research_failed(m: Mission, brief: ResearchBrief | None, notes: str) -> str:
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

def _gather(m: Mission, now) -> str:
    """The Research Assistant's half of a research attempt: find and read sources. What it found is saved,
    so the Principal Investigator can write the brief from it now or after a restart. The search saves its
    transcript as it goes; one a stop or a killed loop cut short carries on from it, as the same attempt."""
    saved = store.load_artifact(m.slug, "evidence")
    paused = Evidence(**saved) if saved and not m.gathered else None
    if paused and paused.finished:     # the loop was killed after the search was saved but before it was marked
        m.attempts, m.gathered, m.search_paused = max(m.attempts, paused.attempt, 1), True, False
        return "sources found before the restart are kept; the brief is next"
    if paused:
        m.attempts = max(m.attempts, paused.attempt, 1)   # the same attempt, carried on (a killed loop never saved it)
    else:
        m.attempts += 1                # an attempt starts with its search
    try:
        activity.begin(m.slug, "sources")
        evidence = agents.gather(m, now, paused) if paused else agents.gather(m, now)
    except Exception as e:             # model, tool or network failure counts as a failed attempt
        store.delete_artifact(m.slug, "evidence")   # the next attempt searches afresh
        m.search_paused = False
        return _research_failed(m, None, f"research crashed: {type(e).__name__}: {e}")
    store.save_artifact(m.slug, "evidence", evidence)
    m.search_paused = not evidence.finished
    if not evidence.finished:          # the stop button cut the search short: it carries on next start
        calls = sum(1 for x in evidence.messages if x.get("role") == "tool")
        return f"stopped mid-search after {calls} tool result(s); the search carries on when the loop starts again"
    m.gathered = True
    if stopping.requested():
        return "stopped after finding sources; the brief is written from them when the loop starts again"
    return f"sources found on attempt {m.attempts}; the brief is next"

def _write_brief(m: Mission, now) -> str:
    """The Principal Investigator's half: write the brief from the gathered sources, then check it."""
    m.attempts = max(m.attempts, 1)    # a draft saved by a stop before attempts were counted this way
    brief, m.interrupted_brief = m.interrupted_brief, None   # a stop's draft: check it, don't redo it
    try:
        if brief is None:
            activity.begin(m.slug, "brief")
            evidence = Evidence(**(store.load_artifact(m.slug, "evidence") or {}))
            brief = agents.write_brief(m, evidence, now)
            if stopping.requested():   # finished, but leave checking it for the next start
                raise stopping.Interrupted(brief)
        activity.begin(m.slug, "source_check")
        ok, notes = agents.validate(m, brief)
    except stopping.Interrupted as e:  # a stopped attempt isn't a failed one
        m.interrupted_brief = e.draft
        if e.draft:                    # the draft replaces the sources; without one, the brief is rewritten from them
            _drop_evidence(m)
        saved = ("its draft is checked" if e.draft else
                 "its checked sources are kept and the claims are written" if m.brief_sources else
                 "its picked sources are kept and checked" if m.brief_pick else
                 "the brief is written again from the same sources")
        return f"stopped mid-brief; {saved} when the loop starts again"
    except Exception as e:             # a crash is a failed attempt; the next one searches again
        ok = False                         # a brief that can't pass (too few sources held up) brings its draft
        brief, notes = getattr(e, "brief", None), getattr(e, "notes", "") or f"research crashed: {type(e).__name__}: {e}"
    _drop_evidence(m)
    if ok:
        store.save_artifact(m.slug, "brief", brief)
        m.unchecked_brief = ""
        path = _write_brief_md(m, brief)
        m.validator_notes = ""
        m.rejected_brief = None
        m.state = State.BRIEF_READY
        return f"brief passed on attempt {m.attempts}; review {path}"
    return _research_failed(m, brief, notes)

def _drop_evidence(m: Mission) -> None:
    """The attempt is over: its search transcript and the brief's saved first step go."""
    m.gathered, m.search_paused, m.brief_pick, m.brief_sources = False, False, None, None
    store.delete_artifact(m.slug, "evidence")

def step(m: Mission, now) -> str | None:
    """Run at most one task. Return a note if the mission changed, None if blocked."""
    if m.state == State.QUEUED:
        m.state = State.RESEARCHING
        return "started"

    if m.state == State.RESEARCHING:
        if m.gathered or m.interrupted_brief:
            return _write_brief(m, now)
        return _gather(m, now)

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
        article, m.interrupted_article = m.interrupted_article, None
        try:
            if article is None:
                activity.begin(m.slug, "writer")
                article = agents.write(m, brief, records)
                if stopping.requested():
                    raise stopping.Interrupted(article)
            activity.begin(m.slug, "article_check")
            ok, notes = agents.check_article(m, brief, article, records)
        except stopping.Interrupted as e:
            m.write_attempts -= 1
            m.interrupted_article = e.draft
            saved = ("its draft is checked" if e.draft else
                     f"writing resumes after its {len(m.article_draft.sections)} saved section(s)" if m.article_draft
                     else "nothing usable was written; writing restarts")
            return f"stopped mid-writing; {saved} when the loop starts again"
        except Exception as e:             # same rule as research: a crash is a failed attempt
            article, ok, notes = None, False, f"writer crashed: {type(e).__name__}: {e}"
        if ok:
            store.save_artifact(m.slug, "article", article)
            m.unchecked_article, m.article_draft = "", None
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
        m.channels["site"] = ChannelPost(at=now, where=str(location))
        m.state = State.DONE
        return f"published to {location}"   # other channels pick it up right after

    return None

def reject(m: Mission, notes: str, now) -> None:
    """Human says no. A brief goes back to research; an article goes back to the writer.
    Either way the feedback is passed on and the attempt budget is fresh."""
    before = m.state
    if m.state == State.BRIEF_READY:
        m.state, m.attempts, m.approved = State.RESEARCHING, 0, False
    elif m.state == State.ARTICLE_READY:
        m.state, m.write_attempts, m.reviewed = State.WRITING, 0, False
        m.article_draft = None             # written again from the feedback, not patched
    else:
        raise ValueError(f"{m.slug} is {m.state.value}; only BRIEF_READY or ARTICLE_READY can be rejected")
    m.validator_notes = f"Human reviewer feedback: {notes}"
    store.save_mission(m)
    store.log_event(m.slug, now, before.value, m.state.value, "rejected by human")

def auto_approve(now, busy: set[str] = frozenset()) -> int:
    """Auto mode: approve every brief waiting for review, every proposed epic and every proposed combine, as a human
    would. Articles are left alone: publishing always waits for a human. An epic or combine with a mission a worker
    is busy with waits for a later sweep, so no worker saves over the move. Returns how many briefs were approved
    and combined missions made (epics move no mission on)."""
    if not settings.auto_mode():
        return 0
    approved = 0
    for m in store.all_missions():
        if m.state == State.BRIEF_READY and not m.approved:
            m.approved = True
            store.save_mission(m)
            store.log_event(m.slug, now, m.state.value, m.state.value, "brief approved by auto mode")
            approved += 1
    for e in store.all_epics("proposed"):
        if not any(m.slug in busy for m in _in_epic(e.slug)):
            approve_epic(e.slug, "", now, by="auto mode")
    for c in store.all_combines("proposed"):
        if not set(c.members) & set(busy):
            try:
                approve_combine(c.slug, now, by="auto mode")
                approved += 1
            except ValueError as e:        # its missions moved on meanwhile: the proposal is dropped
                print(f"combine {c.slug}: {e}", flush=True)
    return approved

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
        m.article_draft = None
        _write_article_review(m, _brief(m), draft.article)
        m.state = State.ARTICLE_READY
        note = "failed article pushed through by human"
    m.validator_notes = ""                 # the writer reads these as problems in its own draft
    store.save_mission(m)
    store.log_event(m.slug, now, before.value, m.state.value, note)
    auto_approve(now)                      # auto mode approves a pushed-through brief too, never an article

def _record(m: Mission, before: State, note: str | None, now) -> int:
    if note is None:
        return 0
    store.save_mission(m)              # saved after every task = resumable
    store.log_event(m.slug, now, before.value, m.state.value, note)
    return 1

def _broke(m: Mission, before: State, e: Exception, now, failed: set[str]) -> None:
    """A step raised. It is logged on the mission and skipped until the loop starts again; the rest go on."""
    failed.add(m.slug)
    why = f"{before.value} step failed: {type(e).__name__}: {e}"
    print(f"{m.slug}: {why}", flush=True)
    store.log_event(m.slug, now, before.value, before.value, why)

# Farthest along first: whatever is closest to being published goes before what has only just started.
FLOW = [State.ARTICLE_READY, State.WRITING, State.AWAITING_COMMENTS, State.BRIEF_READY,
        State.RESEARCHING, State.QUEUED]

def in_flow_order(missions: list[Mission]) -> list[Mission]:
    """Farthest along first (a brief from gathered sources before a new search); within a stage, oldest first."""
    return sorted((m for m in missions if m.state in FLOW),
                  key=lambda m: (FLOW.index(m.state), role(m) != "pi"))

def _quick_steps(m: Mission, now_fn, failed: set[str]) -> int:
    """Steps that need no model (start, outreach, the comment wait, publishing) run right away, in order."""
    changed = 0
    while role(m) is None and m.state not in (State.DONE, State.PARKED, State.COMBINED) and not STOP_FLAG.exists():
        before = m.state
        try:
            note = step(m, now_fn())
        except Exception as e:
            _broke(m, before, e, now_fn(), failed)
            break
        finally:
            activity.clear()
        if not _record(m, before, note, now_fn()):
            break
        changed += 1
    return changed

PROGRESS = ("brief_pick", "brief_sources", "article_draft")   # Mission fields an agent saves mid-task

def save_progress(m: Mission, artifacts: dict) -> None:
    """A checkpoint from inside a task (checkpoint.save): the artifacts given, and the mission's progress
    fields written onto the stored mission. Only those fields: the rest of the mission is saved when the task
    ends, as always, so a checkpoint never writes half a task's changes."""
    for kind, obj in artifacts.items():
        store.save_artifact(m.slug, kind, obj)
    stored = store.get_mission(m.slug)
    if stored is not None and any(getattr(stored, f) != getattr(m, f) for f in PROGRESS):
        store.save_mission(stored.model_copy(update={f: getattr(m, f) for f in PROGRESS}))

def _as_worker(seat: str, fn, *args):
    """Run one task on a worker's thread, so its activity shows under that seat (a copy of a worker) and its
    progress is saved."""
    activity.work_as(seat)
    checkpoint.use(save_progress)
    try:
        return fn(*args)
    finally:
        checkpoint.use(None)
        activity.clear()

def _pick(worker: str, waiting: list[Mission], turns: set[tuple[str, str]]) -> Mission | None:
    """The farthest-along mission for this worker that hasn't had its turn this round. Once every one has,
    a new round starts, so a mission whose attempt failed waits while the others get a go. Every copy of a
    worker shares its rounds. A mission a human rushed ("Add and start loop") goes ahead of the rest."""
    mine = [m for m in waiting if settings.worker_for(role(m)) == worker]
    fresh = [m for m in mine if (worker, m.slug) not in turns]
    if mine and not fresh:
        turns.difference_update((worker, m.slug) for m in mine)
        fresh = mine
    fresh.sort(key=lambda m: not m.rush)   # stable: otherwise farthest along first
    return fresh[0] if fresh else None

PASS_EVERY = 300                           # seconds: during long model work, a publishing pass this often, so a
                                           # backlog goes out as each channel's allowance frees up (Bluesky's gap)
MAX_TASKS = 500                            # per run: a safety net against a mission that never settles

def work(now_fn, max_tasks: int = MAX_TASKS, after_task=None) -> int:
    """Run every mission until each is blocked, with the two workers side by side, each as many copies (seats) as
    its count on the Settings page, read each sweep. Missions are read fresh
    every few seconds, and the order is the same each time:
    1. Every step that needs no model, farthest along first (publish a reviewed article, move an approved brief
       on, ...). Right after the first sweep, whenever one publishes something, whenever a human asks to
       publish an article again and every PASS_EVERY seconds, there is a publishing pass (post_to_channels):
       each channel works through its backlog as far as its allowance goes.
    2. Each idle worker takes the farthest-along mission for its role that hasn't had a turn this round, so
       writing goes before research, and a brief a human approves mid-task is next in line.
    A worker that is off hands its role to the other, which then does everything one task at a time. The
    Theorist, when it is due, is the Principal Investigator's first task, and the finder waits for it so it
    searches around the evolved line. The mission finder runs once, when the Research Assistant's role has
    nothing else to do, and whatever it finds is worked on straight away. after_task, if given, runs after each
    mission's model task. Returns how many changes."""
    changed = tasks = 0
    finder_done, theorist_done, first = False, False, True
    failed: set[str] = set()               # steps that raised: skipped until the loop starts again
    seen: set = set()                      # publish requests the last publishing pass already tried
    last_pass = time.monotonic()
    turns: set[tuple[str, str]] = set()    # (worker, slug): model turns taken this round
    running: dict = {}                     # future -> (seat, mission, its state before) or (seat, None, desk task)
    POLL = 5                               # seconds: a mission a human approves mid-task gets the idle worker soon
    seats = len(settings.WORKERS) * settings.MAX_COUNT       # threads start only as seats need them
    with ThreadPoolExecutor(max_workers=seats, thread_name_prefix="worker") as pool:
        while True:
            if not STOP_FLAG.exists() and tasks < max_tasks:
                busy = {m.slug for _, m, _ in running.values() if m}
                changed += auto_approve(now_fn(), busy)
                missions = in_flow_order([m for m in store.active_missions()
                                          if m.slug not in busy and m.slug not in failed])
                for m in missions:
                    changed += _quick_steps(m, now_fn, failed)
                asked, due = requests(), time.monotonic() - last_pass >= PASS_EVERY
                if first or due or any(m.state == State.DONE for m in missions) or asked - seen:
                    changed += post_to_channels(now_fn())
                    first, seen = False, requests()   # a request that failed now waits for the next pass
                    last_pass = time.monotonic()
                waiting = [m for m in missions if m.slug not in failed and role(m)]
                taken = {w for w, _, _ in running.values()}
                if not theorist_done and not theorist_due(now_fn()):
                    theorist_done = True       # checked once a run; a THINK flag set later waits for the next run
                for seat in settings.seats():
                    if seat in taken:
                        continue
                    worker = settings.seat_worker(seat)
                    if not theorist_done and settings.worker_for("pi") == worker:
                        theorist_done = True
                        running[pool.submit(_as_worker, seat, think, now_fn())] = (seat, None, "theorist")
                        continue
                    m = _pick(worker, waiting, turns)
                    if m:
                        waiting.remove(m)
                        turns.add((worker, m.slug))
                        m.rush = False         # it has its turn; saved with the step's result
                        running[pool.submit(_as_worker, seat, step, m, now_fn())] = (seat, m, m.state)
                        tasks += 1
                    elif not finder_done and theorist_done and settings.worker_for("assistant") == worker \
                            and not any(role(m) == "assistant" for m in waiting) \
                            and "theorist" not in (task for _, _, task in running.values()):
                        finder_done = True
                        running[pool.submit(_as_worker, seat, find_new_missions, now_fn())] = (seat, None, "finder")
            if not running:
                return changed
            done, _ = wait(running, timeout=POLL, return_when=FIRST_COMPLETED)
            for future in done:
                worker, m, before = running.pop(future)
                try:
                    result = future.result()
                except Exception as e:     # step() handles model failures itself; this is anything else
                    if m is None:
                        raise
                    _broke(m, before, e, now_fn(), failed)
                    continue
                if before == "theorist":   # applied here, between tasks, so no worker saves over its changes
                    busy = {m.slug for _, m, _ in running.values() if m}
                    changed += apply_theory(result, now_fn(), busy) if result else 0
                    continue
                if m is None:
                    changed += result
                    continue
                changed += _record(m, before, result, now_fn())
                if after_task:
                    after_task()

def _new_slug(topic: str, taken: set[str]) -> str:
    base = slugify(topic)[:54].strip("-") or "mission"
    slug, n = base, 2
    while slug in taken or slug in SITE_FOLDERS:
        slug, n = f"{base}-{n}", n + 1
    return slug

MIN_NEW_EPIC_MISSIONS = 2                  # the finder may only propose an epic that already has 2+ missions
EPIC_TOPICS_SHOWN = 5                      # recent topics the finder sees per epic, so it knows what each holds

def _finder_epics(missions: list[Mission]) -> dict[str, list[str]]:
    return {e.title: [m.topic for m in missions if m.epic == e.slug][-EPIC_TOPICS_SHOWN:]
            for e in store.all_epics("active")}

def _settle_epic(idea, proposals: set[str]) -> tuple[str, str, str]:
    """(epic, fallback, note) for a found mission. It always gets the best existing epic the finder named;
    a new epic is proposed only when enough ideas from this search share it, and waits for the human."""
    fit = store.find_epic(idea.epic)
    fit = fit.slug if fit and fit.status == "active" else ""
    new = epic_title(idea.new_epic)
    if not new or new.lower() not in proposals:
        return fit, "", ""
    epic = store.create_epic(new, status="proposed")
    if epic.status == "active":            # it named an existing epic after all
        return epic.slug, "", ""
    return epic.slug, fit, f"; proposed new epic: {epic.title}"

def find_new_missions(now) -> int:
    """The mission finder's turn. It needs an inspiration line, and its failures never hold up the loop."""
    inspiration = store.get_inspiration()
    if not inspiration or STOP_FLAG.exists():
        return 0
    missions = store.all_missions()
    try:
        activity.begin("", "finder")
        ideas = agents.find_missions(inspiration, [m.topic for m in missions], now, _finder_epics(missions))
    except Exception as e:
        print(f"mission finder failed: {type(e).__name__}: {e}", flush=True)
        return 0
    finally:
        activity.clear()
    taken = {m.slug for m in missions}
    named = [epic_title(i.new_epic).lower() for i in ideas if epic_title(i.new_epic)]
    proposals = {n for n in named if named.count(n) >= MIN_NEW_EPIC_MISSIONS}
    for idea in ideas:
        slug = _new_slug(idea.topic, taken)
        taken.add(slug)
        epic, fallback, note = _settle_epic(idea, proposals)
        store.save_mission(Mission(slug=slug, topic=idea.topic.strip(), epic=epic, epic_fallback=fallback,
                                   objective=f"{idea.objective.strip()}\n\nStarting lead: {idea.source_url.strip()}"))
        store.log_event(slug, now, "", State.QUEUED.value, f"found by the mission finder from: {inspiration}{note}")
    print(f"mission finder added {len(ideas)} mission(s)", flush=True)
    return len(ideas)

# ---------- the Theorist: looks across the desk, evolves the line ----------

THEORY_GAP = timedelta(hours=6)            # at most one turn this often, unless a human asks (THINK_FLAG)

def desk_fingerprint() -> str:
    """What the Theorist looks across: which missions exist, their epics, which are published, and the epics.
    A mission merely moving through research doesn't count as news."""
    missions = [(m.slug, m.epic, m.state == State.DONE, m.state == State.COMBINED) for m in store.all_missions()]
    epics = [(e.slug, e.title, e.status) for e in store.all_epics()]
    links = [(k.a, k.b, k.status) for k in store.all_links(None)]
    combines = [(c.slug, c.status) for c in store.all_combines()]
    return hashlib.sha1(json.dumps([missions, epics, links, combines]).encode()).hexdigest()

def theorist_due(now) -> bool:
    """Its turn comes when it has a line to evolve and a desk to look at, the desk changed since its last
    turn, and THEORY_GAP has passed. A human asking (THINK_FLAG) skips the wait."""
    if not store.get_inspiration() or STOP_FLAG.exists():
        return False
    if THINK_FLAG.exists():
        return True
    if not store.all_missions():
        return False
    last = store.last_theory()
    if not last:
        return True
    if last.get("fingerprint") == desk_fingerprint():
        return False
    try:
        return now - datetime.fromisoformat(last["at"]) >= THEORY_GAP
    except (KeyError, TypeError, ValueError):
        return True

def think(now):
    """The Theorist's turn, on a worker's thread. It only reads; apply_theory makes the changes. A failure is
    noted, so it waits THEORY_GAP before trying again, and never holds up the loop."""
    THINK_FLAG.unlink(missing_ok=True)
    try:
        activity.begin("", "theorist")
        return agents.theorize(now)
    except Exception as e:
        why = f"{type(e).__name__}: {e}"
        print(f"theorist failed: {why}", flush=True)
        store.save_theory({"at": now.isoformat(), "fingerprint": desk_fingerprint(), "failed": why})
        return None
    finally:
        activity.clear()

def apply_theory(theory, now, busy: set[str] = frozenset()) -> int:
    """Make the Theorist's changes, on the main thread between tasks: the evolved line, its proposed epics (they
    wait for the human like the finder's, and each mission moved keeps its old epic to go back to), its one
    new mission, its combine (proposed: it waits for the human too) and its links (made right away: they change
    no mission, only what each one's research sees). A human edit to the line since the Theorist read it wins.
    Missions being worked on right now stay where they are. Returns how many changes."""
    changed, notes = 0, list(theory.dropped)
    before = store.get_inspiration()
    if theory.inspiration != before:
        if before and before == theory.based_on:
            store.set_inspiration(theory.inspiration, by="theorist", why=theory.why)
            changed += 1
        else:
            notes.append("line kept: the editor changed it while the Theorist was thinking")

    homes, epics = {}, []                  # new epic title (lowercase) -> slug, for the new mission
    joins = epic_title(theory.new_mission.epic).lower() if theory.new_mission else ""
    for e in theory.new_epics:
        try:
            epic, moved = propose_epic(e.title, e.missions, e.why, now, "theorist", busy,
                                       extra=int(joins == e.title.lower()))
        except ValueError as why:
            notes.append(f"new epic dropped: {why}")
            continue
        homes[e.title.lower()] = epic.slug
        epics.append(epic.title)
        changed += moved

    slug = ""
    if theory.new_mission:
        idea, missions = theory.new_mission, store.all_missions()
        home = store.find_epic(idea.epic) if idea.epic else None
        epic = homes.get(joins) or (home.slug if home and home.status == "active" else "")
        slug = _new_slug(idea.topic, {m.slug for m in missions})
        store.save_mission(Mission(slug=slug, topic=idea.topic, epic=epic,
                                   objective=f"{idea.objective}\n\nProposed by the Theorist from the desk's own work."))
        store.log_event(slug, now, "", State.QUEUED.value, "proposed by the Theorist"
                        + (f": {theory.why}" if theory.why else ""))
        changed += 1

    combine = ""
    if theory.combine:
        try:
            combine = propose_combine(theory.combine, now, "theorist").slug
            changed += 1
        except ValueError as why:
            notes.append(f"combine dropped: {why} while the Theorist was thinking")

    linked = []
    for k in theory.links:
        a, b = store.get_mission(k.mission), store.get_mission(k.to)
        if not a or not b or State.COMBINED in (a.state, b.state) or not (is_open(a) or is_open(b)) \
                or store.get_link(a.slug, b.slug):
            notes.append(f"link dropped: {k.mission} <-> {k.to} changed while the Theorist was thinking")
            continue
        link(a.slug, b.slug, k.why, now, by="theorist")
        linked.append([a.slug, b.slug])
        changed += 1
    _drop_empty_proposals()

    if theory.library:
        notes.append(theory.library)
    store.save_theory({"at": now.isoformat(), "fingerprint": desk_fingerprint(), "patterns": theory.patterns,
                       "why": theory.why, "line_before": before, "line_after": store.get_inspiration(),
                       "epics": epics, "mission": slug, "combine": combine, "links": linked, "notes": notes,
                       "lookups": theory.lookups})
    print(f"theorist: {changed} change(s)" + (f"; {'; '.join(notes)}" if notes else ""), flush=True)
    return changed

WHO = {"theorist": "the Theorist", "chat": "the chat assistant", "human": "human"}

def propose_epic(title: str, slugs: list[str], why: str, now, by: str = "theorist", busy: set[str] = frozenset(),
                 extra: int = 0) -> tuple[Epic, int]:
    """A new epic, proposed: it waits for the human like the finder's, and each mission moved into it keeps its old
    (active) epic to go back to if it is rejected. Missions being worked on right now stay where they are. extra
    counts a mission that will join it later (the Theorist's new mission). Used by the Theorist and the chat
    assistant. Returns (epic, missions moved); raises ValueError if the epic exists or too few missions can move."""
    title = epic_title(title)
    if not title:
        raise ValueError("an epic title is 1 to 4 words")
    if store.find_epic(title):
        raise ValueError(f"{title} exists now")
    members = [m for m in (store.get_mission(s) for s in dict.fromkeys(slugs)) if m and m.slug not in busy]
    if len(members) + extra < MIN_NEW_EPIC_MISSIONS:
        raise ValueError(f"{title}, not enough of its missions are free to move")
    epic = store.create_epic(title, status="proposed")
    for m in members:
        old = store.get_epic(m.epic) if m.epic else None
        m.epic_fallback = m.epic if old and old.status == "active" else m.epic_fallback
        m.epic = epic.slug
        store.save_mission(m)
        store.log_event(m.slug, now, m.state.value, m.state.value,
                        f"{WHO.get(by, by)} proposes the new epic {epic.title}" + (f": {why}" if why else ""))
    return epic, len(members)

def propose_combine(c, now, by: str = "theorist") -> Combine:
    """Propose folding c.missions into one bigger mission (theorist.NewCombine, already settled); it waits for the
    human. Raises ValueError if one of them has moved on to writing since it was checked."""
    missions = store.all_missions()
    state = {m.slug: m.state for m in missions}
    late = [s for s in c.missions if state.get(s) not in COMBINABLE]
    if late:
        raise ValueError(f"{', '.join(late)} moved on")
    taken = {m.slug for m in missions} | {k.slug for k in store.all_combines()}
    made = Combine(slug=_new_slug(c.topic, taken), topic=c.topic, objective=c.objective, members=c.missions,
                   epic=c.epic, why=c.why, at=now.isoformat(), by=by)
    store.save_combine(made)
    for s in c.missions:
        store.log_event(s, now, state[s].value, state[s].value, f"{WHO.get(by, by)} proposes combining it into: {c.topic}")
    return made

# ---------- publishing: the local site, then every channel ----------

CHANNEL_NAMES = {"site": "Local site", f916.KEY: f916.NAME, github.KEY: github.NAME, bluesky.KEY: bluesky.NAME}

def channel_name(key: str) -> str:
    """What the UI calls a channel key, custom channels included (a removed one shows its key)."""
    if key in CHANNEL_NAMES:
        return CHANNEL_NAMES[key]
    return next((c.NAME for c in agents.channels() if c.KEY == key), key)

def channels(m: Mission) -> dict[str, ChannelPost]:
    """Everywhere this mission was published. Missions published before channels were recorded
    went to the local site only."""
    found = dict(m.channels)
    if m.state == State.DONE and m.published_at and "site" not in found:
        found["site"] = ChannelPost(at=m.published_at, where=f"site/{m.slug}/index.html")
    return found

def request_publish(m: Mission, keys: list[str], now) -> None:
    """A human asks to publish a published article again: "site" rewrites its page on the local site, a channel
    key posts it there (again, if it already went), ahead of everything else waiting. The loop does it on its
    next sweep, within seconds while it runs; a try that fails stays requested for the next publishing pass."""
    if m.state != State.DONE:
        raise ValueError("only a published article can be published again")
    known = {"site"} | {c.KEY for c in agents.channels() if c.enabled()}
    keys = [k for k in dict.fromkeys(keys) if k in known]
    if not keys:
        raise ValueError("nowhere to publish it")
    m.publish_requests |= {k: now for k in keys}
    store.save_mission(m)
    store.log_event(m.slug, now, m.state.value, m.state.value,
                    "publishing requested by human: " + ", ".join(channel_name(k) for k in keys))

def requests() -> set[tuple[str, str, str]]:
    """Every publish request waiting, as (slug, key, when): the loop runs a pass as soon as a new one shows up."""
    return {(m.slug, k, str(t)) for m in store.all_missions() for k, t in m.publish_requests.items()}

def queue(channel) -> list[dict]:
    """Feed entries this channel hasn't posted yet, newest first, and any it refused after the rest; entries a
    human asked to publish there go ahead of all of them, even ones it already posted.
    An entry still waiting for its text (published before the feed) waits for the next refresh_feed."""
    missions = {m.slug: m for m in store.all_missions()}
    def wanted(e):
        m = missions.get(e["slug"])
        return m and (channel.KEY not in m.channels or channel.KEY in m.publish_requests)
    waiting = [e for e in publisher.entries() if e.get("text") and wanted(e)]
    return sorted(waiting, key=lambda e: (channel.KEY not in missions[e["slug"]].publish_requests,
                                          channel.KEY in missions[e["slug"]].channel_errors))

def republish_site(now) -> int:
    """Rewrite the local page of every article a human asked to publish again, then the index and feeds."""
    done = 0
    for m in store.all_missions():
        if "site" not in m.publish_requests:
            continue
        if m.state == State.DONE and m.published_at and store.load_artifact(m.slug, "article"):
            try:
                activity.begin(m.slug, "publisher")
                location = publisher.write_page(m, _brief(m), _article(m), m.published_at)
            finally:
                activity.clear()
            m.channels["site"] = ChannelPost(at=now, where=str(location))
            store.log_event(m.slug, now, m.state.value, m.state.value, f"page rewritten at {location}")
            done += 1
        m.publish_requests.pop("site")
        store.save_mission(m)
    if done:
        publisher.rebuild_index()
    return done

def _drop_stale_requests() -> None:
    """Requests for a channel that has since been switched off or removed can never be done."""
    known = {"site"} | {c.KEY for c in agents.channels() if c.enabled()}
    for m in store.all_missions():
        stale = [k for k in m.publish_requests if k not in known]
        if stale:
            for k in stale:
                m.publish_requests.pop(k)
            store.save_mission(m)

MAX_REFUSALS = 3                           # per channel per pass: a channel refusing everything stops trying

class _publishing:
    """Hold PUBLISH_LOCK for one pass: a pass started while another runs (the UI's while the loop starts, or the
    other way round) waits for it, then finds the requests it already did gone."""
    def __enter__(self):
        PUBLISH_LOCK.parent.mkdir(parents=True, exist_ok=True)
        self.f = PUBLISH_LOCK.open("a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
    def __exit__(self, *exc):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()

def post_to_channels(now, max_refusals: int = MAX_REFUSALS) -> int:
    """One publishing pass: pages a human asked to rewrite, then every channel that is set up works through its
    backlog, the articles a human asked for first, as far as its allowance goes: a channel with no cap (a folder,
    a webhook) posts everything waiting, 1F916 its one a day, Bluesky one per 10-minute gap."""
    if STOP_FLAG.exists():
        return 0
    with _publishing():
        _drop_stale_requests()
        return republish_site(now) + sum(post_to(c, now, max_refusals) for c in agents.channels() if c.enabled())

def publish_requested(now) -> int:
    """With the loop off, the UI publishes what a human just asked for itself: pages to rewrite, then each
    channel's requested entries, and nothing else from its backlog, which waits for the loop. A request that
    fails stays requested, as in the loop's pass. Needs no model and ignores a leftover stop."""
    with _publishing():
        _drop_stale_requests()
        done = republish_site(now)
        asked = {k for m in store.all_missions() for k in m.publish_requests}
        return done + sum(post_to(c, now, only_asked=True) for c in agents.channels() if c.enabled() and c.KEY in asked)

def post_to(channel, now, max_refusals: int = MAX_REFUSALS, only_asked: bool = False) -> int:
    """Post the channel's whole backlog until it has no allowance left (its wait_until, or NoAllowance). A
    refused entry costs nothing, so the next one is tried, up to max_refusals in a pass; any other failure stops
    this channel until its next pass (an entry a human asked for stays asked for). only_asked stops at the
    first entry nobody asked for (they come first). Returns how many posted."""
    posted = refusals = 0
    if hasattr(channel, "sync"):
        try:
            channel.sync(now)
        except Exception as e:             # nothing is lost, try next turn
            print(f"{channel.NAME} update failed: {type(e).__name__}: {e}", flush=True)
            return 0
    for entry in queue(channel):
        if refusals >= max_refusals:
            break
        m = store.get_mission(entry["slug"])
        asked = channel.KEY in m.publish_requests
        if only_asked and not asked:
            break
        until = channel.wait_until()
        if until and now < until and not (asked and getattr(channel, "MANUAL_SKIPS_WAIT", False)):
            break
        try:
            activity.begin(m.slug, channel.KEY if channel.KEY in activity.AGENTS else "publisher")
            where = channel.post(entry, now)
        except NoAllowance as e:
            print(f"{channel.NAME}: no post allowance now ({e})", flush=True)
            break
        except Refused as e:
            m.channel_errors[channel.KEY] = str(e)
            m.publish_requests.pop(channel.KEY, None)
            store.save_mission(m)
            store.log_event(m.slug, now, m.state.value, m.state.value, f"{channel.NAME} refused the post: {e}")
            refusals += 1
            continue
        except Exception as e:             # bad key, network, site down: nothing is lost, try next turn
            print(f"{channel.NAME} posting failed: {type(e).__name__}: {e}", flush=True)
            break
        finally:
            activity.clear()
        m.channels[channel.KEY] = ChannelPost(at=now, where=where)
        m.channel_errors.pop(channel.KEY, None)
        m.publish_requests.pop(channel.KEY, None)
        store.save_mission(m)
        store.log_event(m.slug, now, m.state.value, m.state.value, f"posted to {channel.NAME}: {where}")
        posted += 1
    return posted

# ---------- epics (human decisions) ----------

def _in_epic(slug: str) -> list[Mission]:
    return [m for m in store.all_missions() if m.epic == slug]

def _refresh_pages(missions: list[Mission]) -> None:
    """Epic labels are baked into article pages, so redraw the ones whose label may have changed."""
    published = False
    for m in missions:
        if m.state == State.DONE and m.published_at and store.load_artifact(m.slug, "article"):
            publisher.write_page(m, _brief(m), _article(m), m.published_at)
            published = True
        elif m.state == State.ARTICLE_READY and store.load_artifact(m.slug, "article"):
            _write_article_review(m, _brief(m), _article(m))
    if published:
        publisher.rebuild_index()

def refresh_feed() -> None:
    """Articles published before meta.json carried their text get it now, and the index and feeds are
    rebuilt so a changed site URL shows up. Cheap: it reads one small file per published article."""
    stale = publisher.missing_text()
    _refresh_pages([m for m in store.all_missions() if m.slug in stale])
    if publisher.entries():
        publisher.rebuild_index()

def _drop_empty_proposals() -> None:
    used = {m.epic for m in store.all_missions()}
    for e in store.all_epics("proposed"):
        if e.slug not in used:
            store.delete_epic(e.slug)

def _move(missions: list[Mission], epic: str, now, note: str) -> None:
    for m in missions:
        m.epic, m.epic_fallback = epic, ""
        store.save_mission(m)
        store.log_event(m.slug, now, m.state.value, m.state.value, note)

def assign_epic(m: Mission, name: str, now) -> None:
    """Human puts a mission in an epic by title or slug, making it if it's new. Blank takes it out of any epic.
    An epic the human names is active right away."""
    epic = store.create_epic(name) if name.strip() else None
    _move([m], epic.slug if epic else "", now, f"epic set to {epic.title}" if epic else "epic cleared")
    _drop_empty_proposals()
    _refresh_pages([m])

def approve_epic(slug: str, title: str, now, by: str = "") -> None:
    """Human accepts a proposed epic, optionally renamed. A name that matches another epic merges into it."""
    epic = store.get_epic(slug)
    if not epic or epic.status != "proposed":
        raise ValueError(f"{slug} is not a proposed epic")
    clean = epic_title(title or epic.title)
    if not clean:
        raise ValueError("an epic title needs 1 to 4 words")
    other = store.find_epic(clean)
    if other and other.slug != slug:
        return merge_epic(slug, other.slug, now)
    epic.title, epic.status = clean, "active"
    store.save_epic(epic)
    missions = _in_epic(slug)
    _move(missions, slug, now, f"new epic approved{' by ' + by if by else ''}: {clean}")
    _refresh_pages(missions)

def merge_epic(slug: str, into: str, now) -> None:
    """Every mission in one epic moves to another, and the first epic is gone."""
    target = store.get_epic(into)
    if not store.get_epic(slug) or not target or slug == into:
        raise ValueError("pick a different epic to move them to")
    if target.status != "active":
        raise ValueError(f"{target.title} is still only proposed")
    missions = _in_epic(slug)
    _move(missions, into, now, f"moved to epic {target.title}")
    store.delete_epic(slug)
    _refresh_pages(missions)

def reject_epic(slug: str, now) -> None:
    """Human turns down a proposed epic: each mission goes back to the existing epic the finder picked for it."""
    epic = store.get_epic(slug)
    if not epic or epic.status != "proposed":
        raise ValueError(f"{slug} is not a proposed epic")
    for m in _in_epic(slug):
        back = store.get_epic(m.epic_fallback)
        back = back.slug if back and back.status == "active" else ""
        _move([m], back, now, f"proposed epic {epic.title} rejected"
              + (f"; back to {store.get_epic(back).title}" if back else ""))
    store.delete_epic(slug)

def rename_epic(slug: str, title: str, now) -> None:
    epic, clean = store.get_epic(slug), epic_title(title)
    if not epic:
        raise ValueError(f"no epic {slug}")
    if not clean:
        raise ValueError("an epic title needs 1 to 4 words")
    other = store.find_epic(clean)
    if other and other.slug != slug:
        raise ValueError(f"there is already an epic called {other.title}")
    epic.title = clean
    store.save_epic(epic)
    _refresh_pages(_in_epic(slug))

# ---------- links and combines (the Theorist connects dots; a human can undo) ----------

def link(a: str, b: str, why: str, now, by: str = "human") -> Link:
    """Connect two missions while keeping them separate. At least one must be open; a published article only
    shows the link once both are published (publisher.connected). A removed link made again is active again."""
    ma, mb = store.get_mission(a), store.get_mission(b)
    if not ma or not mb or a == b:
        raise ValueError("pick two different missions")
    if State.COMBINED in (ma.state, mb.state):
        raise ValueError("a mission combined into another can't be linked; link the bigger one")
    if not is_open(ma) and not is_open(mb):
        raise ValueError("a link needs at least one mission that isn't published yet")
    saved = store.save_link(Link(a=a, b=b, why=" ".join(why.split()), by=by, at=now.isoformat()))
    who = WHO.get(by, by)
    for m, other in ((ma, mb), (mb, ma)):
        store.log_event(m.slug, now, m.state.value, m.state.value,
                        f"linked to {other.slug} by {who}" + (f": {saved.why}" if saved.why else ""))
    return saved

def unlink(a: str, b: str, now) -> None:
    """A human removes a link. It is kept as removed, so the Theorist doesn't draw it again."""
    found = store.get_link(a, b)
    if not found or found.status != "active":
        raise ValueError(f"{a} and {b} aren't linked")
    found.status = "removed"
    store.save_link(found)
    missions = [m for m in (store.get_mission(a), store.get_mission(b)) if m]
    for m in missions:
        store.log_event(m.slug, now, m.state.value, m.state.value, f"link to {found.other(m.slug)} removed by human")
    _refresh_pages(missions)               # two published articles stop pointing at each other

def approve_combine(slug: str, now, by: str = "", busy: set[str] = frozenset()) -> Mission:
    """Accept a proposed combine: the bigger mission is queued (its brief still needs review), each member stops
    (State.COMBINED, pointing at it) and its links pass to the bigger mission. Members that moved on to writing
    meanwhile stay as they are; with fewer than 2 left the proposal is dropped."""
    c = store.get_combine(slug)
    if not c or c.status != "proposed":
        raise ValueError(f"{slug} is not a proposed combine")
    if set(c.members) & set(busy):
        raise ValueError("a worker is busy with one of its missions; try again once that task ends")
    members = [m for m in (store.get_mission(s) for s in c.members) if m and m.state in COMBINABLE]
    if len(members) < 2:
        store.delete_combine(slug)
        raise ValueError("its missions have moved on, so there is nothing left to combine; the proposal is dropped")
    taken = {m.slug for m in store.all_missions()}
    new = c.slug if c.slug not in taken and c.slug not in SITE_FOLDERS else _new_slug(c.topic, taken)
    epic = store.find_epic(c.epic) if c.epic else None
    listed = "\n".join(f"- {m.slug}: {m.topic}" for m in members)
    combined = Mission(slug=new, topic=c.topic, epic=epic.slug if epic else "", combines=[m.slug for m in members],
                       objective=f"{c.objective}\n\nCombines these missions, proposed by {WHO.get(c.by, c.by)}:\n{listed}")
    store.save_mission(combined)
    store.log_event(new, now, "", State.QUEUED.value, f"combined from {', '.join(combined.combines)}"
                    + (f", approved by {by}" if by else "") + (f": {c.why}" if c.why else ""))
    for m in members:
        before = m.state
        m.state, m.combined_into = State.COMBINED, new
        store.save_mission(m)
        store.log_event(m.slug, now, before.value, m.state.value, f"combined into {new}"
                        + (f", approved by {by}" if by else ""))
        for k in store.links_of(m.slug):
            other = k.other(m.slug)
            target = store.get_mission(other)
            if other in combined.combines or not target or target.state == State.COMBINED \
                    or store.get_link(new, other):
                continue
            store.save_link(Link(a=new, b=other, why=k.why, by=k.by, at=now.isoformat()))
    c.slug, c.status = new, "approved"
    store.delete_combine(slug)
    store.save_combine(c)
    return combined

def reject_combine(slug: str, now) -> None:
    """A human turns a combine down. It is kept as rejected, so the Theorist doesn't propose it again."""
    c = store.get_combine(slug)
    if not c or c.status != "proposed":
        raise ValueError(f"{slug} is not a proposed combine")
    c.status = "rejected"
    store.save_combine(c)
    for m in (store.get_mission(s) for s in c.members):
        if m:
            store.log_event(m.slug, now, m.state.value, m.state.value, f"combine into {c.topic!r} rejected by human")

def run_until_idle(now_fn, max_tasks: int = MAX_TASKS, after_task=None) -> int:
    """Work until nothing is left that the loop can do on its own: quick code steps first, then the
    farthest-along model work, with both workers side by side (see work). Last, every channel that is set up
    posts from the feed whatever it has not posted yet, as far as its allowance goes (1F916: one a day)."""
    activity.reset()
    refresh_feed()
    total = work(now_fn, max_tasks, after_task)
    return total + post_to_channels(now_fn())
