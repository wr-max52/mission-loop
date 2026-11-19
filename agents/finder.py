"""Mission finder: a few lateral searches around the human's inspiration line, then 1 to 3 new missions.
The small model searches and the main model picks. Code then keeps only ideas that rest on a page the
searches really returned and that the desk isn't already working on."""
import asyncio, random, re
from pydantic import BaseModel, ValidationError
from contracts import MissionIdea
from agents import llm
from agents.researcher import tool_loop
from agents.toolbox import Toolbox

MAX_SEARCHES = 5                           # "just a few": this runs on every pass
TIME_LIMIT = 5 * 60                        # seconds the searches may run
MAX_IDEAS = 3
MAX_LISTED = 40                            # existing topics shown to the model
TOO_SIMILAR = 0.6                          # share of meaningful words two topics have in common

# The models run at a low temperature, so the same prompt would search the same way every pass.
# Drawing different angles each time is what keeps the finder wandering.
ANGLES = [
    "Invert the consensus: find people arguing the accepted story in this field is backwards.",
    "Follow the money: who gains if the accepted story stays accepted, and who is saying so.",
    "Dig up a buried precedent: an earlier episode in this field that was dropped, reversed or forgotten.",
    "Find the defector: an insider, retired official or credentialed expert who broke with their own field.",
    "Hunt the anomaly: a number, date or result that the usual explanation in this field cannot handle.",
    "Read the fringe: the wildest claim circulating in forums, newsletters and podcasts about this field.",
    "Trace the side effect: an inconvenient consequence of a popular idea here that its supporters avoid.",
    "Find who was left out: the people, places or data the standard account of this field never mentions.",
    "Borrow a pattern: something well known in a neighbouring corner of this field that nobody applies here.",
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
- When you have about three strong leads, stop calling tools and reply with the word DONE."""

PROPOSE = """Now pick the 1 to 3 most surprising leads from the search results above and output them as JSON
matching the schema. Each one becomes a mission for the research desk.
- topic: one specific line naming the theory, claim or possibility to investigate, as a question. Never a
  general subject.
- objective: two or three sentences saying what is being claimed, who is claiming it, and what evidence
  would support or sink it. Treat the claim as a lead to test, not as true.
- source_url: copied exactly from a search result above, a page showing that this claim is really being made.
Prefer what cuts against the usual story over what merely repeats it. Make the leads different from each
other and from the desk's existing missions. Do not propose a mission whose premise accuses a private
person of wrongdoing or blames people for their race, religion or ethnicity."""

class FinderFailed(Exception):
    pass

class MissionIdeas(BaseModel):
    missions: list[MissionIdea]

def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 3}

def too_similar(topic: str, others: list[str]) -> bool:
    words = _words(topic)
    for other in others:
        theirs = _words(other)
        if words and theirs and len(words & theirs) / len(words | theirs) >= TOO_SIMILAR:
            return True
    return False

def _search_results(messages: list) -> str:
    return "\n".join(m["content"] for m in messages
                     if isinstance(m, dict) and m.get("role") == "tool" and not m["content"].startswith("ERROR"))

def _keep(ideas: list[MissionIdea], results: str, topics: list[str]) -> list[MissionIdea]:
    """Only leads the searches really turned up, that the desk doesn't already have. At most MAX_IDEAS."""
    kept: list[MissionIdea] = []
    for idea in ideas:
        url = idea.source_url.strip().rstrip("/")
        if idea.topic.strip() and url.startswith("http") and url in results \
                and not too_similar(idea.topic, topics + [k.topic for k in kept]):
            kept.append(idea)
    return kept[:MAX_IDEAS]

async def run_finder(inspiration: str, topics: list[str], now=None, toolbox_factory=Toolbox) -> list[MissionIdea]:
    angle_1, angle_2 = random.sample(ANGLES, 2)
    listed = "\n".join(f"- {t}" for t in topics[-MAX_LISTED:]) or "- none yet"
    today = f"Today's date: {now:%Y-%m-%d}.\n" if now else ""
    system = SEARCH.format(inspiration=inspiration, today=today, angle_1=angle_1, angle_2=angle_2,
                           existing=listed, searches=MAX_SEARCHES)
    async with toolbox_factory(["search"]) as tb:
        messages = await tool_loop(system, tb, MAX_SEARCHES, TIME_LIMIT)
    results = _search_results(messages)
    if not results:
        raise FinderFailed("the searches returned nothing")

    messages.append({"role": "user", "content": PROPOSE})
    error = None
    for _ in range(2):                     # one repair attempt for malformed JSON
        msg = await llm.chat(messages, format=MissionIdeas.model_json_schema())
        try:
            return _keep(MissionIdeas.model_validate_json(msg.content).missions, results, topics)
        except ValidationError as e:
            error = e
            messages.append({"role": "assistant", "content": msg.content})
            messages.append({"role": "user", "content": f"That JSON was invalid:\n{e}\nReturn corrected JSON only."})
    raise FinderFailed(f"model could not produce valid missions: {error}")

def find_missions(inspiration: str, topics: list[str], now=None) -> list[MissionIdea]:
    """Sync entry point the orchestrator calls."""
    return asyncio.run(run_finder(inspiration, topics, now))
