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
import trail, wayback

SYSTEM = """You are a tough, fair editor deciding whether an article is good enough to show the publisher.
Rate it on three things, each from 1 to {scale}:

logic: the strength of the claim and its logic. Is the claim at the top specific (who did what, when, how
  much)? Does each step of the evidence actually support it, or does the article leap from what a source says
  to more than it shows? Are the possibilities (sentences starting "It is possible that") each resting on a
  cited detail, and is the counterpoint answered fairly instead of waved away?
intrigue: would a curious reader keep reading? Does the opening make the stakes concrete, is there a real
  question or surprise, does it say why it matters to an ordinary person? Dull, vague or padded writing
  scores low; so does hype the evidence can't carry.
sources: the credibility of the sources it cites. Primary records (filings, court documents, official data,
  the people involved on the record) and established outlets score high; anonymous blogs, press releases
  repeated as fact, partisan or commercial sites with a stake, and one source carrying the whole story score
  low. A source's quote that doesn't say what the article uses it for counts against it.

How to score: 1-3 weak, 4-6 passable with real problems, 7-8 strong, 9-10 rare and excellent. Most decent
articles land between 5 and 8. Judge each factor on its own; don't let one pull the others up or down.
For each, first write why in one to three plain sentences naming the specific sentence, claim or source that
decided it and what would raise the score, then give the number. Output JSON matching the schema."""

ASK = """THE ARTICLE
{article}

ITS SOURCES (the id it cites, the site, the page's title, the sentence quoted from it, and whether it supports or
contradicts the thesis)
{sources}"""

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

def messages(brief: ResearchBrief, a: Article) -> list[dict]:
    return [{"role": "system", "content": SYSTEM.format(scale=RATING_SCALE)},
            {"role": "user", "content": ASK.format(article=article_markdown(a), sources=sources_text(brief, a))}]

async def run_rater(m: Mission, brief: ResearchBrief, a: Article) -> ArticleRating:
    trail.record("note", "rating the article")
    reply = await llm.ask(messages(brief, a), RatingReply, "article rater", RatingFailed,
                          model=llm.model_for("assistant"))
    rating = ArticleRating(**reply.model_dump())
    trail.record("note", f"rated {rating.total}: " + json.dumps(reply.model_dump(include={"logic", "intrigue", "sources"})))
    return rating

def rate(m: Mission, brief: ResearchBrief, a: Article) -> ArticleRating:
    """Sync entry point the orchestrator calls."""
    return asyncio.run(run_rater(m, brief, a))
