"""Research agent: a small model finds sources (and contacts), then the main model writes a ResearchBrief."""
import asyncio
from pydantic import BaseModel, ValidationError
import settings
from contracts import Mission, ResearchBrief, POC
from agents import llm
from checks import validator
from agents.toolbox import Toolbox

MAX_TOOL_CALLS = 40                        # sources run
MAX_CONTACT_CALLS = 20                     # contacts run (outreach only)

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

POCS_LATER = "- Do not look for points of contact; a separate run finds them.\n"
NO_POC_RULE = "- Do not look for points of contact; leave pocs as an empty list.\n"

CONTACTS = """You are finding people to interview.
Topic: {topic}
Mission: {objective}

Find at least 3 points of contact (experts, researchers, press offices, spokespeople) on this topic.
Work in this order for each person:
1. Search for the person or their organisation.
2. Fetch a public HTML page that should show their email: a staff, faculty, author or press contact
   page (not a PDF or contact form).
3. Read the fetched text. Keep the person only if their name AND their email address are both
   in the text of that fetched page. Record that page as contact_source_url.
Rules:
- Search results are not proof. An email seen only in a search result does not count.
- Never guess or build an email address from a name.
- If a page has no email, fetch a different page or move on to another person.
- When you have 3 contacts whose emails you saw on fetched pages, stop calling tools and reply DONE.
{notes}"""

LIST_CONTACTS = ("Now list the contacts as JSON matching the schema. Include only people whose name "
                 "and email you saw on a page you fetched, with that page as contact_source_url.")

FINAL = ("Now output the research brief as JSON matching the schema. "
         "Use only sources you fetched, with quotes copied exactly from those pages.")
REVISE_FINAL = ("Now output the revised research brief as JSON matching the schema. "
                "Use only the verified sources listed above and sources you fetched in this session, "
                "with quotes copied exactly from those pages.")

class ResearchFailed(Exception):
    pass

class Contacts(BaseModel):
    contacts: list[POC]

def _failed_source_ids(notes: str) -> set[str]:
    """The validator starts every per-source problem with "<source id>: "."""
    return {line.split(":", 1)[0] for line in notes.splitlines()}

def _notes(m: Mission) -> str:
    if m.rejected_brief:
        brief, notes = m.rejected_brief.brief, m.rejected_brief.notes
        failed = _failed_source_ids(notes)
        verified = [s.id for s in brief.sources if s.id not in failed]
        return f"""
Your previous brief was rejected. Revise it instead of starting over:
{brief.model_dump_json(indent=2)}

Sources that passed verification: {", ".join(verified) or "none"}.
Keep these exactly as they are (same id, url, title, quote and stance). They count as fetched, so do
not search for them or fetch them again. Spend your research only on fixing every problem below, and
update any claim that cites a source you replace:
{notes}
"""
    if not m.validator_notes:
        return ""
    return f"\nYour previous brief was rejected. Fix every problem below:\n{m.validator_notes}\n"

def _contact_notes(m: Mission) -> str:
    lines = [l for l in m.validator_notes.splitlines() if l.startswith("POC ")]
    return "\nLast time these contacts failed the check. Do not repeat them:\n" + "\n".join(lines) + "\n" if lines else ""

async def tool_loop(system: str, tb, budget: int) -> list:
    """One run of the small model: search and fetch until it stops or the budget runs out."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": "Begin."}]
    calls = 0
    while calls < budget:
        msg = await llm.chat(messages, tools=tb.tools, model=llm.SEARCH_MODEL)
        messages.append(msg)
        if not msg.tool_calls:
            break
        for tc in msg.tool_calls:
            if calls >= budget:
                break
            calls += 1
            result = await tb.call(tc.function.name, dict(tc.function.arguments or {}))
            messages.append({"role": "tool", "tool_name": tc.function.name, "content": result})
    return messages

async def find_contacts(m: Mission, tb) -> list[POC]:
    """Contacts run, then a code check: names and emails must really be on the cited pages."""
    messages = await tool_loop(CONTACTS.format(topic=m.topic, objective=m.objective or m.topic,
                                               notes=_contact_notes(m)), tb, MAX_CONTACT_CALLS)
    messages.append({"role": "user", "content": LIST_CONTACTS})
    msg = await llm.chat(messages, format=Contacts.model_json_schema(), model=llm.SEARCH_MODEL)
    try:
        found = Contacts.model_validate_json(msg.content).contacts
    except ValidationError as e:
        raise ResearchFailed(f"contacts run returned invalid JSON: {e}")
    problems = [validator.poc_problems([p]) for p in found]
    good = [p for p, bad in zip(found, problems) if not bad]
    if len(good) < validator.MIN_POCS:
        notes = "\n".join(n for bad in problems for n in bad)
        raise ResearchFailed(f"only {len(good)} verified contact(s); need {validator.MIN_POCS}\n{notes}")
    return good

async def run_research(m: Mission, now=None, toolbox_factory=Toolbox) -> ResearchBrief:
    today = f"Today's date: {now:%Y-%m-%d}. Prefer current information.\n" if now else ""
    system = SYSTEM.format(topic=m.topic, objective=m.objective or m.topic, today=today, notes=_notes(m),
                           pocs=POCS_LATER if settings.OUTREACH else NO_POC_RULE)
    pocs = None
    async with toolbox_factory(m.tools) as tb:
        messages = await tool_loop(system, tb, MAX_TOOL_CALLS)
        if settings.OUTREACH:                  # separate run; a failed check stops here, before the main model
            pocs = await find_contacts(m, tb)
            messages.append({"role": "user", "content": "Verified points of contact. Use exactly these as pocs:\n"
                             + Contacts(contacts=pocs).model_dump_json()})

    messages.append({"role": "user", "content": REVISE_FINAL if m.rejected_brief else FINAL})
    schema = ResearchBrief.model_json_schema()
    error = None
    for _ in range(2):                     # one repair attempt for malformed JSON
        msg = await llm.chat(messages, format=schema)
        try:
            brief = ResearchBrief.model_validate_json(msg.content)
            if pocs is not None:
                brief.pocs = pocs              # code-checked contacts; the model can't swap them
            return brief
        except ValidationError as e:
            error = e
            messages.append({"role": "assistant", "content": msg.content})
            messages.append({"role": "user", "content": f"That JSON was invalid:\n{e}\nReturn corrected JSON only."})
    raise ResearchFailed(f"model could not produce a valid brief: {error}")

def research(m: Mission, now=None) -> ResearchBrief:
    """Sync entry point the orchestrator calls."""
    return asyncio.run(run_research(m, now))
