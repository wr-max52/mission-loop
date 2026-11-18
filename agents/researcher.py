"""Research agent: a bounded tool loop on Ollama that ends in a ResearchBrief."""
import asyncio
from pydantic import ValidationError
import settings
from contracts import Mission, ResearchBrief
from agents import llm
from agents.toolbox import Toolbox

MAX_TOOL_CALLS = 40

SYSTEM = """You are a nonpartisan political research analyst.
Topic: {topic}
Mission: {objective}
{today}
Find one specific, defensible thesis and prove it with sources.
Rules:
- The thesis must be a verifiable factual claim about what happened, what someone said or did, or what
  a public record shows. Not an opinion, prediction, endorsement or moral judgement.
- Prefer primary sources: official government and legislature websites, bill text and roll-call votes,
  court opinions and filings, election and campaign-finance records, official transcripts and press
  releases. Use news outlets and research organisations for context and for the opposing view.
- When you attribute words or actions to a named person, cite a page that shows their own words
  or the official record, not a summary of it.
- For events, use sources published close to the event and put dates in the claims.
- Use the tools to search, then fetch and read full pages. Only cite pages you actually fetched.
  Prefer HTML pages; PDFs and paywalled pages usually cannot be verified.
- For every source, copy one exact sentence from that page as its quote, character for character, from
  the fetched text. Never paraphrase, shorten or merge sentences. Code checks every quote against the page.
- Find at least 3 supporting sources on 3 different websites, and at least 1 credible source that
  disagrees or complicates the thesis, on a different website from all the supporting sources.
{pocs}- When you have enough evidence, stop calling tools and reply with the word DONE.
{notes}"""

POC_RULE = """- Find at least 3 points of contact (experts, researchers, press offices, spokespeople) whose email
  address appears in the text of a public HTML page you fetched (not a PDF or contact form).
  Record that page as contact_source_url.
"""
NO_POC_RULE = "- Do not look for points of contact; leave pocs as an empty list.\n"

FINAL = ("Now output the research brief as JSON matching the schema. "
         "Use only sources you fetched, with quotes copied exactly from those pages.")

class ResearchFailed(Exception):
    pass

def _notes(m: Mission) -> str:
    if not m.validator_notes:
        return ""
    return f"\nYour previous brief was rejected. Fix every problem below:\n{m.validator_notes}\n"

async def run_research(m: Mission, now=None, toolbox_factory=Toolbox) -> ResearchBrief:
    today = f"Today's date: {now:%Y-%m-%d}. Prefer current information.\n" if now else ""
    messages = [
        {"role": "system", "content": SYSTEM.format(topic=m.topic, objective=m.objective or m.topic,
                                                    today=today, notes=_notes(m),
                                                    pocs=POC_RULE if settings.OUTREACH else NO_POC_RULE)},
        {"role": "user", "content": "Begin."},
    ]
    async with toolbox_factory(m.tools) as tb:
        calls = 0
        while calls < MAX_TOOL_CALLS:
            msg = await llm.chat(messages, tools=tb.tools)
            messages.append(msg)
            if not msg.tool_calls:
                break
            for tc in msg.tool_calls:
                if calls >= MAX_TOOL_CALLS:
                    break
                calls += 1
                result = await tb.call(tc.function.name, dict(tc.function.arguments or {}))
                messages.append({"role": "tool", "tool_name": tc.function.name, "content": result})

    messages.append({"role": "user", "content": FINAL})
    schema = ResearchBrief.model_json_schema()
    error = None
    for _ in range(2):                     # one repair attempt for malformed JSON
        msg = await llm.chat(messages, format=schema)
        try:
            return ResearchBrief.model_validate_json(msg.content)
        except ValidationError as e:
            error = e
            messages.append({"role": "assistant", "content": msg.content})
            messages.append({"role": "user", "content": f"That JSON was invalid:\n{e}\nReturn corrected JSON only."})
    raise ResearchFailed(f"model could not produce a valid brief: {error}")

def research(m: Mission, now=None) -> ResearchBrief:
    """Sync entry point the orchestrator calls."""
    return asyncio.run(run_research(m, now))
