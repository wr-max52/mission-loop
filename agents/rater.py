"""Article rater: the Research Assistant (the small model) reads an article that passed the article check and rates
it 1 to 10 on three things (contracts.RATING_FACTORS): the strength of its claim and logic, its intrigue, and the
credibility of its sources. Code adds them up and decides (orchestrator._rate): a total of RATING_FAIL_AT (20) or
less sends the article back to the writer with the rater's reasons, as another writing attempt, before a human
ever sees it.

The article check is code and checks what code can (citations, quotes, links); this is the one judgement it can't
make. No tools: the rater sees the article and the brief's sources, nothing else.
"""
import asyncio, json
from urllib.parse import urlparse
from contracts import Article, ArticleRating, Mission, RatingReply, ResearchBrief, RATING_SCALE, cited_ids
from agents import llm
import claim_tags, desk_profiles, store, trail, wayback

SYSTEM = """You are a tough, fair editor deciding whether an article is good enough to show the publisher.
Rate it on three things, each from 1 to {scale}:

logic: the strength of the claim and its logic. Is the claim at the top specific (who did what, when, how
  much)? Does each step of the evidence actually support it, or does the article leap from what a source says
  to more than it shows? Are the possibilities (sentences starting "It is possible that") each resting on a
  cited detail, and is the counterpoint answered fairly instead of waved away?
intrigue: would a curious reader keep reading? Does the opening make the stakes concrete, is there a real
  question or surprise, does it say why it matters to an ordinary person? Dull, vague or padded writing
  scores low; so does hype the evidence can't carry.
sources: {rater} A source's quote that doesn't say what the article uses it for counts against it.

How to score: 1-3 weak, 4-6 passable with real problems, 7-8 strong, 9-10 rare and excellent. Most decent
articles land between 5 and 8. Judge each factor on its own; don't let one pull the others up or down.
For each, first write why in one to three plain sentences naming the specific sentence, claim or source that
decided it and what would raise the score, then give the number.

Last, check the writer's claim tag, which counts toward no score: "confirmed" means the claim at the top states
the brief's claim as fact and the cited records show it; "unconfirmed" means it rests on a possibility or says less
than the brief set out to show. Write claim_why in one sentence, then claim: the tag you would give.
Output JSON matching the schema."""

ASK = """THE ARTICLE
{article}

ITS SOURCES (the id it cites, the site, the page's title, the sentence quoted from it, and whether it supports or
contradicts the thesis)
{sources}

THE BRIEF'S CLAIM (its thesis)
{thesis}

THE WRITER'S CLAIM TAG
{tag}"""

class RatingFailed(Exception):
    pass

def article_markdown(a: Article) -> str:
    out = [f"# {a.title}", a.dek]
    if a.claim.strip():
        out.append(f"The claim: {a.claim}")
    for sec in a.sections:
        out.append(f"## {sec.heading}\n" + "\n\n".join(sec.paragraphs))
    return "\n\n".join(out)

def _site(url: str) -> str:
    original = wayback.original(url) or url  # an archived copy counts as its original site
    return urlparse(original).netloc.removeprefix("www.") or url

def sources_text(brief: ResearchBrief, a: Article) -> str:
    cited = {sid for sec in a.sections for p in sec.paragraphs for sid in cited_ids(p)} | set(cited_ids(a.claim))
    lines = [f'- [{s.id}] {_site(s.url)} · "{s.title}" · {s.stance} · quote: "{s.quote}"'
             for s in brief.sources if s.id in cited]
    return "\n".join(lines) or "(none cited)"

def messages(brief: ResearchBrief, a: Article, tag: str = "") -> list[dict]:
    return [{"role": "system", "content": SYSTEM.format(scale=RATING_SCALE, rater=desk_profiles.indented("rater"))},
            {"role": "user", "content": ASK.format(article=article_markdown(a), sources=sources_text(brief, a),
                                                   thesis=brief.thesis, tag=tag or "none given")}]

async def run_rater(m: Mission, brief: ResearchBrief, a: Article) -> ArticleRating:
    trail.record("note", "rating the article")
    tag = store.get_claim_tag(m.slug)
    reply = await llm.ask(messages(brief, a, claim_tags.line(tag) if tag else ""),
                          RatingReply, "article rater", RatingFailed,
                          model=llm.model_for("assistant"))
    rating = ArticleRating(**reply.model_dump())
    trail.record("note", f"rated {rating.total}: " + json.dumps(reply.model_dump(include={"logic", "intrigue", "sources"})))
    return rating

def rate(m: Mission, brief: ResearchBrief, a: Article) -> ArticleRating:
    """Sync entry point the orchestrator calls."""
    return asyncio.run(run_rater(m, brief, a))
