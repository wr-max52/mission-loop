"""Claim tags (#132): whether a mission's claim held up, "confirmed" or "unconfirmed". Code, no model.

Ours only: the UI, the Theorist and the chat assistant read it. The published site, the feeds and the research and
writing prompts never see it, and it never changes an article's rating. Code sets the brief's (a thesis worded "It is
possible that" is unconfirmed, else confirmed), the writer sets the article's in its plan, and the rater, the
Theorist, the chat assistant or a human may change it. Agents have a floor (floor): a claim the record doesn't show
can't be called confirmed. A human may set anything, and a human's tag stands: agents don't change it afterwards.
Each tag keeps its earlier values, so the mission page shows how it got there."""
from contracts import Article, ClaimTag, CLAIM_TAGS, Mission, POSSIBILITY, ResearchBrief
import store

WHO = {"code": "code", "writer": "the writer", "rater": "the rater", "theorist": "the Theorist",
       "chat": "Researcher Chat", "human": "human"}
MAX_WHY = 300

def _brief(slug: str) -> ResearchBrief | None:
    data = store.load_artifact(slug, "brief")
    return ResearchBrief(**data) if data else None

def _article(slug: str) -> Article | None:
    data = store.load_artifact(slug, "article")
    return Article(**data) if data else None

def from_brief(brief: ResearchBrief) -> str:
    """The brief's tag: its thesis is worded "It is possible that" unless the record directly shows it."""
    return "unconfirmed" if POSSIBILITY.search(brief.thesis) else "confirmed"

def floor(m: Mission, article: Article | None = None, brief: ResearchBrief | None = None) -> str:
    """Why an agent can't call this mission's claim confirmed, or "" if it may. The article (the one about to be
    tagged, else the saved one) decides once there is one; before that, the brief's thesis."""
    article = article or _article(m.slug)
    if article:
        if m.unchecked_article:
            return "the article was pushed through a failed check"
        if POSSIBILITY.search(article.claim):
            return 'the claim box says "It is possible that"'
        return ""
    brief = brief or _brief(m.slug)
    if brief and POSSIBILITY.search(brief.thesis):
        return 'the thesis says "It is possible that"'
    return ""

def get(slug: str) -> ClaimTag | None:
    return store.get_claim_tag(slug)

def taggable(m: Mission) -> bool:
    """A mission has a claim to tag once it has a brief."""
    return store.load_artifact(m.slug, "brief") is not None

def set_tag(m: Mission, value: str, by: str, why: str, now, article: Article | None = None) -> ClaimTag:
    """Record a tag. An agent's "confirmed" over the floor is recorded as unconfirmed, saying why. An agent never
    changes a human's tag. The same value from someone else adds nothing. Raises ValueError for an unknown value or
    a mission with no brief yet. Returns the tag as it stands."""
    value = (value or "").strip().lower()
    if value not in CLAIM_TAGS:
        raise ValueError(f"a claim tag is one of: {', '.join(CLAIM_TAGS)}")
    if not taggable(m) and article is None:
        raise ValueError(f"{m.slug} has no brief yet, so no claim to tag")
    why = " ".join((why or "").split())[:MAX_WHY]
    old = store.get_claim_tag(m.slug)
    if old and old.by == "human" and by != "human":
        return old
    if by != "human" and value == "confirmed":
        held = floor(m, article)
        if held:
            value, why = "unconfirmed", f"{why} (code kept it unconfirmed: {held})".strip()
    if old and old.value == value:
        return old
    history = (old.history + [old.model_dump(include={"value", "by", "why", "at"})]) if old else []
    return store.save_claim_tag(ClaimTag(slug=m.slug, value=value, by=by, why=why, at=now.isoformat(),
                                         history=history))

def tag_brief(m: Mission, brief: ResearchBrief, now) -> ClaimTag:
    """Code's tag for a brief that just passed (or was pushed through); the brief is saved already."""
    words = "the thesis is a possibility" if from_brief(brief) == "unconfirmed" else "the thesis states it as fact"
    return set_tag(m, from_brief(brief), "code", f"from the brief: {words}", now)

def backfill(now) -> int:
    """Missions that have a brief but no tag (made before tags) get code's: the article's floor, else the brief's
    thesis. Run at every CLI start; cheap once every mission has one."""
    tags, made = store.all_claim_tags(), 0
    for m in store.all_missions():
        if m.slug in tags:
            continue
        brief = _brief(m.slug)
        if not brief:
            continue
        article = _article(m.slug)
        held = floor(m, article, brief) if article else ""
        value = "unconfirmed" if held else from_brief(brief)
        why = f"tagged by code at start-up: {held or ('the thesis is a possibility' if value == 'unconfirmed' else 'the thesis states it as fact')}"
        store.save_claim_tag(ClaimTag(slug=m.slug, value=value, why=why, at=now.isoformat()))
        made += 1
    return made

def text(t: ClaimTag | None) -> str:
    """"claim: confirmed", or "" when there is no tag."""
    return f"claim: {t.value}" if t else ""

def line(t: ClaimTag) -> str:
    """One plain line: the tag, who set it and why."""
    return f"{text(t)} (by {WHO.get(t.by, t.by)}" + (f": {t.why})" if t.why else ")")
