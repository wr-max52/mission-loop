"""Theorist: the desk's mastermind, at the very start of the flow. One structured call on the Principal
Investigator's model, no tools: it reads the whole desk (every epic with its missions, the missions in no epic,
the latest published articles and the possibilities they left open, and how the inspiration line got here)
and looks for patterns inside an epic and themes emerging across epics.

It may evolve the inspiration line, propose up to MAX_EPICS new epics and propose at most one new mission. It
also connects dots: it may propose combining 2 to MAX_MEMBERS open missions into one bigger mission (at most one
combine a turn), and link up to MAX_LINKS pairs while keeping them separate: two open missions, or an open
mission and a published article. Code holds it to that: the line may only ask questions and must keep at least
KEEP of the current line's words (an evolution, not a rewrite), a new epic needs MIN_EPIC_MISSIONS missions, a
new mission must not repeat one the desk has, a combine takes only missions not yet being written, and a link
needs an open mission and a reason. The orchestrator applies what is left (orchestrator.apply_theory)."""
import asyncio, re
from pydantic import BaseModel, ValidationError
import store
from contracts import Mission, State, ResearchBrief, Article, COMBINABLE, epic_title, is_open, words
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
MAX_MEMBERS = 4                            # missions one combine folds together
MAX_LINKS = 3                              # links a turn
MAX_WHY = 300                              # characters of a link's or a combine's reason

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
- new_epics: usually empty, at most {epics}. Only for a theme emerging across epics, or a strand inside one epic
  that has grown into a line of inquiry of its own. title: 1 to 3 words, specific yet thematic, a line of
  inquiry that can grow, not a goal (e.g. "Fed Policy Capture"), and not the name of an existing epic. why:
  one sentence. missions: the slugs of the existing missions that belong in it (they move there once the
  editor approves it). It needs at least {min_epic} missions, counting your new mission if it goes there.
- new_mission: at most one, or null. The single question that best tests a pattern you named. It must connect
  missions, not repeat one. topic: one specific line, as a question. objective: two or three sentences naming
  the missions and the pattern it draws on and what evidence would support or sink it. epic: the title of an
  existing epic, or of one of your new_epics.
- combine: usually null. Only when 2 to {members} open missions are really one investigation, so researching
  them apart would duplicate the work or miss the connection. Only missions that are queued, researching, brief
  in review or parked can be combined; never published ones or ones being written. missions: their slugs.
  topic: one specific line, as a question, for the bigger mission. objective: two or three sentences: what each
  mission brings and what evidence would answer the combined question. epic: the title of an existing epic, of
  one of your new_epics, or "". why: one sentence. The editor approves it; the missions then stop and the
  combined mission is researched in their place.
- links: usually empty, at most {links}. Connect missions that should stay separate but bear on each other, so
  each one's research sees the other. mission: an open mission's slug (not published). to: the slug of another
  open mission, or of a published article it builds on, answers or tests. why: one sentence naming the shared
  thread (actor, mechanism, number, date, money trail). Never a pair already linked.

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

class NewCombine(BaseModel):
    missions: list[str]                    # 2+ slugs of open missions not yet being written
    topic: str
    objective: str
    epic: str = ""                         # an existing epic's title, one of new_epics, or ""
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
    new_epics: list[NewEpic] = []
    new_mission: NewMission | None = None
    combine: NewCombine | None = None
    links: list[NewLink] = []

class Theory(TheoryReply):
    """The reply after code has checked it, with what code dropped and the line it was based on."""
    based_on: str = ""                     # the line as the Theorist read it; a human edit meanwhile wins
    dropped: list[str] = []

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
        who = "the editor" if h.get("by") == "human" else "you, the Theorist"
        why = f" (why: {h['why']})" if h.get("why") else ""
        rows.append(f'- {h.get("at", "")[:10]}, {who}: "{h.get("line", "")}"{why}')
    return "\n".join(rows) or "- the editor wrote it; it has not changed since"

def prompt(line: str, history: list[dict], missions: list[Mission], now=None) -> str:
    today = f"Today's date: {now:%Y-%m-%d}.\n" if now else ""
    return SYSTEM.format(today=today, patterns=MAX_PATTERNS, questions=MAX_QUESTIONS, max_line=MAX_LINE,
                         epics=MAX_EPICS, min_epic=MIN_EPIC_MISSIONS, members=MAX_MEMBERS, links=MAX_LINKS,
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
    combine = settle_combine(reply.combine, missions, epics, dropped)
    links = settle_links(reply.links, missions, combine, dropped)
    return Theory(patterns=patterns, inspiration=new_line, why=" ".join(reply.why.split()), new_epics=epics,
                  new_mission=mission, combine=combine, links=links, based_on=line, dropped=dropped)

def _epic_for(name: str, new_epics: list[NewEpic], members: list[Mission]) -> str:
    """The combined mission's epic: one of this turn's new epics, an active epic, else the one its members share."""
    title = epic_title(name)
    ours = next((e.title for e in new_epics if title and e.title.lower() == title.lower()), "")
    found = store.find_epic(title) if title else None
    if ours or (found and found.status == "active"):
        return ours or found.title
    shared = {m.epic for m in members}
    epic = store.get_epic(shared.pop()) if len(shared) == 1 and "" not in shared else None
    return epic.title if epic and epic.status == "active" else ""

def settle_combine(c: NewCombine | None, missions: list[Mission], new_epics: list[NewEpic],
                   dropped: list[str]) -> NewCombine | None:
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
                      epic=_epic_for(c.epic, new_epics, members), why=_clean(c.why))

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
