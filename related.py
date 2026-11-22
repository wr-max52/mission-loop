"""Past desk work for a new mission: the missions the Theorist connected to it (the missions a combined mission
was made from, and the missions and articles linked to it), then the published articles most like it, found by
word overlap in code (no model). Research sees them as background and leads, never as sources: the site never
leaves this machine, so they can't be fetched or cited. The pages they cited can be fetched again and cited
fresh."""
import board, store
from contracts import Mission, State, ResearchBrief, Article, words

MAX_RELATED = 3                            # articles shown; the small search model has a small context
MAX_OPEN = 2                               # possibilities each one left open
MAX_SOURCES = 4                            # cited pages listed for each one
MAX_LINKED = 4                             # linked missions and articles shown
MAX_CLAIMS = 3                             # claims shown from a linked or combined mission's brief
MIN_SHARED = 2                             # meaningful words in common before an article counts as related
SAME_EPIC_BONUS = 0.05                     # any published article can match; its own epic just ranks higher

def similar_published(m: Mission, limit: int = MAX_RELATED) -> list[tuple[Mission, ResearchBrief, Article]]:
    mine = words(f"{m.topic} {m.objective}")
    linked = {k.other(m.slug) for k in store.links_of(m.slug)}   # shown with the connections instead
    scored = []
    for other in store.all_missions():
        if other.state != State.DONE or other.slug == m.slug or other.slug in linked:
            continue
        b, a = store.load_artifact(other.slug, "brief"), store.load_artifact(other.slug, "article")
        if not b or not a:
            continue
        brief, article = ResearchBrief(**b), Article(**a)
        theirs = words(f"{other.topic} {article.title} {article.dek} {brief.thesis}")
        shared = len(mine & theirs)
        if shared < MIN_SHARED:
            continue
        score = shared / len(mine | theirs) + (SAME_EPIC_BONUS if m.epic and m.epic == other.epic else 0)
        scored.append((score, other, brief, article))
    scored.sort(key=lambda s: s[0], reverse=True)
    return [(o, b, a) for _, o, b, a in scored[:limit]]

def _brief(slug: str) -> ResearchBrief | None:
    data = store.load_artifact(slug, "brief")
    return ResearchBrief(**data) if data else None

def _brief_lines(b: ResearchBrief | None) -> list[str]:
    if not b:
        return []
    lines = [f"    Its thesis so far: {b.thesis}"]
    lines += [f"    {'Possibility' if c.kind == 'possibility' else 'Fact'}: {c.text}" for c in b.claims[:MAX_CLAIMS]]
    lines += [f"    Cited: {s.title} - {s.url}" for s in b.sources[:MAX_SOURCES]]
    return lines

def connections_block(m: Mission) -> str:
    """What the Theorist connected to this mission: the missions it was combined from, and the open missions and
    published articles linked to it, each with why. "" when there are none."""
    lines = []
    parts = [p for p in (store.get_mission(s) for s in m.combines) if p]
    if parts:
        lines += ["\nThis mission combines earlier missions into one bigger question. Answer the bigger question,",
                  "using what each of them was after; their work so far is background and leads, never evidence:"]
        for p in parts:
            lines.append(f"- {p.topic}")
            if p.objective:
                lines.append(f"    Its objective: {' '.join(p.objective.split())[:400]}")
            lines += _brief_lines(_brief(p.slug) or (p.rejected_brief.brief if p.rejected_brief else None))
    links = [(k, store.get_mission(k.other(m.slug))) for k in store.links_of(m.slug)]
    links = [(k, o) for k, o in links if o and o.state != State.COMBINED][:MAX_LINKED]
    if links:
        lines += ["\nMissions and articles linked to this one: separate work that bears on it. They are background",
                  "only: never cite them or treat them as evidence. Look for the thread named, and say in the brief",
                  "where this mission confirms, extends or cuts against them:"]
        for k, o in links:
            b = _brief(o.slug)
            a = store.load_artifact(o.slug, "article") if o.state == State.DONE else None
            if a:
                article = Article(**a)
                lines.append(f'- Published article "{article.title}": {article.dek}')
                if b:
                    lines += [f"    Left open: {c.text}" for c in b.claims if c.kind == "possibility"][:MAX_OPEN]
                    lines += [f"    Cited: {s.title} - {s.url}" for s in b.sources[:MAX_SOURCES]]
            else:
                lines.append(f"- Open mission: {o.topic}")
                lines += _brief_lines(b)
            if k.why:
                lines.append(f"    Linked because: {k.why}")
    return "\n".join(lines) + "\n" if lines else ""

def reference_block(m: Mission) -> str:
    """The research prompt's section on past desk work: its connections, then the closest published articles.
    "" when there is nothing."""
    return connections_block(m) + similar_block(m)

def similar_block(m: Mission) -> str:
    """The closest published articles, or "" when nothing published is close."""
    found = similar_published(m)
    try:                                   # the cork board strings the mission to what research was shown
        board.record_background(m.slug, [other.slug for other, _, _ in found])
    except Exception as e:
        print(f"board background for {m.slug} failed: {type(e).__name__}: {e}", flush=True)
    if not found:
        return ""
    lines = ["\nThe desk's earlier published articles closest to this mission. They are background only: never",
             "cite them or treat them as evidence. Use them for leads. The possibilities they left open are",
             "worth testing again, and you may fetch the pages they cited and use those as sources, if the",
             "pages still say what you need."]
    for other, b, a in found:
        when = f" ({other.published_at:%Y-%m-%d})" if other.published_at else ""
        lines.append(f'- "{a.title}"{when}: {a.dek}')
        lines += [f"    Left open: {c.text}" for c in b.claims if c.kind == "possibility"][:MAX_OPEN]
        lines += [f"    Cited: {s.title} - {s.url}" for s in b.sources[:MAX_SOURCES]]
    return "\n".join(lines) + "\n"
