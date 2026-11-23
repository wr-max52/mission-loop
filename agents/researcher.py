"""Research agent, in two steps that can run in different passes and side by side with other missions:
gather (the Research Assistant's model finds sources) and write_brief (the Principal
Investigator's model writes a ResearchBrief from what was gathered)."""
import asyncio, json, re, time
from urllib.parse import urlparse
import activity, checkpoint, related, search_status, stopping, trail
from contracts import ClaimSet, Evidence, Mission, POSSIBILITY, ResearchBrief, SourceFix, SourcePick
from agents import llm
from checks import validator
from agents.toolbox import Toolbox

MAX_TOOL_CALLS = 40                        # sources run
NOT_WEB = {"wiki_search"}                  # on the search server, but its failure says nothing about web search
MAX_REFUSED = 3                            # calls refused in a row (repeats, unreadable sites) before the run ends
LOW_BUDGET = 10                            # from here on every result says how many calls are left
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
- Use web_search and news_search to find pages, then fetch them. Set freshness ("day", "week", "month",
  "year") only for recent events; leave it empty when looking for older ones.
- wiki_search gives background on a specific name, place or term, and the sources Wikipedia cites. It is
  not reliable on its own: fetch and cite those sources, not Wikipedia, for anything that matters.
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
- Do not look for people to contact; the Outreach agent does that after the brief is approved.
- When you have enough evidence, stop calling tools and reply with the word DONE.
{notes}"""

# The small model gathers the evidence; the main model makes these calls when it writes the brief.
KIND_RULE = ("Treat every source above as part truth, part spin. Drop only what specific evidence contradicts "
             "or nothing above gives a reason to consider; a denial does not rule anything out. Set kind on "
             "every remaining claim: \"fact\" only for what the record directly shows (what a document says, "
             "what someone said, what an outlet reported), \"possibility\" for what the evidence leaves open, "
             "worded \"It is possible that ..., based on ...\". Write at least 3 pointed possibilities, more "
             "than facts, and keep at least one fact. The counterpoint is the strongest case against the thesis: from "
             "a contradicting source if one was found, else the objection a skeptic would raise; never leave it empty.")
IDS = "Give each source a short id such as s1, s2 (never its URL) and cite sources in claims by that id. "
SOURCE_ROUNDS = 3                          # source checks: the first pick, then up to 2 rounds of fixes
CLAIM_ROUNDS = 3                           # claim checks: the first draft, then up to 2 rounds of fixes
PICK = ("The search is over. The brief is written in two steps; this is step 1 of 2: choose its sources. Output "
        "JSON matching the schema: the thesis (follow the rules on the thesis above), and every page you fetched "
        "whose evidence bears on it, each with its stance and one sentence copied character for character from "
        "that page as its quote. Sources that passed verification last time count as fetched. Do not write claims "
        "yet. Code checks every quote against its page before step 2, and step 2 may cite only sources that pass. "
        "Include at least 3 supporting sources on 3 different websites, and any page that contradicts the thesis. " + IDS)
CLAIMS = """Step 2 of 2: write the claims and the counterpoint. Code checked the quotes of the sources
below against their pages, and these passed. Cite only these ids.
Thesis: {thesis}
Verified sources:
{sources}
{kind_rule} Every claim cites the verified source ids whose pages show it. Write each claim once.{revise}
Output JSON matching the schema."""
REVISE_CLAIMS = (" The last brief failed its check: its claims and the problems found are in the instructions "
                 "above. Keep the claims that held up and fix the rest.")

class ResearchFailed(Exception):
    pass

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

def _tool_servers(m: Mission) -> list[str]:
    """Saved missions list only search and fetch, so anything that can fetch also gets the page tools."""
    return m.tools + ["pages"] if "fetch" in m.tools and "pages" not in m.tools else m.tools

STATUS = re.compile(r"status(?: code)? (\d{3})")
BUDGET_NOTE = re.compile(r"\n\[\d+ tool calls? left[^\]]*\]$")

def _domain(url) -> str:
    return (urlparse(str(url or "")).hostname or "").removeprefix("www.")

class Guard:
    """Code's side of the search loop. A small model learns little from a bare error and often repeats the
    call that failed, so calls that can't help are answered before they run (with what to do instead), and a
    failed fetch says what it means for the rest of the search."""
    def __init__(self, tool_names: list[str]):
        self.archive = "wayback_versions" in tool_names
        self.finder = "find_in_page" in tool_names
        self.blocked = validator.blocked_domains()
        self.refused: dict[str, str] = {}  # site -> why it can't be read, learned this run
        self.searches: set[str] = set()
        self.last = None
        self.in_a_row = 0                  # calls refused since the last one that ran
        self.paging_tip = True
        self.search_tools: set[str] = set()   # the web search tools (from the "search" server); empty: told by name
        self.search_down: search_status.Outage | None = None   # web search failed this run: no more searches
        self.rate_retried = False          # a rate-limited search gets one retry per run

    def is_search(self, name: str) -> bool:
        """A web search tool (not wiki_search, find_in_page or the library's search): its outage stops searching."""
        if name in NOT_WEB:
            return False
        return name in self.search_tools if self.search_tools else ("web_search" in name or "news_search" in name)

    def _instead(self) -> str:
        return ("its Wayback Machine copy (wayback_versions), or " if self.archive else "") + "another site"

    def refuse(self, name: str, args: dict) -> str | None:
        """Why this call won't run, or None to run it."""
        key = json.dumps([name, args], sort_keys=True, default=str)
        site = _domain(args.get("url")) if name == "fetch" else ""
        why = None
        if key == self.last:
            why = ("Not run: this is the same call you just made, and its result is above. Use what it found: "
                   "fetch a page from it, or search with different words.")
        elif "search" in name and key in self.searches:
            why = "Not run: you already ran this exact search and its results are above. Search with different words."
        elif site and validator._is_blocked(site, self.blocked):
            why = f"Not run: {site} is on the blocked list, so it can't be cited. Use {self._instead()}."
        elif site in self.refused:
            why = f"Not run: {site} {self.refused[site]} earlier in this search. Use {self._instead()}."
        elif self.search_down and self.is_search(name):
            why = "Not run: " + search_status.note(self.search_down)
        if why:
            self.in_a_row += 1
            return why
        self.last, self.in_a_row = key, 0
        if "search" in name:
            self.searches.add(key)
        return None

    def explain(self, name: str, args: dict, result: str) -> str:
        """The result, with what a failure means and what to try next."""
        if name != "fetch":
            return result
        if not result.startswith("ERROR"):
            if args.get("start_index") and self.paging_tip and self.finder:
                self.paging_tip = False
                return result + ("\n[Tip: find_in_page searches a whole page for the words you need, "
                                 "so you don't have to page through it.]")
            return result
        site, low = _domain(args.get("url")), result.lower()
        found = STATUS.search(low)          # the status the site answered with, not digits in its address
        status = found.group(1) if found else ""
        if status in ("401", "403") or "received status 403" in low:
            self.refused[site] = "refused automated reading"
            hint = (f"{site} refuses automated reading, so none of its pages can be read or cited. Don't fetch it "
                    f"again. Use {self._instead()}.")
        elif "autonomous fetching" in low:   # robots.txt rules out this page, maybe not the whole site
            hint = f"This site doesn't allow this page to be read automatically. Use {self._instead()}."
        elif "timed out" in low or "timeout" in low:
            hint = f"The page took too long to load. Use {self._instead()}."
        elif status in ("404", "410"):
            hint = "There is no page at this address. Check the address" + (
                ", or look for an archived copy with wayback_versions." if self.archive else ".")
        else:
            hint = f"This page can't be read. Use {self._instead()}."
        return result.splitlines()[0][:300] + "\n" + hint

def _budget_note(left: int, time_limit: float | None, started: float) -> str:
    """Once the budget runs low, each result says what is left, so the model wraps up instead of being cut off."""
    minutes = None if time_limit is None else max(0, (time_limit - (time.monotonic() - started)) / 60)
    if left > LOW_BUDGET and (minutes is None or minutes > time_limit / 60 / 3):
        return ""
    when = f"; about {minutes:.0f} minutes left" if minutes is not None else ""
    return f"\n[{left} tool call{'s' if left != 1 else ''} left{when}. Read what matters most, then reply DONE.]"

class SearchRun:
    """How far a search got: tool calls run, seconds spent, and whether the stop button ended it (so it
    carries on next time instead of counting as done)."""
    def __init__(self, calls: int = 0, seconds: float = 0, search_down: str = ""):
        self.calls, self.seconds, self.stopped = calls, seconds, False
        self.search_down = search_down     # why web search failed during this search, if it did

async def _search(guard: Guard, tb, name: str, args: dict, run: SearchRun) -> str:
    """Run one web search. An outage (out of credit, key refused, rate limited after one retry, unreachable) is
    recorded once and answered with a plain note instead of the raw error, and the run searches no more; a search
    that works clears a recorded outage."""
    result = await tb.call(name, args)
    outage = search_status.classify(result)
    if outage and outage.kind == "rate" and not guard.rate_retried:
        guard.rate_retried = True
        await asyncio.sleep(search_status.RATE_WAIT)
        result = await tb.call(name, args)
        outage = search_status.classify(result)
    if outage:
        guard.search_down, run.search_down = outage, outage.reason
        if search_status.record(outage):
            print(f"web search is down: {outage.reason}", flush=True)
        trail.record("note", f"web search is down: {outage.reason}")
        return search_status.note(outage)
    if not result.startswith("ERROR") and search_status.clear():
        print("web search works again", flush=True)
    return result

CARRY_ON = ("(The search was paused here and now carries on. Continue from where you were: you have the results "
            "above; don't repeat calls you already made.)")

def _calls_made(messages: list) -> int:
    """Tool calls that ran in a saved transcript (refused ones didn't use the budget)."""
    return sum(1 for m in messages if m.get("role") == "tool" and not (m.get("content") or "").startswith("Not run:"))

async def tool_loop(system: str, tb, budget: int, time_limit: float | None = None, *, start: list | None = None,
                    run: SearchRun | None = None, saved=None, role: str = "assistant", guard: Guard | None = None) -> list:
    """One run of the small model: search and fetch until it stops or the budget or time runs out.
    Running out of time works like running out of budget: the run keeps what it found so far.
    So does the stop button: the turn in flight may finish, then the run ends without its tool calls, and
    run.stopped says so. start carries on a saved transcript, with the budget and time it has left (run.calls,
    run.seconds); saved(messages), if given, is called after every tool result, so a killed loop loses nothing.
    role picks the worker whose model runs it (the Outreach agent runs on the Principal Investigator's); guard
    replaces the search's Guard."""
    run = run or SearchRun()
    if start:
        messages = list(start) + [{"role": "user", "content": CARRY_ON}]
    else:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": "Begin."}]
    calls, spent = run.calls, run.seconds
    guard = guard or Guard([t["function"]["name"] for t in tb.tools])
    guard.search_tools = {t for t, server in (getattr(tb, "server", None) or {}).items() if server == "search"}
    model = llm.model_for(role)
    started = time.monotonic() - spent
    left = None if time_limit is None else max(time_limit - spent, 1)
    try:
        async with asyncio.timeout(left) as limit:
            while calls < budget:
                if stopping.requested():
                    run.stopped = True
                    break
                if guard.in_a_row >= MAX_REFUSED:   # stuck on calls that can't run: end the run with what it found
                    print("search stopped: it kept making calls that can't help; continuing with what it found", flush=True)
                    trail.record("note", "search stopped: it kept making calls that can't help")
                    break
                messages = llm.compact(messages, llm.TURN_TOKENS, model)   # kept cut: the next turns reuse the prompt cache
                try:
                    msg = await llm.chat(messages, tools=tb.tools, model=model)
                except llm.Cut:
                    run.stopped = True
                    break
                except llm.Overrun as e:   # a stuck turn ends the run; what it found is kept
                    print(f"search stopped: {e}; continuing with what it found", flush=True)
                    break
                if msg.tool_calls and stopping.requested():
                    run.stopped = True
                    break                  # its tool calls would never be answered
                messages.append(msg)
                if not msg.tool_calls:
                    break
                for tc in msg.tool_calls:
                    if calls >= budget:    # every call gets an answer, or some chat templates break
                        messages.append({"role": "tool", "tool_name": tc.function.name,
                                         "content": "Not run: the tool budget is used up."})
                        continue
                    args = dict(tc.function.arguments or {})
                    if why := guard.refuse(tc.function.name, args):   # answered without running: it can't help
                        trail.record("note", why)
                        messages.append({"role": "tool", "tool_name": tc.function.name, "content": why})
                        continue
                    calls += 1
                    raw = (await _search(guard, tb, tc.function.name, args, run) if guard.is_search(tc.function.name)
                           else await tb.call(tc.function.name, args))
                    result = guard.explain(tc.function.name, args, raw)
                    trail.record("tool_result", f"{tc.function.name}: {result}")
                    messages.append({"role": "tool", "tool_name": tc.function.name,
                                     "content": result + _budget_note(budget - calls, time_limit, started)})
                run.calls, run.seconds = calls, time.monotonic() - started
                if saved:
                    saved(messages)        # every answered turn is on disk before the next one starts
    except TimeoutError:
        if not limit.expired():            # some other timeout inside the call: a real failure
            raise
        print(f"search stopped at its {time_limit / 60:g}-minute limit; continuing with what it found", flush=True)
        trail.record("note", f"search stopped at its {time_limit / 60:g}-minute limit")
    run.calls, run.seconds = calls, time.monotonic() - started
    return messages

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
            text = BUDGET_NOTE.sub("", text)   # for the search, not the brief
            m["content"] = text
            if text.startswith(FAILED):
                m["content"] = text.splitlines()[0][:200]
            elif text in seen:
                m["content"] = "(same result as an earlier call)"
            seen.add(text)
        out.append(m)
    return out

async def run_gather(m: Mission, now=None, toolbox_factory=Toolbox, resume: Evidence | None = None) -> Evidence:
    """The Research Assistant's step: search and read until done, out of budget or time, or stopped. The
    transcript is saved after every tool result; a search the stop button (or a killed loop) cut short comes
    back unfinished and carries on from it next time (resume), with the budget and time it has left."""
    today = f"Today's date: {now:%Y-%m-%d}. Prefer current information.\n" if now else ""
    system = SYSTEM.format(topic=m.topic, objective=m.objective or m.topic, today=today, notes=_notes(m),
                           past=related.reference_block(m))
    start = resume.messages if resume and resume.messages else None
    run = SearchRun(_calls_made(start) if start else 0, resume.seconds if start else 0,
                    resume.search_down if start else "")
    if start:
        trail.record("note", f"search carries on after {run.calls} tool call(s)")

    def evidence(messages, finished: bool) -> Evidence:
        return Evidence(messages=[_plain(x) for x in messages], finished=finished,
                        attempt=m.attempts, seconds=run.seconds, search_down=run.search_down)

    async with toolbox_factory(_tool_servers(m)) as tb:
        messages = await tool_loop(system, tb, MAX_TOOL_CALLS, SOURCE_TIME_LIMIT, start=start, run=run,
                                   saved=lambda msgs: checkpoint.save(m, evidence=evidence(msgs, False)))
        if run.stopped:
            return evidence(messages, False)
    return evidence(messages, True)

def _unique(claims: list) -> list:
    """Claims with repeats left out, first copy kept."""
    out, seen = [], set()
    for c in claims:
        key = " ".join(c.text.split())
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out

class BriefFailed(ResearchFailed):
    """The brief can't pass: brief is the draft so far (kept for the next attempt and for humans), notes
    say why in the validator's words."""
    def __init__(self, brief: ResearchBrief, notes: str):
        super().__init__(notes)
        self.brief, self.notes = brief, notes

def _source_lines(sources) -> str:
    return "\n".join(f'- {s.id} ({s.stance}) {s.title} - {s.url}\n  quote: "{s.quote}"' for s in sources)

def _source_feedback(passed: list, failed: list[str]) -> str:
    return ("Code checked every quote against its page.\n"
            f"Passed, and kept exactly as they are: {', '.join(s.id for s in passed) or 'none'}.\n"
            "Failed:\n" + "\n".join(f"- {p}" for p in failed) + "\n"
            "For each failed source, return a fixed version (same id) or a replacement (a new id) from another "
            "page you fetched, or leave it out to drop it. A quote is one sentence copied character for "
            "character from the page; where the check shows the closest sentence actually on the page, copy that "
            "sentence if it still shows what you need. Return only the fixed or replacement sources, as JSON "
            "matching the schema.")

def _save_pick(m: Mission, thesis: str, sources: list) -> None:
    """The source pick as it stands, saved before the (slow, network-bound) checks, so a stop or a killed loop
    resumes by checking it again instead of asking the model again."""
    m.brief_pick = SourcePick(thesis=thesis, sources=sources)
    checkpoint.save(m)

async def _pick_sources(m: Mission, messages: list) -> SourcePick:
    """Step 1: the thesis and its sources. Code checks every quote straight away; the failed ones go back
    with what the check found (the closest real sentence, an archived copy) for SOURCE_ROUNDS - 1 rounds of
    fixes, then the ones still failing are dropped. Too few left is a failed attempt: the search must find more.
    A pick saved by an earlier, stopped run (Mission.brief_pick) is checked without asking the model again."""
    messages = messages + [{"role": "user", "content": PICK}]
    if m.brief_pick:
        pick = m.brief_pick
        trail.record("note", "brief step 1: checking the sources picked before the stop")
    else:
        trail.record("note", "brief step 1: choosing the sources")
        pick = await llm.ask(messages, SourcePick, "source picker", ResearchFailed)
        _save_pick(m, pick.thesis, pick.sources)
    blocked, passed, failed = validator.blocked_domains(), [], []
    todo = pick.sources
    for round in range(SOURCE_ROUNDS):
        seen = {s.id for s in passed}
        failed = []
        for s in todo:
            if s.id in seen:
                continue                   # a passed source can't be swapped out by a fix
            if stopping.requested():       # each check can take seconds: stop between them, the pick is saved
                raise stopping.Interrupted(None)
            seen.add(s.id)
            problem, url = validator.source_problem(s, blocked, archive=True)
            if problem:
                failed.append((s, problem))
            else:
                passed.append(s.model_copy(update={"url": url}))   # an archived copy if the live page refused us
        if not failed or round == SOURCE_ROUNDS - 1:
            break
        if stopping.requested():           # no fix round now; the pick so far is saved and checked next time
            raise stopping.Interrupted(None)
        trail.record("note", f"{len(failed)} source(s) failed the check; asking for fixes")
        messages += [{"role": "assistant", "content": SourcePick(thesis=pick.thesis, sources=todo).model_dump_json()},
                     {"role": "user", "content": _source_feedback(passed, [p for _, p in failed])}]
        todo = (await llm.ask(messages, SourceFix, "source picker", ResearchFailed)).sources
        _save_pick(m, pick.thesis, passed + todo)   # passed ones are checked again on a resume: cheap, and certain
    domains = validator.support_domains(passed)
    if len(domains) < validator.MIN_SUPPORT_DOMAINS:
        notes = [p for _, p in failed] + [
            f"Only {len(domains)} verified supporting domain(s); need {validator.MIN_SUPPORT_DOMAINS}. Find "
            "supporting pages on other websites, and copy each quote exactly from the page"]
        draft = ResearchBrief(thesis=pick.thesis, sources=passed + [s for s, _ in failed], counterpoint="", claims=[])
        raise BriefFailed(draft, "\n".join(notes))
    if failed:
        trail.record("note", f"dropped {len(failed)} source(s) that still failed: {', '.join(s.id for s, _ in failed)}")
    return SourcePick(thesis=pick.thesis, sources=passed)

def _settle_claims(claims: ClaimSet, ids: set[str], final: bool) -> tuple[ClaimSet, list[str]]:
    """Code's part of step 2. What it can fix safely it fixes: repeats go, and a claim worded as a
    possibility is labeled one (never the other way). The rest is fed back; on the final round, claims that
    still break a rule are dropped rather than failing the brief over them."""
    kept = []
    for c in _unique(claims.claims):
        if c.kind == "fact" and POSSIBILITY.search(c.text):
            c = c.model_copy(update={"kind": "possibility"})
        broken = (not c.source_ids or any(i not in ids for i in c.source_ids)
                  or (c.kind == "possibility" and not POSSIBILITY.search(c.text)))
        if not (final and broken):
            kept.append(c)
    claims = claims.model_copy(update={"claims": kept})
    problems = validator.claim_problems(claims.claims, ids)
    if not claims.counterpoint.strip():
        problems.append(validator.EMPTY_COUNTERPOINT)
    return claims, problems

async def _write_claims(m: Mission, messages: list, pick: SourcePick) -> ClaimSet:
    """Step 2: claims and the counterpoint, from the transcript and only the sources that passed."""
    trail.record("note", f"brief step 2: writing claims from {len(pick.sources)} verified sources")
    ids = {s.id for s in pick.sources}
    ask = CLAIMS.format(thesis=pick.thesis, sources=_source_lines(pick.sources), kind_rule=KIND_RULE,
                        revise=REVISE_CLAIMS if m.rejected_brief else "")
    messages = messages + [{"role": "user", "content": ask}]
    for round in range(CLAIM_ROUNDS):
        final = round == CLAIM_ROUNDS - 1
        claims = await llm.ask(messages, ClaimSet, "claim writer", ResearchFailed)
        claims, problems = _settle_claims(claims, ids, final)
        if not problems or final or stopping.requested():
            return claims
        trail.record("note", f"claims: {len(problems)} problem(s); asking for a fix")
        messages += [{"role": "assistant", "content": claims.model_dump_json()},
                     {"role": "user", "content": "Code found these problems:\n" + "\n".join(f"- {p}" for p in problems)
                      + "\nReturn the whole JSON again with every problem fixed."}]
    return claims

async def run_write_brief(m: Mission, evidence: Evidence) -> ResearchBrief:
    """The Principal Investigator's step, in two parts so neither reply is long and the first is kept:
    choose and check the sources (saved on the mission as brief_sources), then write the claims citing only
    those. A stop or crash after the first part resumes at the second."""
    activity.begin(m.slug, "brief")
    messages = _for_brief(evidence.messages)
    pick = m.brief_sources
    if pick is None:
        try:
            pick = await _pick_sources(m, messages)
        except stopping.Interrupted:
            raise stopping.Interrupted(None)   # the saved pick (if the model got one out) is checked next time
        m.brief_sources, m.brief_pick = pick, None
        checkpoint.save(m)                 # step 1 is done and on disk
        if stopping.requested():           # don't start step 2 only for the grace to cut it off
            raise stopping.Interrupted(None)
    try:
        claims = await _write_claims(m, messages, pick)
    except stopping.Interrupted as stop:   # a part-written claim set still makes a draft worth checking
        raise stopping.Interrupted(_brief(pick, stop.draft, evidence) if stop.draft else None)
    return _brief(pick, claims, evidence)

def _brief(pick: SourcePick, claims: ClaimSet, evidence: Evidence) -> ResearchBrief:
    return ResearchBrief(thesis=pick.thesis, sources=pick.sources, counterpoint=claims.counterpoint,
                         claims=_unique(claims.claims))

async def run_research(m: Mission, now=None, toolbox_factory=Toolbox) -> ResearchBrief:
    """Both steps back to back."""
    return await run_write_brief(m, await run_gather(m, now, toolbox_factory))

def gather(m: Mission, now=None, resume: Evidence | None = None) -> Evidence:
    """Sync entry point the orchestrator calls on the Research Assistant's worker."""
    return asyncio.run(run_gather(m, now, resume=resume))

def write_brief(m: Mission, evidence: Evidence, now=None) -> ResearchBrief:
    """Sync entry point the orchestrator calls on the Principal Investigator's worker."""
    return asyncio.run(run_write_brief(m, evidence))

def research(m: Mission, now=None) -> ResearchBrief:
    return asyncio.run(run_research(m, now))
