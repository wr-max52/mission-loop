"""Past desk work for a new mission: the published articles most like it, found by word overlap in code
(no model). Research sees them as background and leads, never as sources: the site never leaves this
machine, so they can't be fetched or cited. The pages they cited can be fetched again and cited fresh."""
import store
from contracts import Mission, State, ResearchBrief, Article, words

MAX_RELATED = 3                            # articles shown; the small search model has a small context
MAX_OPEN = 2                               # possibilities each one left open
MAX_SOURCES = 4                            # cited pages listed for each one
MIN_SHARED = 2                             # meaningful words in common before an article counts as related
SAME_EPIC_BONUS = 0.05                     # any published article can match; its own epic just ranks higher

def similar_published(m: Mission, limit: int = MAX_RELATED) -> list[tuple[Mission, ResearchBrief, Article]]:
    mine = words(f"{m.topic} {m.objective}")
    scored = []
    for other in store.all_missions():
        if other.state != State.DONE or other.slug == m.slug:
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

def reference_block(m: Mission) -> str:
    """The research prompt's section on past articles, or "" when nothing published is close."""
    found = similar_published(m)
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
