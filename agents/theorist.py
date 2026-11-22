"""Theorist: the desk's mastermind, at the very start of the flow. One structured call on the Principal
Investigator's model, no tools: it reads the whole desk (every epic with its missions, the missions in no epic,
the latest published articles and the possibilities they left open, and how the inspiration line got here)
and looks for patterns inside an epic and themes emerging across epics.

It may evolve the inspiration line, propose up to MAX_EPICS new epics and propose at most one new mission.
Code holds it to that: the line may only ask questions and must keep at least KEEP of the current line's
words (an evolution, not a rewrite), a new epic needs MIN_EPIC_MISSIONS missions, and a new mission must not
repeat one the desk has. The orchestrator applies what is left (orchestrator.apply_theory)."""
import asyncio, re
from pydantic import BaseModel, ValidationError
import store
from contracts import Mission, State, ResearchBrief, Article, epic_title, words
from agents import llm
from agents.finder import too_similar
import stopping

KEEP = 0.5                                 # share of the current line's meaningful words the new line keeps
MAX_LINE = 500                             # characters, as in the UI's box
MAX_QUESTIONS = 3
MAX_PATTERNS = 4
MAX_EPICS = 2
MIN_EPIC_MISSIONS = 2                      # an epic is a line of inquiry, not one mission
MAX_PER_EPIC = 8                           # latest missions shown under each epic
MAX_LOOSE = 12                             # latest missions in no epic shown
MAX_POSTS = 6                              # latest published articles shown in full
MAX_OPEN = 2                               # possibilities each one left open
MAX_HISTORY = 6                            # past versions of the line shown

STAGES = {State.QUEUED: "queued", State.RESEARCHING: "researching", State.BRIEF_READY: "brief in review",
          State.AWAITING_COMMENTS: "writing next", State.WRITING: "writing", State.ARTICLE_READY: "article in review",
          State.DONE: "published", State.PARKED: "parked"}

SYSTEM = """You are the Theorist of an independent investigative desk: its mastermind. You do not research or
write stories. You step back, look across everything the desk has done and is doing, and see what nobody
working on a single mission can see.
{today}
Below is the whole desk: its epics (long-running lines of inquiry) with their missions, the missions in no
epic, its latest published articles with the possibilities they left open, and the editor's inspiration line,
which the mission finder searches around for new missions.

Look for two things:
- A pattern inside one epic: missions that, read together, point at something none of them says alone (the
  same actor, mechanism, number, date, money trail or silence turning up again).
- A theme emerging across epics: missions from different epics that share a thread no epic names yet.
A pattern is a lead, not a finding. Say what it rests on and what would test it.

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
- new_epics: usually empty, at most {epics}. Only for a theme emerging across epics, or a strand inside one epic
  that has grown into a line of inquiry of its own. title: 1 to 3 words, specific yet thematic, a line of
  inquiry that can grow, not a goal (e.g. "Fed Policy Capture"), and not the name of an existing epic. why:
  one sentence. missions: the slugs of the existing missions that belong in it (they move there once the
  editor approves it). It needs at least {min_epic} missions, counting your new mission if it goes there.
- new_mission: at most one, or null. The single question that best tests a pattern you named. It must connect
  missions, not repeat one. topic: one specific line, as a question. objective: two or three sentences naming
  the missions and the pattern it draws on and what evidence would support or sink it. epic: the title of an
  existing epic, or of one of your new_epics.

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

class NewEpic(BaseModel):
    title: str
    why: str = ""
    missions: list[str] = []               # slugs of existing missions that belong in it

class NewMission(BaseModel):
    topic: str
    objective: str
    epic: str = ""                         # an existing epic's title, or one of new_epics

class TheoryReply(BaseModel):
    """What the model writes."""
    patterns: list[str]
    inspiration: str
    why: str = ""
    new_epics: list[NewEpic] = []
    new_mission: NewMission | None = None

class Theory(TheoryReply):
    """The reply after code has checked it, with what code dropped and the line it was based on."""
    based_on: str = ""                     # the line as the Theorist read it; a human edit meanwhile wins
    dropped: list[str] = []

# ---------- what the Theorist reads ----------

def _row(m: Mission) -> str:
    when = f" {m.published_at:%Y-%m-%d}" if m.state == State.DONE and m.published_at else ""
    return f"- {m.slug} | {STAGES[m.state]}{when} | {m.topic}"

def desk_text(missions: list[Mission]) -> str:
    """Every epic with its latest missions, the missions in no epic, and the latest published articles."""
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
    return "\n\n".join(parts)

def history_text(history: list[dict]) -> str:
    rows = []
    for h in history[-MAX_HISTORY:]:
        who = "the editor" if h.get("by") == "human" else "you, the Theorist"
        why = f" (why: {h['why']})" if h.get("why") else ""
        rows.append(f'- {h.get("at", "")[:10]}, {who}: "{h.get("line", "")}"{why}')
    return "\n".join(rows) or "- the editor wrote it; it has not changed since"

def prompt(line: str, history: list[dict], missions: list[Mission], now=None) -> str:
    today = f"Today's date: {now:%Y-%m-%d}.\n" if now else ""
    return SYSTEM.format(today=today, patterns=MAX_PATTERNS, questions=MAX_QUESTIONS, max_line=MAX_LINE,
                         epics=MAX_EPICS, min_epic=MIN_EPIC_MISSIONS, line=line, history=history_text(history),
                         desk=desk_text(missions))

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
    """Keep only what the Theorist may do: a line that passes line_problems (else the line stays), new epics
    with enough missions that aren't existing epics, and one new mission the desk doesn't already have."""
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

    known = {m.slug for m in missions}
    epics: list[NewEpic] = []
    for e in reply.new_epics:
        title = epic_title(e.title)
        if not title:
            dropped.append(f"new epic dropped: {e.title.strip()!r} is not a 1 to 4 word title")
            continue
        if store.find_epic(title) or title.lower() in (k.title.lower() for k in epics):
            dropped.append(f"new epic dropped: {title} already exists")
            continue
        members = [s for s in dict.fromkeys(s.strip() for s in e.missions) if s in known]
        joins = bool(mission and epic_title(mission.epic).lower() == title.lower())
        if len(members) + joins < MIN_EPIC_MISSIONS:
            dropped.append(f"new epic dropped: {title} needs at least {MIN_EPIC_MISSIONS} missions")
            continue
        if len(epics) == MAX_EPICS:
            dropped.append(f"new epic dropped: {title}, at most {MAX_EPICS} at a time")
            continue
        epics.append(NewEpic(title=title, why=" ".join(e.why.split()), missions=members))

    if mission:
        home = epic_title(mission.epic)
        ours = next((e.title for e in epics if e.title.lower() == home.lower()), "")
        found = store.find_epic(home) if home else None
        home = ours or (found.title if found and found.status == "active" else "")
        mission = NewMission(topic=" ".join(mission.topic.split()), objective=mission.objective.strip(), epic=home)
    return Theory(patterns=patterns, inspiration=new_line, why=" ".join(reply.why.split()), new_epics=epics,
                  new_mission=mission, based_on=line, dropped=dropped)

# ---------- the call ----------

async def run_theorist(line: str, history: list[dict], missions: list[Mission], now=None) -> Theory:
    messages = [{"role": "system", "content": prompt(line, history, missions, now)},
                {"role": "user", "content": "Give your theory as JSON matching the schema."}]
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
    return settle(reply, line, missions)

def theorize(now=None) -> Theory:
    """Sync entry point the orchestrator calls. Reads the desk from the store; changes nothing."""
    line = store.get_inspiration()
    if not line:
        raise TheoryFailed("no inspiration line to evolve")
    return asyncio.run(run_theorist(line, store.inspiration_history(), store.all_missions(), now))
