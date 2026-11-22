import fcntl
from datetime import timedelta
from pathlib import Path
import activity
import agents                              # switchboard: see agents/__init__.py
import stopping, store
from agents import bluesky, f916, publisher
from agents.channel import NoAllowance, Refused
from contracts import (Mission, State, ResearchBrief, RejectedBrief, RejectedArticle, OutreachRecord, Article,
                       ChannelPost, epic_title, slugify)
from render import brief_markdown, article_markdown, article_html

STOP_FLAG = Path("data/STOP")
WAKE_FLAG = Path("data/WAKE")             # the UI touches this so a sleeping loop runs a pass now
LOCK_FILE = Path("data/loop.lock")         # held by whichever run/loop is working

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

def step(m: Mission, now) -> str | None:
    """Run at most one task. Return a note if the mission changed, None if blocked."""
    if m.state == State.QUEUED:
        m.state = State.RESEARCHING
        return "started"

    if m.state == State.RESEARCHING:
        m.attempts += 1
        brief, m.interrupted_brief = m.interrupted_brief, None   # a stop's draft: check it, don't redo it
        try:
            if brief is None:
                activity.begin(m.slug, "sources")
                brief = agents.research(m, now)
                if stopping.requested():   # finished, but leave checking it for the next pass
                    raise stopping.Interrupted(brief)
            activity.begin(m.slug, "source_check")
            ok, notes = agents.validate(m, brief)
        except stopping.Interrupted as e:  # a stopped attempt isn't a failed one
            m.attempts -= 1
            m.interrupted_brief = e.draft
            saved = "its draft is checked" if e.draft else "nothing usable was written; research restarts"
            return f"stopped mid-research; {saved} when the loop starts again"
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
            saved = "its draft is checked" if e.draft else "nothing usable was written; writing restarts"
            return f"stopped mid-writing; {saved} when the loop starts again"
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
        m.channels["site"] = ChannelPost(at=now, where=str(location))
        m.state = State.DONE
        return f"published to {location}"   # other channels pick it up at the end of this pass

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
            break                          # mid-task, a stop gets agents to write out what they have
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

def _new_slug(topic: str, taken: set[str]) -> str:
    base = slugify(topic)[:54].strip("-") or "mission"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug

MIN_NEW_EPIC_MISSIONS = 2                  # the finder may only propose an epic that already has 2+ missions
EPIC_TOPICS_SHOWN = 5                      # recent topics the finder sees per epic, so it knows what each holds

def _finder_epics(missions: list[Mission]) -> dict[str, list[str]]:
    return {e.title: [m.topic for m in missions if m.epic == e.slug][-EPIC_TOPICS_SHOWN:]
            for e in store.all_epics("active")}

def _settle_epic(idea, proposals: set[str]) -> tuple[str, str, str]:
    """(epic, fallback, note) for a found mission. It always gets the best existing epic the finder named;
    a new epic is proposed only when enough ideas from this pass share it, and waits for the human."""
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
    """The mission finder's turn. It needs an inspiration line, and its failures never hold up a pass."""
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

# ---------- publishing channels beyond the local site ----------

CHANNEL_NAMES = {"site": "Local site", f916.KEY: f916.NAME, bluesky.KEY: bluesky.NAME}

def channels(m: Mission) -> dict[str, ChannelPost]:
    """Everywhere this mission was published. Missions published before channels were recorded
    went to the local site only."""
    found = dict(m.channels)
    if m.state == State.DONE and m.published_at and "site" not in found:
        found["site"] = ChannelPost(at=m.published_at, where=f"site/{m.slug}/index.html")
    return found

def queue(channel) -> list[dict]:
    """Feed entries this channel hasn't posted yet: newest first, and any it refused after the rest.
    An entry still waiting for its text (published before the feed) waits for the next refresh_feed."""
    missions = {m.slug: m for m in store.all_missions()}
    waiting = [e for e in publisher.entries() if e.get("text") and e["slug"] in missions
               and channel.KEY not in missions[e["slug"]].channels]
    return sorted(waiting, key=lambda e: channel.KEY in missions[e["slug"]].channel_errors)

def post_to_channels(now, max_tries: int = 3) -> int:
    """Every channel that is set up posts from the feed, as far as its allowance goes."""
    if STOP_FLAG.exists():
        return 0
    return sum(post_to(c, now, max_tries) for c in agents.CHANNELS if c.enabled())

def post_to(channel, now, max_tries: int = 3) -> int:
    """Post waiting entries until the channel has no allowance left. A refused entry costs nothing, so the
    next one is tried; any other failure stops this channel until the next pass. Returns how many posted."""
    posted = 0
    for entry in queue(channel)[:max_tries]:
        until = channel.wait_until()
        if until and now < until:
            break
        m = store.get_mission(entry["slug"])
        try:
            activity.begin(m.slug, channel.KEY)
            where = channel.post(entry, now)
        except NoAllowance as e:
            print(f"{channel.NAME}: no post allowance now ({e})", flush=True)
            break
        except Refused as e:
            m.channel_errors[channel.KEY] = str(e)
            store.save_mission(m)
            store.log_event(m.slug, now, m.state.value, m.state.value, f"{channel.NAME} refused the post: {e}")
            continue
        except Exception as e:             # bad key, network, site down: nothing is lost, try next pass
            print(f"{channel.NAME} posting failed: {type(e).__name__}: {e}", flush=True)
            break
        finally:
            activity.clear()
        m.channels[channel.KEY] = ChannelPost(at=now, where=where)
        m.channel_errors.pop(channel.KEY, None)
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

def approve_epic(slug: str, title: str, now) -> None:
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
    _move(missions, slug, now, f"new epic approved: {clean}")
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

def _run_missions(now_fn, max_ticks: int) -> int:
    total = 0
    for _ in range(max_ticks):
        if STOP_FLAG.exists():
            break
        n = tick(now_fn())
        total += n
        if n == 0:
            break
    return total

def run_until_idle(now_fn, max_ticks: int = 100) -> int:
    """One pass. Existing missions go first so the finder never delays work a human just approved;
    whatever it finds is then researched in the same pass. Last, every channel that is set up posts from
    the feed whatever it has not posted yet, as far as its allowance goes (1F916: one a day)."""
    refresh_feed()
    total = _run_missions(now_fn, max_ticks)
    found = find_new_missions(now_fn())
    if found:
        total += found + _run_missions(now_fn, max_ticks)
    return total + post_to_channels(now_fn())
