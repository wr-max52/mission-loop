"""Research agent, in two steps that can run in different passes and side by side with other missions:
gather (the Research Assistant's model finds sources, and contacts) and write_brief (the Principal
Investigator's model writes a ResearchBrief from what was gathered)."""
import asyncio
from pydantic import BaseModel, ValidationError
import activity, related, settings, stopping, trail
from contracts import Evidence, Mission, ResearchBrief, POC
from agents import llm
from checks import validator
from agents.toolbox import Toolbox

MAX_TOOL_CALLS = 40                        # sources run
MAX_CONTACT_CALLS = 20                     # contacts run (outreach only)
SOURCE_TIME_LIMIT = 15 * 60                # seconds the source finder may run before it must stop

SYSTEM = """You are a skeptical, nonpartisan political investigator. Your job is to find the edge of what
is known and say plainly what may lie just past it.
Topic: {topic}
Mission: {objective}
{today}{past}
Assume every source is spinning. News outlets, officials, campaigns, think tanks, experts and contrarian
sites all mix some truth with framing, omission and motive, and none is accurate because of who it is.
Read each one by separating what it shows (a document, number, date, vote, direct quote) from what it
asserts (framing, characterisations, unnamed sources, "experts say"). Ask who benefits from this version,
what it leaves out, and where its evidence stops short of its conclusion. Keep the details and distrust
the story built around them. Doubt alone proves nothing: "the media is lying" is never evidence, so
every possibility must rest on a specific detail you found.

Work through these four stages in order. The brief contains both possibilities and facts on the record,
but they are different things and must never be blurred.
1. Explore widely. Think of many different explanations, especially ones the coverage avoids, dismisses
   or never considers, and ones that cut against your first idea. Search for evidence on several of
   them, not only the first.
2. Rule out. Drop only what specific evidence contradicts and what nothing you can cite gives a reason
   to consider. Do not drop an idea because it is uncomfortable, unpopular or surprising, or because
   officials deny it or outlets call it baseless: a denial is a claim to weigh, not a refutation.
3. Report what is possible. This is the main product. Write at least 3 claims with kind "possibility",
   and more of them than facts. Each must begin with the words "It is possible that", make a pointed,
   specific statement that someone would want to dispute, name what it rests on ("..., based on ..."),
   and cite the fetched sources for that evidence. Go as far as the evidence can carry and no further.
4. Pin down the record, as its own separate job. Write claims with kind "fact" only for what the record
   directly shows: what a document says, how a vote went, what a person said, what an outlet reported.
   Include at least one. Word anything contested as attribution ("The agency said ..."), not as settled
   truth. A fact must be directly shown by a source it cites; if it is not, make it a possibility or
   drop it.
Rules:
- The thesis is one specific, provocative, defensible statement: the boldest conclusion the evidence can
  carry. Word it "It is possible that ..." unless the record directly shows it. It must be checkable:
  name who, what, when or how much, so a document, number or record could show it wrong. A thesis so
  broad that nothing could count against it is too vague; narrow it.
- No claim may be an opinion, endorsement or moral judgement. Apply the same skepticism to every side.
- Aim provocation at what the evidence leaves open: official accounts, institutions, incentives, numbers
  and gaps. Do not use a possibility to suggest that a named person committed a crime or acted in bad
  faith, unless a source you cite makes that allegation, and then say who made it.
- Go beneath the coverage to primary material: legislature and government records, bill text and
  roll-call votes, court opinions and filings, election and campaign-finance records, official
  transcripts. Press releases are spin too; use them to show what was said. Use news outlets and
  research organisations for leads, details and the opposing view.
- Set each source's stance by what its evidence does to the thesis, not by what the outlet concludes. An
  article that dismisses an idea can still contain the detail that supports it; quote that detail.
- When you attribute words or actions to a named person, cite a page that shows their own words
  or the official record, not a summary of it.
- For events, use sources published close to the event and put dates in the claims.
- Use the tools to search, then fetch pages. News search may cover only the last day unless you set
  freshness, so set it when looking for older events.
- fetch shows only the start of a long page. Use find_in_page to search the whole page for what you need.
- Use page_links on useful articles to find the bill, filing, vote record, report or rebuttal they
  point to, then read that page too.
- Use wayback_versions when a site is gone or a page no longer loads, and to check whether a page has
  changed what it says over time: run find_in_page with the same words on an old copy and on the live
  page, and report a change as a fact with both dates. An archived copy is cited like any page, with its
  web.archive.org address as the url. When find_in_page says it read the Wayback Machine's copy, cite
  the address it gives.
- Only cite pages you read with fetch or find_in_page. If find_in_page says CANNOT CITE, do not use
  that page. Prefer HTML pages; PDFs and paywalled pages usually cannot be verified.
- For every source, copy one exact sentence from that page as its quote, character for character. The
  sentences find_in_page returns are safest. Never paraphrase, shorten or merge sentences. Code checks
  every quote against the page.
- Find at least 3 supporting sources on 3 different websites.
- Test the thesis: search for the strongest case against it (often the official or mainstream account)
  and cite it with stance "contradicts" if you find one. If no source pushes back, do not invent one or
  mislabel a supporting source: write the counterpoint as the strongest objection a skeptic would raise,
  and say what evidence would show the thesis wrong.
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

# The small model gathers the evidence; the main model makes these calls when it writes the brief.
KIND_RULE = ("Treat every source above as part truth, part spin. Drop only what specific evidence contradicts "
             "or nothing above gives a reason to consider; a denial does not rule anything out. Set kind on "
             "every remaining claim: \"fact\" only for what the record directly shows (what a document says, "
             "what someone said, what an outlet reported), \"possibility\" for what the evidence leaves open, "
             "worded \"It is possible that ..., based on ...\". Write at least 3 pointed possibilities, more "
             "than facts, and keep at least one fact. The counterpoint is the strongest case against the thesis: from "
             "a contradicting source if one was found, else the objection a skeptic would raise; never leave it empty.")
IDS = "Give each source a short id such as s1, s2 (never its URL) and cite sources in claims by that id. "
FINAL = ("Now output the research brief as JSON matching the schema. "
         "Use only sources you fetched, with quotes copied exactly from those pages. " + IDS + KIND_RULE)
REVISE_FINAL = ("Now output the revised research brief as JSON matching the schema. "
                "Use only the verified sources listed above and sources you fetched in this session, "
                "with quotes copied exactly from those pages. " + IDS + KIND_RULE)

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
Your previous brief was rejected. Revise it instead of starting over, and skip the exploring stage:
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

def _tool_servers(m: Mission) -> list[str]:
    """Saved missions list only search and fetch, so anything that can fetch also gets the page tools."""
    return m.tools + ["pages"] if "fetch" in m.tools and "pages" not in m.tools else m.tools

async def tool_loop(system: str, tb, budget: int, time_limit: float | None = None) -> list:
    """One run of the small model: search and fetch until it stops or the budget or time runs out.
    Running out of time works like running out of budget: the run keeps what it found so far.
    So does the stop button: the turn in flight may finish, then the run ends without its tool calls."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": "Begin."}]
    calls = 0
    try:
        async with asyncio.timeout(time_limit) as limit:
            while calls < budget and not stopping.requested():
                messages = llm.compact(messages, llm.TURN_TOKENS, llm.model_for("assistant"))   # kept cut: the next turns reuse the prompt cache
                try:
                    msg = await llm.chat(messages, tools=tb.tools, model=llm.model_for("assistant"))
                except llm.Cut:
                    break
                except llm.Overrun as e:   # a stuck turn ends the run; what it found is kept
                    print(f"search stopped: {e}; continuing with what it found", flush=True)
                    break
                if msg.tool_calls and stopping.requested():
                    break                  # its tool calls would never be answered
                messages.append(msg)
                if not msg.tool_calls:
                    break
                for tc in msg.tool_calls:
                    if calls >= budget:    # every call gets an answer, or some chat templates break
                        messages.append({"role": "tool", "tool_name": tc.function.name,
                                         "content": "Not run: the tool budget is used up."})
                        continue
                    calls += 1
                    result = await tb.call(tc.function.name, dict(tc.function.arguments or {}))
                    trail.record("tool_result", f"{tc.function.name}: {result}")
                    messages.append({"role": "tool", "tool_name": tc.function.name, "content": result})
    except TimeoutError:
        if not limit.expired():            # some other timeout inside the call: a real failure
            raise
        print(f"search stopped at its {time_limit / 60:g}-minute limit; continuing with what it found", flush=True)
        trail.record("note", f"search stopped at its {time_limit / 60:g}-minute limit")
    return messages

async def find_contacts(m: Mission, tb) -> list[POC]:
    """Contacts run, then a code check: names and emails must really be on the cited pages."""
    messages = await tool_loop(CONTACTS.format(topic=m.topic, objective=m.objective or m.topic,
                                               notes=_contact_notes(m)), tb, MAX_CONTACT_CALLS)
    messages.append({"role": "user", "content": LIST_CONTACTS})
    msg = await llm.chat(messages, format=Contacts.model_json_schema(), model=llm.model_for("assistant"))
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

def _plain(msg) -> dict:
    """A transcript message as plain JSON, so it can be saved between the two steps. The small model's
    thinking is left out: the brief writer only needs what it did and what it found."""
    out = msg.model_dump(exclude_none=True) if hasattr(msg, "model_dump") else dict(msg)
    out.pop("thinking", None)
    return out

FAILED = ("ERROR", "CANNOT CITE", "Not run:")

def _for_brief(messages: list) -> list:
    """The gathered transcript, trimmed for the brief writer: failed tool calls shrink to their first
    line and a result already shown earlier is not shown again, which leaves more room for real pages."""
    out, seen = [], set()
    for m in messages:
        m = {k: v for k, v in m.items() if k != "thinking"}   # evidence saved before thinking was dropped
        text = m.get("content") or ""
        if m.get("role") == "tool":
            if text.startswith(FAILED):
                m["content"] = text.splitlines()[0][:200]
            elif text in seen:
                m["content"] = "(same result as an earlier call)"
            seen.add(text)
        out.append(m)
    return out

async def run_gather(m: Mission, now=None, toolbox_factory=Toolbox) -> Evidence:
    """The Research Assistant's step: search and read until done, out of budget or time, or stopped."""
    today = f"Today's date: {now:%Y-%m-%d}. Prefer current information.\n" if now else ""
    system = SYSTEM.format(topic=m.topic, objective=m.objective or m.topic, today=today, notes=_notes(m),
                           past=related.reference_block(m),
                           pocs=POCS_LATER if settings.OUTREACH else NO_POC_RULE)
    pocs = None
    async with toolbox_factory(_tool_servers(m)) as tb:
        messages = await tool_loop(system, tb, MAX_TOOL_CALLS, SOURCE_TIME_LIMIT)
        if settings.OUTREACH and not stopping.requested():   # separate run; a failed check stops here, before the main model
            activity.begin(m.slug, "contacts")
            pocs = await find_contacts(m, tb)
    return Evidence(messages=[_plain(x) for x in messages], pocs=pocs)

async def run_write_brief(m: Mission, evidence: Evidence) -> ResearchBrief:
    """The Principal Investigator's step: write the brief from the gathered transcript."""
    activity.begin(m.slug, "brief")
    messages = _for_brief(evidence.messages)
    if evidence.pocs is not None:
        messages.append({"role": "user", "content": "Verified points of contact. Use exactly these as pocs:\n"
                         + Contacts(contacts=evidence.pocs).model_dump_json()})
    messages.append({"role": "user", "content": REVISE_FINAL if m.rejected_brief else FINAL})
    schema = ResearchBrief.model_json_schema()
    error = None
    for _ in range(2):                     # one repair attempt for malformed JSON
        try:
            msg = await llm.chat(messages, format=schema)
        except llm.Cut as cut:             # stop's grace ran out: keep what got written
            raise stopping.Interrupted(stopping.salvage(cut.text, ResearchBrief))
        except llm.Overrun as e:           # stuck: keep what it wrote before, if that holds a brief
            brief = stopping.salvage(e.text, ResearchBrief)
            if brief is None:
                raise ResearchFailed(f"the brief writer got stuck: {e}")
            print(f"brief writer: {e}; the validator checks what it wrote before that", flush=True)
            if evidence.pocs is not None:
                brief.pocs = evidence.pocs
            return brief
        try:
            brief = ResearchBrief.model_validate_json(msg.content)
            if evidence.pocs is not None:
                brief.pocs = evidence.pocs     # code-checked contacts; the model can't swap them
            return brief
        except ValidationError as e:
            if stopping.requested():       # no time for a repair round
                raise stopping.Interrupted(stopping.salvage(msg.content, ResearchBrief))
            error = e
            messages.append({"role": "assistant", "content": msg.content})
            messages.append({"role": "user", "content": llm.invalid(e)})
    raise ResearchFailed(f"model could not produce a valid brief: {error}")

async def run_research(m: Mission, now=None, toolbox_factory=Toolbox) -> ResearchBrief:
    """Both steps back to back."""
    return await run_write_brief(m, await run_gather(m, now, toolbox_factory))

def gather(m: Mission, now=None) -> Evidence:
    """Sync entry point the orchestrator calls on the Research Assistant's worker."""
    return asyncio.run(run_gather(m, now))

def write_brief(m: Mission, evidence: Evidence, now=None) -> ResearchBrief:
    """Sync entry point the orchestrator calls on the Principal Investigator's worker."""
    return asyncio.run(run_write_brief(m, evidence))

def research(m: Mission, now=None) -> ResearchBrief:
    return asyncio.run(run_research(m, now))
