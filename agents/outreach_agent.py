"""The Outreach agent: after a human (or auto mode) approves a brief, the Principal Investigator's model finds up to
settings.outreach_contacts() people who can answer a specific question about it, and asks each one through
send_outreach (the outreach MCP server, outreach_tools.py) on a channel that is set up.

It never writes a message: it gives a channel, an address, a first name, a topic and a question, and code
(contacts.py) fills the template, checks, sends and records it, or says why it didn't and what to fix, so the agent
tries again or picks someone else. Code also refuses, before the tool runs, an email address that wasn't on any
page or search result it read in this run, so addresses are found, never made up.

Outreach is best effort: a run that asks nobody, crashes or runs out of time just moves the mission on to writing.
A stop ends the run with whoever was asked so far kept; the next start carries on, told who was already asked.
"""
import asyncio
import contacts, settings, stopping
from agents.researcher import Guard, SearchRun, tool_loop
from agents.toolbox import Toolbox
from contracts import Mission, OutreachRecord, ResearchBrief

MAX_CALLS = 30                             # tool calls in one run
TIME_LIMIT = 10 * 60                       # seconds
SERVERS = ["search", "fetch", "pages", "outreach"]

SYSTEM = """You are the Outreach lead on a research desk run by AI agents. A brief was approved; before the article is
written, you ask up to {n} real people one specific question each, so the article can include what they say.
{today}
Topic: {topic}
Thesis: {thesis}
Claims:
{claims}
Counterpoint: {counterpoint}
Sources:
{sources}

Who to ask: someone who would know first-hand: an author of a source above, a person or organisation named in a
claim, an official, a researcher or a reporter who covered it. Ask each one the question they can answer best.

Channels you can use: {channels}
- email: only an address printed on a page you fetched or in a search result you got in this run. Never guess or
  build an address from a name. A press office or a named person's own address is best.
- bluesky: a handle that find_on_bluesky returns (check the bio matches the person) or that a page links to.
- 1f916: only people list_1f916_commenters lists.

For each person call send_outreach with:
- channel and address (as above),
- name: their first name only (e.g. "Jane"), or a title and surname ("Dr. Smith"),
- topic: a few words naming what the mission is about, e.g. "the 2024 Texas grid outage",
- question: one short, very specific question about a claim that they can answer: did X happen, was Y done, who
  was involved, when. Ending in "?". No preamble, no links.
Code writes the message from your fields, exactly:
"Hi <name>, I'm an AI Agent on a research mission about <topic> and I need your help answering this:
<question>
Appreciate your help! You can learn more about my missions here <our site>

From,
<our publication>, AI Agent"
Keep it brief. If send_outreach answers "Not sent", read why, fix it and call it again, or pick someone else.
When {n} people have been asked, or nobody else fits, stop calling tools and reply DONE.
{asked}"""

class OutreachGuard(Guard):
    """The search's Guard, plus: an email address the run never saw on a page or in a search result (the Guard's
    seen record) is refused before send_outreach runs, and the agent is told to find one first."""
    def refuse(self, name: str, args: dict) -> str | None:
        if name == "send_outreach" and str(args.get("channel", "")).strip().lower() == "email":
            address = str(args.get("address", "")).strip().removeprefix("mailto:").lower()
            if address and address not in self.seen:
                self.in_a_row += 1
                return (f"Not sent: {address} isn't on any page or search result you read in this run, so it may not "
                        "be real. Fetch a page that shows the address first, or use another channel.")
        return super().refuse(name, args)

    def explain(self, name: str, args: dict, result: str) -> str:
        if name == "send_outreach":        # our own tool's reply shows no one's address
            return result
        return super().explain(name, args, result)

def _asked(done: list[OutreachRecord]) -> str:
    if not done:
        return ""
    lines = "\n".join(f"- {r.name} ({r.address}, {r.channel}): {r.question}" for r in done)
    return f"\nAlready asked about this mission (don't ask them again):\n{lines}\n"

def prompt(m: Mission, brief: ResearchBrief, done: list[OutreachRecord], channels: list[str], now=None) -> str:
    claims = "\n".join(f"{i}. ({c.kind}) {c.text} [{', '.join(c.source_ids)}]" for i, c in enumerate(brief.claims, 1))
    sources = "\n".join(f"- [{s.id}] {s.title}: {s.url}" for s in brief.sources)
    names = "; ".join(f"{c} ({contacts.addresses(c)})" for c in channels)
    return SYSTEM.format(n=settings.outreach_contacts() - len(done), topic=m.topic, thesis=brief.thesis,
                         claims=claims, counterpoint=brief.counterpoint, sources=sources, channels=names,
                         today=f"Today's date: {now:%Y-%m-%d}." if now else "", asked=_asked(done))

async def run_outreach(m: Mission, brief: ResearchBrief, now=None, toolbox_factory=Toolbox) -> list[OutreachRecord]:
    """One run. Returns everyone asked about this mission (this run and before). Raises stopping.Interrupted with
    them when the stop button cut it short."""
    done = contacts.records(m.slug)
    channels = contacts.ready_channels()
    if not channels or len(done) >= settings.outreach_contacts():
        return done
    servers = [s for s in SERVERS if s != "search" or "search" in m.tools]
    async with toolbox_factory(servers, env={"MISSION_LOOP_MISSION": m.slug}) as tb:
        if "send_outreach" not in {t["function"]["name"] for t in tb.tools}:
            raise RuntimeError(f"the outreach tool server didn't start ({tb.skipped.get('outreach', 'unknown')})")
        run = SearchRun()
        guard = OutreachGuard([t["function"]["name"] for t in tb.tools])
        await tool_loop(prompt(m, brief, done, channels, now), tb, MAX_CALLS, TIME_LIMIT, run=run, role="pi",
                        guard=guard)
    found = contacts.records(m.slug)
    if run.stopped:
        raise stopping.Interrupted(found)
    return found

def reach_out(m: Mission, brief: ResearchBrief, now=None) -> list[OutreachRecord]:
    """Sync entry point the orchestrator calls on the Principal Investigator's worker."""
    return asyncio.run(run_outreach(m, brief, now))
