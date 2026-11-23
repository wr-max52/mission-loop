"""Mission finder: a few lateral searches around the human's inspiration line, then 1 to 3 new missions.
The small model searches and the main model picks. Code then keeps only ideas that rest on a page the
searches really returned and that the desk isn't already working on."""
import asyncio, random
from pydantic import BaseModel, ValidationError
from contracts import MissionIdea, words
from agents import llm
import stopping
from agents.researcher import SearchRun, tool_loop
from agents.toolbox import Toolbox

MAX_SEARCHES = 5                           # "just a few": this runs on every pass
TIME_LIMIT = 5 * 60                        # seconds the searches may run
MAX_IDEAS = 3
MAX_LISTED = 40                            # existing topics shown to the model
TOO_SIMILAR = 0.6                          # share of meaningful words two topics have in common

# The models run at a low temperature, so the same prompt would search the same way every pass.
# Drawing different angles each time is what keeps the finder wandering.
# The finder explores: a lead may start anywhere, the fringe included. Every lead it proposes then carries the open
# question and the record that could settle it, so research tests the world, not the desk's own ideas.
ANGLES = [
    "Read the fringe: the wildest claim in forums, newsletters and podcasts about this field. It may be the start of a "
    "real story: what is the open question in it, and what record (a filing, a ruling, testimony, data) could settle it?",
    "Find the court case: a lawsuit, ruling, settlement or prosecution in this field the usual story leaves out.",
    "Find the testimony: someone who said it under oath, to a legislature or an inquiry, on the record.",
    "Follow the money through filings: contracts, lobbying and campaign-finance records, annual reports, tax filings.",
    "Find the defector: an insider, retired official or credentialed expert who broke with their field on the record.",
    "Hunt the anomaly in the data: an official dataset, audit or inspection whose number the usual story can't handle.",
    "Dig up a buried precedent: an earlier episode with a paper trail (an inquiry, a recall, a reversal) now forgotten.",
    "Find the official record that contradicts the story: a regulator's finding, an arrest or enforcement record, a FOIA release.",
    "Find who was left out: the people, places or data the standard account never mentions, and the record that shows them.",
]

SEARCH = """You are the mission finder for an independent investigative desk. You do not research stories.
You scout for the ones nobody on the desk has thought of.

The editor's inspiration line: {inspiration}
{today}
First work out what domain and field this line belongs to and what its real focus is. Then go sideways.
Look for what is already being argued in that same field, away from the mainstream: existing theories,
wild claims, heretical minority positions, dissenting experts, and provocative or inconvenient
possibilities that people in the field would rather not discuss.

Take these angles on this pass:
- {angle_1}
- {angle_2}

The desk already has these missions. Look for something different:
{existing}

Rules:
- You have at most {searches} searches. Give the first two to the first angle and the next two to the
  second, then use the last on the strangest thing you have seen so far.
- Every search must chase a different idea, in different words. If a query looks like an earlier one with
  a few words swapped, it is wasted. Name specific people, events, schools of thought or terms of art from
  the results instead of repeating general words.
- Never search the inspiration line as written or the obvious headline version of it. Use the words the
  believers, critics and insiders of this particular field use.
- Stay in the same domain and field as the inspiration line, and close to its main focus.
- A claim does not need to be true or respectable to be worth finding. It does need to exist already:
  someone must really be making it, on a page your searches return.
- Explore. A lead can start anywhere: a fringe post, a podcast, a dissenting expert, an odd number. For
  each one, ask what open question it raises and what record could answer it: has anyone testified to it,
  has a court heard it, is there a filing, an arrest record, an audit or a dataset?
- When you have about three strong leads, stop calling tools and reply with the word DONE."""

PROPOSE = """Now pick the 1 to 3 most surprising leads from the search results above and output them as JSON
matching the schema. Each one becomes a mission for the research desk.
- topic: one specific line: the open question the lead raises, as a question a document can answer (has
  anyone testified that..., was there a ruling on..., do the filings show...). Never a general subject.
- objective: two or three sentences saying what is being claimed, who is claiming it (a fringe source is fine:
  say so), and which records would support or sink it, by kind and where they would be (testimony, court
  filings, arrest or booking records, regulatory or campaign-finance filings, contracts, datasets, FOIA
  responses). Treat the claim as a lead to test, not as true.
- source_url: copied exactly from a search result above, a page showing that this claim is really being made.
- epic: the title of the desk's existing epic (listed below) this lead fits best, copied exactly. Pick the
  closest one even when the fit is loose. Leave it empty only if the desk has no epics yet.
- new_epic: almost always empty. Fill it only when two or more of your leads share a theme that none of
  the existing epics covers; then give each of them the same new title. A title is at most 3 words,
  specific yet thematic: a line of inquiry that can grow, not a goal (e.g. "Fed Policy Capture").
  A new epic on a single lead is thrown away.

The desk's existing epics, each with some of its missions:
{epics}

Prefer what cuts against the usual story over what merely repeats it. Make the leads different from each
other and from the desk's existing missions. Do not propose a mission whose premise accuses a private
person of wrongdoing or blames people for their race, religion or ethnicity."""

class FinderFailed(Exception):
    pass

class SearchDown(FinderFailed):
    """Web search was down (out of credit, key refused, rate limited, unreachable), so there was nothing to pick
    from. Said plainly: the reason is already a sentence."""
    plain = True

    def __init__(self, reason: str):
        super().__init__(f"web search is down ({reason})")
        self.reason = reason

class MissionIdeas(BaseModel):
    missions: list[MissionIdea]

def too_similar(topic: str, others: list[str]) -> bool:
    mine = words(topic)
    for other in others:
        theirs = words(other)
        if mine and theirs and len(mine & theirs) / len(mine | theirs) >= TOO_SIMILAR:
            return True
    return False

def _search_results(messages: list) -> str:
    return "\n".join(m["content"] for m in messages
                     if isinstance(m, dict) and m.get("role") == "tool"
                     and not m["content"].startswith(("ERROR", "Not run:", "Web search is down")))

def _keep(ideas: list[MissionIdea], results: str, topics: list[str]) -> list[MissionIdea]:
    """Only leads the searches really turned up, that the desk doesn't already have. At most MAX_IDEAS."""
    kept: list[MissionIdea] = []
    for idea in ideas:
        url = idea.source_url.strip().rstrip("/")
        if idea.topic.strip() and url.startswith("http") and url in results \
                and not too_similar(idea.topic, topics + [k.topic for k in kept]):
            kept.append(idea)
    return kept[:MAX_IDEAS]

def epic_listing(epics: dict[str, list[str]] | None) -> str:
    if not epics:
        return "- none yet"
    return "\n".join(f"- {title}" + "".join(f"\n    · {t}" for t in topics) for title, topics in epics.items())

async def run_finder(inspiration: str, topics: list[str], now=None, epics: dict[str, list[str]] | None = None,
                     toolbox_factory=Toolbox) -> list[MissionIdea]:
    angle_1, angle_2 = random.sample(ANGLES, 2)
    listed = "\n".join(f"- {t}" for t in topics[-MAX_LISTED:]) or "- none yet"
    today = f"Today's date: {now:%Y-%m-%d}.\n" if now else ""
    system = SEARCH.format(inspiration=inspiration, today=today, angle_1=angle_1, angle_2=angle_2,
                           existing=listed, searches=MAX_SEARCHES)
    async with toolbox_factory(["search"]) as tb:
        if getattr(tb, "skipped", {}).get("search"):
            raise FinderFailed(f"web search is off on this machine: {tb.skipped['search']} (see Settings → Setup)")
        run = SearchRun()
        messages = await tool_loop(system, tb, MAX_SEARCHES, TIME_LIMIT, run=run)
    results = _search_results(messages)
    if not results and run.search_down:
        raise SearchDown(run.search_down)
    if not results:
        raise FinderFailed("the searches returned nothing")

    messages.append({"role": "user", "content": PROPOSE.format(epics=epic_listing(epics))})
    error = None
    for _ in range(2):                     # one repair attempt for malformed JSON
        try:
            msg = await llm.chat(messages, format=MissionIdeas.model_json_schema())
        except llm.Cut as cut:             # stopped: keep whatever leads got written
            ideas = stopping.salvage(cut.text, MissionIdeas)
            return _keep(ideas.missions, results, topics) if ideas else []
        except llm.Overrun as e:           # stuck: keep the leads it wrote before, if any
            ideas = stopping.salvage(e.text, MissionIdeas)
            if ideas is None:
                raise FinderFailed(f"the finder got stuck: {e}")
            return _keep(ideas.missions, results, topics)
        try:
            return _keep(MissionIdeas.model_validate_json(msg.content).missions, results, topics)
        except ValidationError as e:
            error = e
            messages.append({"role": "assistant", "content": msg.content})
            messages.append({"role": "user", "content": llm.invalid(e)})
    raise FinderFailed(f"model could not produce valid missions: {error}")

def find_missions(inspiration: str, topics: list[str], now=None,
                  epics: dict[str, list[str]] | None = None) -> list[MissionIdea]:
    """Sync entry point the orchestrator calls. epics: active epic title -> a few of its mission topics."""
    return asyncio.run(run_finder(inspiration, topics, now, epics))
