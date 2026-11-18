"""Writer agent: one structured Ollama call that turns an approved brief into an Article.

No tools, no browsing. The writer may only use what is in the brief (and any POC comments),
and checks/article_check.py enforces that in code.
"""
import asyncio, json
from pydantic import ValidationError
from contracts import Mission, ResearchBrief, OutreachRecord, Article
from agents import llm

SYSTEM = """You are a skeptical, nonpartisan investigative writer.
Write a short, sharp article (500 to 900 words) from the research brief below. The brief is the only
thing you know: do not add facts, numbers, names, dates or quotes that are not in it.

The article shows where the public account stops and what may lie past it. Treat every source as part
truth, part spin: report what it shows, and never repeat its framing as settled.

Rules:
- Direct, provocative tone, aimed at the evidence and not at a party. Take no partisan side. Attribute
  everything to its source ("according to ...", "the filing shows ...", "the agency claims ..."). The
  only speculation allowed is the brief's claims with kind "possibility".
- Claims with kind "possibility" are unverified, and they are the heart of the article. Include every
  one, each written with the words "It is possible that", naming the evidence it rests on, and cited.
  Never state or imply one as fact, including in the title and dek. Do not use the words "it is
  possible that" for anything else.
- Say what the cited sources do not establish, and where their evidence stops short of their conclusions.
- Do not suggest that a named person committed a crime or acted in bad faith unless the brief says who
  made that allegation, and then name who made it.
- End every sentence that states a fact or a possibility with an inline citation of the source id(s) it
  comes from, in square brackets, e.g. "The bill passed 52-48 [senate]." or "[s1, s3]". Every paragraph
  needs one.
- Only use source ids listed in the brief. Never write URLs or Markdown links.
- Put words in quotation marks only when they are copied exactly from a source quote below.
- Include a section that fairly presents the strongest counterpoint, citing the contradicting source(s).
- 3 to 5 sections, each with a short heading. The first section states the thesis plainly.
- title: specific and provocative, but if it rests on a possibility, word it as a question or say
  "possible". dek: one sentence summarising the finding, saying so if it is unverified.
{comments}{notes}
RESEARCH BRIEF (JSON):
{brief}"""

class WritingFailed(Exception):
    pass

def _comments(records: list[OutreachRecord]) -> str:
    replies = [r for rec in records for r in rec.replies if r.get("is_comment")]
    if not replies:
        return ""
    lines = "\n".join(f"- {r.get('from', 'a contact')}: {r.get('text', '')}" for r in replies)
    return f"\nComments received from points of contact (you may quote these exactly):\n{lines}\n"

def _notes(m: Mission) -> str:
    if not m.validator_notes:
        return ""
    return f"\nYour previous draft was rejected. Fix every problem below:\n{m.validator_notes}\n"

def prompt(m: Mission, brief: ResearchBrief, records: list[OutreachRecord]) -> str:
    data = brief.model_dump(exclude={"pocs", "question_for_pocs"})
    return SYSTEM.format(comments=_comments(records), notes=_notes(m), brief=json.dumps(data, indent=1))

async def run_writer(m: Mission, brief: ResearchBrief, records: list[OutreachRecord]) -> Article:
    messages = [{"role": "system", "content": prompt(m, brief, records)},
                {"role": "user", "content": "Write the article as JSON matching the schema."}]
    schema = Article.model_json_schema()
    error = None
    for _ in range(2):                     # one repair attempt for malformed JSON
        msg = await llm.chat(messages, format=schema)
        try:
            return Article.model_validate_json(msg.content)
        except ValidationError as e:
            error = e
            messages.append({"role": "assistant", "content": msg.content})
            messages.append({"role": "user", "content": f"That JSON was invalid:\n{e}\nReturn corrected JSON only."})
    raise WritingFailed(f"model could not produce a valid article: {error}")

def write(m: Mission, brief: ResearchBrief, records: list[OutreachRecord]) -> Article:
    """Sync entry point the orchestrator calls."""
    return asyncio.run(run_writer(m, brief, records))
