"""Theorist: the desk's mastermind, at the very start of the flow. One structured call on the Principal
Investigator's model, no tools: it reads the whole desk (every epic with its missions, the missions in no epic,
the latest published articles and the possibilities they left open, and how the inspiration line got here)
and looks for patterns inside an epic and themes emerging across epics.

Before it answers it searches the desk's library (library.py, through its MCP server "library"): code first looks up
the inspiration line itself, then the Theorist may run up to LOOKUPS searches of its own for what a pattern it
suspects rests on (the claims, quotes and article sections behind the desk's one-line summaries). What it found goes
with the request for its theory. A library that fails or runs out of time never stops the Theorist; it answers from
the desk alone and says so.

It may evolve the inspiration line and propose at most one new mission (in an existing epic or none). It
also connects dots: it may propose combining 2 to MAX_MEMBERS open missions into one bigger mission (at most one
combine a turn), and link up to MAX_LINKS pairs while keeping them separate: two open missions, or an open
mission and a published article. Code holds it to that: the line may only ask questions and must keep at least
KEEP of the current line's words (an evolution, not a rewrite), a new mission must not repeat one the desk has, a
combine takes only missions not yet being written, and a link needs an open mission and a reason. The orchestrator
applies what is left (orchestrator.apply_theory).

New epics come only from the board turn (#131): every BOARD_EVERY-th turn, which arranges the cork board, may also
propose one new epic (MAX_EPICS) that gathers at least MIN_EXISTING missions already on the desk, with up to
MAX_NEW_MISSIONS new ones, MIN_EPIC_MISSIONS in all, and only while no other proposed epic waits for review."""
import asyncio, math, re
from pathlib import Path
from pydantic import BaseModel, ValidationError
import board, library, settings, store, trail
from contracts import Mission, State, ResearchBrief, Article, COMBINABLE, epic_title, is_open, words
from agents import llm
from agents.finder import too_similar
from agents.toolbox import Toolbox
import stopping

KEEP = 0.5                                 # share of the current line's meaningful words the new line keeps
MAX_LINE = 500                             # characters, as in the UI's box
MAX_QUESTIONS = 3
MAX_PATTERNS = 4
MAX_EPICS = 1                              # new epics a board turn may propose (ideas turns propose none, #131)
MIN_EPIC_MISSIONS = 3                      # an epic is a line of inquiry, not one mission
MIN_EXISTING = 2                           # of them already on the desk; the rest may be new missions
MAX_NEW_MISSIONS = 2                       # new missions a board turn's epic may bring
MAX_PER_EPIC = 8                           # latest missions shown under each epic
MAX_LOOSE = 12                             # latest missions in no epic shown
MAX_POSTS = 6                              # latest published articles shown in full
MAX_OPEN = 2                               # possibilities each one left open
MAX_HISTORY = 6                            # past versions of the line shown
MAX_MEMBERS = 4                            # missions one combine folds together
MAX_LINKS = 3                              # links a turn
MAX_WHY = 300                              # characters of a link's or a combine's reason
LOOKUPS = 4                                # library searches the Theorist may run itself, after code's first
LOOKUP_TIME = 4 * 60                       # seconds for the whole lookup phase
FINDINGS_CHARS = 14_000                    # what it found, as it goes with the request (about 4.5k tokens)
FINDINGS_MAX = 24_000                      # the most it may fill when Top N is set high (about 7k tokens)
LIBRARY, SEARCH = "library", "search_library"   # the MCP server in mcp.json and its tool
CONFIG = Path(__file__).resolve().parent.parent / "mcp.json"

STAGES = {State.QUEUED: "queued", State.RESEARCHING: "researching", State.BRIEF_READY: "brief in review",
          State.AWAITING_COMMENTS: "writing next", State.WRITING: "writing", State.ARTICLE_READY: "article in review",
          State.DONE: "published", State.PARKED: "parked", State.COMBINED: "combined"}

SYSTEM = """You are the Theorist of an independent investigative desk: its mastermind. You do not research or
write stories. You step back, look across everything the desk has done and is doing, and see what nobody
working on a single mission can see.
{today}
Below is the whole desk: its epics (long-running lines of inquiry) with their missions, the missions in no
epic, its latest published articles with the possibilities they left open, the links already drawn between
missions, and the editor's inspiration line, which the mission finder searches around for new missions.

Look for two things:
- A pattern inside one epic: missions that, read together, point at something none of them says alone (the
  same actor, mechanism, number, date, money trail or silence turning up again).
- A theme emerging across epics: missions from different epics that share a thread no epic names yet.
A pattern is a lead, not a finding. Say what it rests on and what would test it. Then connect the dots you
found: link missions that bear on each other, combine missions that are really one question, and propose a new
mission only for the question nobody is asking yet.

Reply with JSON matching the schema:
- patterns: 1 to {patterns} short notes. Each names the epic or epics and the mission slugs it draws on.
- inspiration: the editor's line, evolved by what you saw.
  - Evolve it, never replace it. Keep the editor's own words and focus: at least half of the words of the
    line as it is now must stay. Sharpen, extend or redirect only the part a pattern calls for. If nothing new
    emerged, return it unchanged.
  - Questions only: 1 to {questions} questions, each ending with a question mark. No statements, instructions,
    answers or claims.
  - At most {max_line} characters.
- why: one or two sentences: what you changed in the line and which pattern led there, or why you kept it.
- new_mission: at most one, or null. The single question that best tests a pattern you named. It must connect
  missions, not repeat one. topic: one specific line, as a question. objective: two or three sentences naming
  the missions and the pattern it draws on and what evidence would support or sink it. epic: the title of an
  existing epic, or "". You don't make new epics this turn.
- combine: usually null. Only when 2 to {members} open missions are really one investigation, so researching
  them apart would duplicate the work or miss the connection. Only missions that are queued, researching, brief
  in review or parked can be combined; never published ones or ones being written. missions: their slugs.
  topic: one specific line, as a question, for the bigger mission. objective: two or three sentences: what each
  mission brings and what evidence would answer the combined question. epic: the title of an existing epic, or
  "". why: one sentence. The editor approves it; the missions then stop and the
  combined mission is researched in their place.
- links: usually empty, at most {links}. Connect missions that should stay separate but bear on each other, so
  each one's research sees the other. mission: an open mission's slug (not published). to: the slug of another
  open mission, or of a published article it builds on, answers or tests. why: one sentence naming the shared
  thread (actor, mechanism, number, date, money trail). Never a pair already linked.

The desk's library holds the work behind each one-line summary below: every brief's claims with the exact quotes
they cite, counterpoints, sources and article sections. What you found there comes with the request. A pattern may
rest on a passage: name its mission slug. The passages are the desk's own past work, not new evidence.

Do not propose anything whose premise accuses a private person of wrongdoing or blames people for their race,
religion or ethnicity.

THE DESK

The editor's inspiration line now:
{line}

How the line got here, oldest first:
{history}

{desk}"""

class TheoryFailed(Exception):
    pass

class EpicMission(BaseModel):
    topic: str
    objective: str

class NewEpic(BaseModel):
    title: str
    why: str = ""
    missions: list[str] = []               # slugs of existing missions that belong in it
    new_missions: list[EpicMission] = []   # a board turn's new missions for it (the chat brings none)

class NewMission(BaseModel):
    topic: str
    objective: str
    epic: str = ""                         # an existing epic's title, or ""

class NewCombine(BaseModel):
    missions: list[str]                    # 2+ slugs of open missions not yet being written
    topic: str
    objective: str
    epic: str = ""                         # an existing epic's title, or ""
    why: str = ""

class NewLink(BaseModel):
    mission: str                           # an open mission's slug
    to: str                                # another open mission, or a published article
    why: str = ""

class TheoryReply(BaseModel):
    """What the model writes."""
    patterns: list[str]
    inspiration: str
    why: str = ""
    new_mission: NewMission | None = None
    combine: NewCombine | None = None
    links: list[NewLink] = []

class Theory(TheoryReply):
    """The reply after code has checked it, with what code dropped and the line it was based on."""
    based_on: str = ""                     # the line as the Theorist read it; a human edit meanwhile wins
    dropped: list[str] = []
    lookups: list[str] = []                # what it searched the library for, code's first search included
    library: str = ""                      # why it answered without (part of) the library, if it did

def _clean(text: str, limit: int = MAX_WHY) -> str:
    return " ".join(text.split())[:limit]

# ---------- what the Theorist reads ----------

def _row(m: Mission) -> str:
    when = f" {m.published_at:%Y-%m-%d}" if m.state == State.DONE and m.published_at else ""
    made = f" | combines {', '.join(m.combines)}" if m.combines else ""
    return f"- {m.slug} | {STAGES[m.state]}{when} | {m.topic}{made}"

def connections_text(missions: list[Mission]) -> str:
    """The links already drawn, the combines waiting for the editor, and what the editor turned down."""
    topics = {m.slug: m.topic for m in missions}
    links = [k for k in store.all_links(None) if k.a in topics and k.b in topics]
    rows = [f"- {k.a} <-> {k.b}: {k.why}" for k in links if k.status == "active"]
    parts = ["Links between missions:\n" + ("\n".join(rows) or "- none yet")]
    waiting = [f"- {' + '.join(c.members)} -> {c.topic}" for c in store.all_combines("proposed")]
    if waiting:
        parts.append("Combines waiting for the editor (don't propose them again):\n" + "\n".join(waiting))
    turned = [f"- {k.a} <-> {k.b} (link removed)" for k in links if k.status == "removed"]
    turned += [f"- {' + '.join(c.members)} (combine rejected)" for c in store.all_combines("rejected")]
    if turned:
        parts.append("The editor turned these down; never propose them again:\n" + "\n".join(turned))
    return "\n\n".join(parts)

def desk_text(missions: list[Mission]) -> str:
    """Every epic with its latest missions, the missions in no epic, the latest published articles, and the
    connections drawn so far. Missions already combined into another are left out: the bigger one stands in."""
    every, missions = missions, [m for m in missions if m.state != State.COMBINED]
    parts = []
    for e in store.all_epics():
        mine = [m for m in missions if m.epic == e.slug]
        status = " (proposed, waiting for the editor)" if e.status == "proposed" else ""
        rows = [_row(m) for m in mine[-MAX_PER_EPIC:]] or ["- no missions yet"]
        more = [f"- and {len(mine) - MAX_PER_EPIC} earlier missions"] if len(mine) > MAX_PER_EPIC else []
        parts.append(f"Epic: {e.title}{status}, {len(mine)} missions\n" + "\n".join(more + rows))
    if not parts:
        parts.append("Epics: none yet")
    loose = [m for m in missions if not m.epic]
    if loose:
        parts.append("Missions in no epic:\n" + "\n".join(_row(m) for m in loose[-MAX_LOOSE:]))
    posts = sorted((m for m in missions if m.state == State.DONE and m.published_at),
                   key=lambda m: m.published_at, reverse=True)
    lines, shown = [], 0
    for m in posts:
        b, a = store.load_artifact(m.slug, "brief"), store.load_artifact(m.slug, "article")
        if not b or not a:
            continue
        brief, article = ResearchBrief(**b), Article(**a)
        lines.append(f'- {m.slug} ({m.published_at:%Y-%m-%d}): "{article.title}". {article.dek}')
        lines += [f"    Left open: {c.text}" for c in brief.claims if c.kind == "possibility"][:MAX_OPEN]
        shown += 1
        if shown == MAX_POSTS:
            break
    parts.append("Latest published articles, newest first:\n" + ("\n".join(lines) or "- none yet"))
    parts.append(connections_text(every))
    return "\n\n".join(parts)

def history_text(history: list[dict]) -> str:
    rows = []
    for h in history[-MAX_HISTORY:]:
        who = {"human": "the editor", "chat": "the chat assistant, for the editor"}.get(h.get("by"), "you, the Theorist")
        why = f" (why: {h['why']})" if h.get("why") else ""
        rows.append(f'- {h.get("at", "")[:10]}, {who}: "{h.get("line", "")}"{why}')
    return "\n".join(rows) or "- the editor wrote it; it has not changed since"

def prompt(line: str, history: list[dict], missions: list[Mission], now=None) -> str:
    today = f"Today's date: {now:%Y-%m-%d}.\n" if now else ""
    return SYSTEM.format(today=today, patterns=MAX_PATTERNS, questions=MAX_QUESTIONS, max_line=MAX_LINE,
                         members=MAX_MEMBERS, links=MAX_LINKS,
                         line=line, history=history_text(history), desk=desk_text(missions))

# ---------- what code holds it to ----------

def line_problems(new: str, current: str) -> list[str]:
    """Why the evolved line can't replace the current one, or [] if it can."""
    line = " ".join(new.split())
    if not line:
        return ["inspiration is empty: return the line, evolved or unchanged"]
    problems = []
    if len(line) > MAX_LINE:
        problems.append(f"inspiration is {len(line)} characters; at most {MAX_LINE}")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", line) if s]
    if not all(s.endswith("?") for s in sentences):
        problems.append("inspiration may only ask questions: every sentence must end with a question mark")
    elif len(sentences) > MAX_QUESTIONS:
        problems.append(f"inspiration asks {len(sentences)} questions; at most {MAX_QUESTIONS}")
    mine = words(current)
    kept = mine & words(line)
    if mine and len(kept) / len(mine) < KEEP:
        missing = ", ".join(sorted(mine - kept)[:8])
        problems.append(f"inspiration rewrites the editor's line instead of evolving it: keep at least half of "
                        f"its words (missing: {missing})")
    return problems

def settle(reply: TheoryReply, line: str, missions: list[Mission]) -> Theory:
    """Keep only what the Theorist may do: a line that passes line_problems (else the line stays) and one new
    mission the desk doesn't already have, in an active epic or none. New epics are the board turn's (#131)."""
    dropped: list[str] = []
    new_line = " ".join(reply.inspiration.split())
    problems = line_problems(new_line, line)
    if problems:
        dropped.append("line kept as it was: " + "; ".join(problems))
        new_line = line
    patterns = [" ".join(p.split())[:400] for p in reply.patterns if p.strip()][:MAX_PATTERNS]

    mission = reply.new_mission
    if mission and (not mission.topic.strip() or not mission.objective.strip()):
        dropped.append("new mission dropped: it needs a topic and an objective")
        mission = None
    if mission and too_similar(mission.topic, [m.topic for m in missions]):
        dropped.append(f"new mission dropped: the desk already has one like it ({mission.topic.strip()})")
        mission = None

    if mission:
        home = epic_title(mission.epic)
        found = store.find_epic(home) if home else None
        home = found.title if found and found.status == "active" else ""
        mission = NewMission(topic=" ".join(mission.topic.split()), objective=mission.objective.strip(), epic=home)
    combine = settle_combine(reply.combine, missions, dropped)
    links = settle_links(reply.links, missions, combine, dropped)
    return Theory(patterns=patterns, inspiration=new_line, why=" ".join(reply.why.split()),
                  new_mission=mission, combine=combine, links=links, based_on=line, dropped=dropped)

def settle_epic(e: NewEpic, missions: list[Mission], dropped: list[str], new: int = 0) -> NewEpic | None:
    """A new epic with a 1 to 4 word title no epic has, holding at least MIN_EXISTING known missions and
    MIN_EPIC_MISSIONS in all (new: how many new missions come with it, a board turn's). Used for the Theorist's
    board turn and the chat assistant's proposals, which bring no new missions, so need MIN_EPIC_MISSIONS known
    ones."""
    title = epic_title(e.title)
    if not title:
        dropped.append(f"new epic dropped: {e.title.strip()!r} is not a 1 to 4 word title")
        return None
    if store.find_epic(title):
        dropped.append(f"new epic dropped: {title} already exists")
        return None
    known = {m.slug for m in missions}
    members = [s for s in dict.fromkeys(s.strip() for s in e.missions) if s in known]
    if len(members) < MIN_EXISTING or len(members) + new < MIN_EPIC_MISSIONS:
        dropped.append(f"new epic dropped: {title} needs at least {MIN_EPIC_MISSIONS} missions, "
                       f"{MIN_EXISTING} of them already on the desk")
        return None
    return NewEpic(title=title, why=" ".join(e.why.split()), missions=members)

def _epic_for(name: str, members: list[Mission]) -> str:
    """The combined mission's epic: an active epic, else the one its members share."""
    title = epic_title(name)
    found = store.find_epic(title) if title else None
    if found and found.status == "active":
        return found.title
    shared = {m.epic for m in members}
    epic = store.get_epic(shared.pop()) if len(shared) == 1 and "" not in shared else None
    return epic.title if epic and epic.status == "active" else ""

def settle_combine(c: NewCombine | None, missions: list[Mission], dropped: list[str]) -> NewCombine | None:
    """A combine of 2 to MAX_MEMBERS missions that aren't being written yet, with a topic and objective, new to
    the desk (apart from the missions it folds together), and not one the editor has seen already."""
    if not c:
        return None
    by_slug = {m.slug: m for m in missions}
    slugs = list(dict.fromkeys(s.strip() for s in c.missions if s.strip()))
    unknown = [s for s in slugs if s not in by_slug]
    late = [s for s in slugs if s in by_slug and by_slug[s].state not in COMBINABLE]
    members = [by_slug[s] for s in slugs if s in by_slug and by_slug[s].state in COMBINABLE]
    why = None
    if unknown:
        why = f"no such mission: {', '.join(unknown)}"
    elif late:
        why = f"only missions not yet being written can be combined ({', '.join(late)})"
    elif not 2 <= len(members) <= MAX_MEMBERS:
        why = f"a combine takes 2 to {MAX_MEMBERS} missions"
    elif not c.topic.strip() or not c.objective.strip():
        why = "it needs a topic and an objective"
    elif too_similar(c.topic, [m.topic for m in missions if m.slug not in slugs]):
        why = f"the desk already has a mission like it ({c.topic.strip()})"
    else:
        seen = [set(k.members) for k in store.all_combines() if k.status in ("proposed", "rejected")]
        if set(slugs) in seen:
            why = "the editor has seen this combine already"
        elif any(set(slugs) & set(k.members) for k in store.all_combines("proposed")):
            why = "one of its missions is already waiting in a proposed combine"
    if why:
        dropped.append(f"combine dropped: {why}")
        return None
    return NewCombine(missions=slugs, topic=_clean(c.topic, 400), objective=c.objective.strip(),
                      epic=_epic_for(c.epic, members), why=_clean(c.why))

def settle_links(links: list[NewLink], missions: list[Mission], combine: NewCombine | None,
                 dropped: list[str]) -> list[NewLink]:
    """Up to MAX_LINKS new links, each from an open mission to another open mission or a published article,
    with a reason, between missions not already linked (or unlinked by the editor), and not both inside this
    turn's combine (it joins them anyway). The open mission comes first."""
    by_slug = {m.slug: m for m in missions}
    kept: list[NewLink] = []
    pairs: set[frozenset] = set()
    folded = set(combine.missions) if combine else set()
    for k in links:
        a, b = by_slug.get(k.mission.strip()), by_slug.get(k.to.strip())
        name = f"{k.mission.strip()} <-> {k.to.strip()}"
        old = store.get_link(a.slug, b.slug) if a and b else None
        why = None
        if not a or not b:
            why = "no such mission"
        elif a.slug == b.slug:
            why = "a mission can't link to itself"
        elif State.COMBINED in (a.state, b.state):
            why = "one of them was combined into another mission"
        elif not is_open(a) and not is_open(b):
            why = "a link needs at least one open mission"
        elif not k.why.strip():
            why = "it needs a reason"
        elif {a.slug, b.slug} <= folded:
            why = "the combine joins them already"
        elif old and old.status == "removed":
            why = "the editor removed this link"
        elif old or frozenset((a.slug, b.slug)) in pairs:
            why = "they are linked already"
        elif len(kept) == MAX_LINKS:
            why = f"at most {MAX_LINKS} links a turn"
        if why:
            dropped.append(f"link dropped: {name}, {why}")
            continue
        if not is_open(a):                 # the open mission first
            a, b = b, a
        pairs.add(frozenset((a.slug, b.slug)))
        kept.append(NewLink(mission=a.slug, to=b.slug, why=_clean(k.why)))
    return kept

# ---------- the library: what it looks up before it answers ----------

LOOK = """Before you give your theory, look inside the desk's library. {tool} searches every mission's brief
(claims with their quotes, counterpoint, sources), article sections and objective, by meaning and words. Here is
what it returns for the inspiration line itself:

{first}

Search it up to {n} more times for what you need: the actor, mechanism, number, date or money trail a pattern you
suspect rests on, or whether two missions really share one. Optional filters: epic (a title), mission (a slug).
When you have what you need, reply with the single word done."""

FINDINGS = """What you found in the desk's library (the desk's own past work, not new evidence):

{found}

Give your theory as JSON matching the schema."""

class _Stopped(Exception):
    """Stop was pressed while a library search was running past the turn grace."""

async def _search(tb, args: dict) -> str:
    """One library search that gives up once stop's turn grace runs out, as llm.chat does."""
    task = asyncio.ensure_future(tb.call(SEARCH, args))
    try:
        while True:
            left = stopping.left(stopping.TURN_GRACE)
            if left is not None and left <= 0:
                raise _Stopped()
            await asyncio.wait({task}, timeout=1 if left is None else min(1, left))
            if task.done():
                return task.result()
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass

async def look_up(system: str, line: str, toolbox_factory) -> tuple[list[tuple[str, str]], str]:
    """The lookup phase: code searches the library for the line, then the model may search LOOKUPS more times, in
    LOOKUP_TIME. Returns ([(query, result)], note); the note says why it stopped short ("" when it didn't). Never
    raises: a library that won't start or a model that gets stuck leaves what was found so far."""
    found: list[tuple[str, str]] = []
    if stopping.requested():
        return found, "library not searched: stop was pressed"
    try:
        async with asyncio.timeout(LOOKUP_TIME), toolbox_factory() as tb:
            tools = [t for t in tb.tools if t["function"]["name"] == SEARCH]
            first = await _search(tb, {"query": line})
            trail.record("tool_result", f"{SEARCH}: {first}")
            found.append((line, first))
            messages = [{"role": "system", "content": system},
                        {"role": "user", "content": LOOK.format(tool=SEARCH, first=first, n=LOOKUPS)}]
            calls = 0
            while calls < LOOKUPS and tools and not stopping.requested():
                try:
                    msg = await llm.chat(messages, tools=tools)
                except (llm.Cut, llm.Overrun):     # stopped or stuck: answer from what it found
                    break
                if not msg.tool_calls or stopping.requested():
                    break
                messages.append(msg)
                for tc in msg.tool_calls:          # every call gets an answer, or some chat templates break
                    args = {k: str(v) for k, v in dict(tc.function.arguments or {}).items()
                            if k in ("query", "epic", "mission")}
                    if tc.function.name != SEARCH or calls >= LOOKUPS or not args.get("query", "").strip():
                        messages.append({"role": "tool", "tool_name": tc.function.name,
                                         "content": f"Not run: only {SEARCH} with a query, at most {LOOKUPS} times."})
                        continue
                    calls += 1
                    result = await _search(tb, args)
                    trail.record("tool_result", f"{SEARCH}: {result}")
                    found.append((args["query"], result))
                    messages.append({"role": "tool", "tool_name": SEARCH, "content": result})
    except TimeoutError:
        return found, f"library: the lookups ran out of time after {len(found)} search(es)"
    except _Stopped:
        return found, f"library: stop was pressed after {len(found)} search(es)"
    except Exception as e:                  # the library server didn't start, or a call broke: carry on without it
        return found, f"library not searched: {type(e).__name__}: {e}"[:300]
    return found, ""

def findings_room() -> int:
    """What the findings may fill: FINDINGS_CHARS, or two searches at the Settings page's Top N if that is more,
    never over FINDINGS_MAX (the request still has to fit a local model's context)."""
    return min(FINDINGS_MAX, max(FINDINGS_CHARS, 2 * settings.library_top_n() * library.PASSAGE_ROOM))

def findings_text(found: list[tuple[str, str]]) -> str:
    """Each search and what it returned, shared out within findings_room()."""
    if not found:
        return ""
    each = findings_room() // len(found)
    parts = []
    for query, result in found:
        text = result if len(result) <= each else result[:each].rsplit("\n", 1)[0] + "\n[more passages cut]"
        parts.append(f'Search: "{query}"\n{text}')
    return "\n\n".join(parts)

def library_toolbox():
    return Toolbox([LIBRARY], config_path=CONFIG)

# ---------- the call ----------

async def run_theorist(line: str, history: list[dict], missions: list[Mission], now=None,
                       toolbox_factory=None) -> Theory:
    """The Theorist's turn. toolbox_factory opens the library (theorize passes library_toolbox); None answers from
    the desk alone, with no lookups."""
    system = prompt(line, history, missions, now)
    found, note = await look_up(system, line, toolbox_factory) if toolbox_factory else ([], "")
    findings = findings_text(found)
    request = FINDINGS.format(found=findings) if findings else "Give your theory as JSON matching the schema."
    messages = [{"role": "system", "content": system}, {"role": "user", "content": request}]
    schema = TheoryReply.model_json_schema()
    reply, error = None, None
    for attempt in range(2):               # one repair round: malformed JSON, or a line it may not write
        try:
            msg = await llm.chat(messages, format=schema)
        except llm.Cut as cut:             # stopped: keep whatever it wrote
            reply = stopping.salvage(cut.text, TheoryReply)
            break
        except llm.Overrun as e:           # stuck: keep what it wrote before, if that holds a theory
            reply = stopping.salvage(e.text, TheoryReply)
            if reply is None:
                raise TheoryFailed(f"the theorist got stuck: {e}")
            break
        try:
            reply = TheoryReply.model_validate_json(msg.content)
        except ValidationError as e:
            error = e
            messages += [{"role": "assistant", "content": msg.content}, {"role": "user", "content": llm.invalid(e)}]
            continue
        problems = line_problems(reply.inspiration, line)
        if not problems or attempt or stopping.requested():
            break
        messages += [{"role": "assistant", "content": msg.content},
                     {"role": "user", "content": "The inspiration line can't be used:\n- " + "\n- ".join(problems)
                      + "\nReturn the whole JSON again with the line fixed."}]
    if reply is None:
        raise TheoryFailed(f"model could not produce a valid theory: {error}")
    theory = settle(reply, line, missions)
    theory.lookups, theory.library = [q for q, _ in found], note
    return theory

def theorize(now=None) -> Theory:
    """Sync entry point the orchestrator calls. Reads the desk (and its library) from the store; changes nothing."""
    line = store.get_inspiration()
    if not line:
        raise TheoryFailed("no inspiration line to evolve")
    return asyncio.run(run_theorist(line, store.inspiration_history(), store.all_missions(), now,
                                    toolbox_factory=library_toolbox))


# ---------- board turns: every BOARD_EVERY-th turn arranges the cork board (#69) and may propose an epic (#131) ----------

MAX_MOVES = 12                             # notes a board turn may move or resize
BOARD_MARGIN = 800                         # px a note may go past the board's current edges
GRID_COLS, GRID_ROWS = 60, 22              # the rough picture of the board the Theorist sees

BOARD_SYSTEM = """You are the Theorist of an investigative research desk. This turn you arrange the desk's cork board
so a human reading it sees how the work hangs together, and you may propose one new epic. You change nothing
else: no links, combines or inspiration.

The board is a 2D map in pixels; x grows to the right, y grows downwards. Each note is a mission or an epic. A
note at size 1.0 is about 160 px wide and 110 px tall; sizes run from {size_min} to {size_max}. Strings join an
epic to its missions, linked missions, and missions that cite the same page or name each other.

Make the board read well:
- Keep each epic's missions round it, the closest work nearest; put epics whose missions are strung together
  near each other, and keep unrelated epics apart.
- Make a note bigger when it matters more: published or nearly published, strongly connected, or central to a
  pattern you see; smaller when it is a side question.
- Leave room between notes (at least {gap} px between centres) so nothing hides another, and keep crossings of
  strings few.
- Move only what helps; leave a note alone when it already sits well. At most {max_moves} notes.

A new epic (a long-running line of inquiry) is rare. Propose one only if it makes sense: the board shows a strand
of missions that belong together and no epic names yet. Keep the desk to few epics; a strand that fits an existing
epic is not a new one. {epics}
- new_epic: usually null. title: 1 to 3 words, specific yet thematic, a line of inquiry that can grow, not a goal
  (e.g. "Fed Policy Capture"), and not the name of an existing epic. why: one sentence. missions: the slugs of at
  least {min_existing} missions already on the board that belong in it (they move there once the editor approves
  it; a slug is a note id without "m:"). new_missions: up to {max_new} new questions that fill out the strand, each
  with a topic (one specific line, as a question, not repeating a mission on the board) and an objective (two or
  three sentences: which missions it draws on and what evidence would answer it). At least {min_epic} missions in
  all, counting the new ones.

Reply with "moves": each {{"id": a note id from the list, "x", "y", "size"}} (size may be null to keep it),
"why": one or two sentences on what you arranged and why, and "new_epic"."""

class NoteMove(BaseModel):
    id: str                                # a note id: "m:<slug>" or "e:<slug>"
    x: float
    y: float
    size: float | None = None

class BoardReply(BaseModel):
    """What the model writes on a board turn."""
    moves: list[NoteMove] = []
    why: str = ""
    new_epic: NewEpic | None = None

class BoardTidy(BoardReply):
    """A board turn after code has checked it."""
    dropped: list[str] = []

def board_picture(notes: list[dict]) -> str:
    """The board as a rough character grid, no style: E an epic, a letter per mission stage, + two in one cell."""
    if not notes:
        return "(the board is empty)"
    xs, ys = [n["x"] for n in notes], [n["y"] for n in notes]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    grid = [[" "] * GRID_COLS for _ in range(GRID_ROWS)]
    for n in notes:
        c = round((n["x"] - x0) / max(1, x1 - x0) * (GRID_COLS - 1))
        r = round((n["y"] - y0) / max(1, y1 - y0) * (GRID_ROWS - 1))
        mark = "E" if n["kind"] != "mission" else n.get("mark", "o")
        grid[r][c] = mark if grid[r][c] == " " else "+"
    rows = ["|" + "".join(row) + "|" for row in grid]
    edge = "+" + "-" * GRID_COLS + "+"
    return "\n".join([f"x {x0:.0f} to {x1:.0f}, y {y0:.0f} to {y1:.0f}", edge, *rows, edge,
                      "E epic or Unsorted, q queued, r researching, b brief, w writing, a article in review, "
                      "p published, x parked, c combined, + two notes in one cell"])

MARKS = {State.QUEUED: "q", State.RESEARCHING: "r", State.BRIEF_READY: "b", State.AWAITING_COMMENTS: "w",
         State.WRITING: "w", State.ARTICLE_READY: "a", State.DONE: "p", State.PARKED: "x", State.COMBINED: "c"}

def epics_note(active: int, waiting: list[str]) -> str:
    """How many epics the desk has, for the board prompt, and whether one already waits for the editor."""
    note = f"The desk has {active} active epic{'s' * (active != 1)}."
    if waiting:
        note += (f" A proposed epic is waiting for the editor ({', '.join(waiting)}), so propose none this turn: "
                 "new_epic must be null.")
    return note

def board_desk() -> dict:
    """board.layout() with what the Theorist needs to read each note (its title and stage), and the epics: how
    many are active and which proposed ones wait for the editor."""
    data = board.layout()
    data["epics"] = {"active": len(store.all_epics("active")), "waiting": [e.title for e in store.all_epics("proposed")]}
    missions = {m.slug: m for m in store.all_missions()}
    epics = {e.slug: e for e in store.all_epics()}
    for n in data["notes"]:
        if n["kind"] == "mission":
            m = missions[n["slug"]]
            n.update(title=m.topic, stage=STAGES[m.state], mark=MARKS[m.state])
        else:
            e = epics.get(n["slug"])
            n.update(title=e.title if e else "Unsorted (missions in no epic)", stage="epic" if e else "label")
    return data

def board_text(data: dict) -> str:
    """The board for the prompt: every note with its place and size, every string, and the rough picture."""
    rows = [f"- {n['id']} | {n['stage']} | {n['title']} | x={n['x']:.0f} y={n['y']:.0f} size={n['size']:.2f}"
            for n in data["notes"]]
    strings = [f"- {s['a']} -- {s['b']} ({s['kind']}{': ' + s['why'] if s.get('why') else ''})" for s in data["strings"]]
    return ("Notes:\n" + "\n".join(rows) + "\n\nStrings:\n" + ("\n".join(strings) or "- none")
            + "\n\nThe board now:\n" + board_picture(data["notes"]))

def settle_board_epic(e: NewEpic | None, missions: list[Mission], dropped: list[str]) -> NewEpic | None:
    """A board turn's new epic (#131): none while another proposed epic waits for the editor; up to MAX_NEW_MISSIONS
    new missions, each with a topic and objective and not like any on the desk (or each other); then settle_epic:
    at least MIN_EXISTING missions on the desk and MIN_EPIC_MISSIONS in all. The new missions go with the epic."""
    if not e:
        return None
    waiting = [x.title for x in store.all_epics("proposed")]
    if waiting:
        dropped.append(f"new epic dropped: {epic_title(e.title) or e.title.strip()}, "
                       f"another proposed epic is waiting for the editor ({', '.join(waiting)})")
        return None
    topics, new = [m.topic for m in missions], []
    for idea in e.new_missions:
        topic = " ".join(idea.topic.split())
        if not topic or not idea.objective.strip():
            dropped.append("new mission dropped: it needs a topic and an objective")
        elif too_similar(topic, topics):
            dropped.append(f"new mission dropped: the desk already has one like it ({topic})")
        elif len(new) == MAX_NEW_MISSIONS:
            dropped.append(f"new mission dropped: {topic}, at most {MAX_NEW_MISSIONS} with a new epic")
        else:
            new.append(EpicMission(topic=topic, objective=idea.objective.strip()))
            topics.append(topic)
    kept = settle_epic(e, missions, dropped, new=len(new))
    if kept:
        kept.new_missions = new
    return kept

def settle_board(reply: BoardReply, data: dict, missions: list[Mission] | None = None) -> BoardTidy:
    """Code holds a board turn to the board: known notes only, each once, at most MAX_MOVES, sizes in range,
    positions near the board, and no note dropped on top of another (it is nudged to the nearest free spot). Its
    new epic is held to settle_board_epic; dropping it never drops the moves."""
    notes = {n["id"]: n for n in data["notes"]}
    if not notes:
        return BoardTidy(why=_clean(reply.why), dropped=[f"{len(reply.moves)} move(s): the board is empty"] if reply.moves else [])
    epic_dropped: list[str] = []
    epic = settle_board_epic(reply.new_epic, store.all_missions() if missions is None else missions, epic_dropped)
    xs, ys = [n["x"] for n in notes.values()], [n["y"] for n in notes.values()]
    box = (min(xs) - BOARD_MARGIN, max(xs) + BOARD_MARGIN, min(ys) - BOARD_MARGIN, max(ys) + BOARD_MARGIN)
    where = {i: (n["x"], n["y"]) for i, n in notes.items()}
    kept, dropped = [], []
    for mv in reply.moves:
        if mv.id not in notes:
            dropped.append(f"{mv.id}: no such note")
        elif any(k.id == mv.id for k in kept):
            dropped.append(f"{mv.id}: moved twice")
        elif len(kept) >= MAX_MOVES:
            dropped.append(f"{mv.id}: at most {MAX_MOVES} moves a turn")
        elif not all(map(math.isfinite, (mv.x, mv.y))) or (mv.size is not None and not math.isfinite(mv.size)):
            dropped.append(f"{mv.id}: not a number")
        else:
            x, y = min(box[1], max(box[0], mv.x)), min(box[3], max(box[2], mv.y))
            others = [xy for i, xy in where.items() if i != mv.id]
            if not board._far(x, y, others, board.MIN_GAP):
                x, y = board._spot(x, y, board.MIN_GAP / 3, others, board.MIN_GAP)
            size = None
            if mv.size is not None and notes[mv.id]["kind"] != "unsorted":
                size = round(min(board.SIZE_MAX, max(board.SIZE_MIN, mv.size)), 2)
            where[mv.id] = (round(x, 1), round(y, 1))
            kept.append(NoteMove(id=mv.id, x=round(x, 1), y=round(y, 1), size=size))
    return BoardTidy(moves=kept, why=_clean(reply.why), new_epic=epic, dropped=dropped + epic_dropped)

async def run_board(data: dict, now=None) -> BoardTidy:
    today = f"Today's date: {now:%Y-%m-%d}.\n" if now else ""
    counts = data.get("epics", {})
    system = today + BOARD_SYSTEM.format(size_min=board.SIZE_MIN, size_max=board.SIZE_MAX, gap=board.MIN_GAP,
                                         max_moves=MAX_MOVES, min_existing=MIN_EXISTING, max_new=MAX_NEW_MISSIONS,
                                         min_epic=MIN_EPIC_MISSIONS,
                                         epics=epics_note(counts.get("active", 0), counts.get("waiting", [])))
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": board_text(data) + "\n\nArrange the board. Reply as JSON matching the schema."}]
    reply = await llm.ask(messages, BoardReply, "theorist", TheoryFailed)
    return settle_board(reply, data)

def tidy_board(now=None) -> BoardTidy:
    """Sync entry point the orchestrator calls on a board turn. Reads the board; changes nothing (apply_board does)."""
    return asyncio.run(run_board(board_desk(), now))
