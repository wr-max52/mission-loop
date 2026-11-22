"""Writer agent: turns an approved brief into an Article in small, checked steps.

No tools, no browsing. The writer may only use what is in the brief (and any POC comments),
and checks/article_check.py enforces that in code. First a plan (title, dek, sections and which claims
each carries), then each section on its own. Code checks every piece as soon as it is written and sends
back only what is wrong with that piece; each finished piece is saved on the mission (Mission.article_draft),
so a stop, crash or failed check never throws away the sections that were fine.
"""
import asyncio, json
from contracts import (Mission, ResearchBrief, OutreachRecord, Article, ArticleDraft, ArticlePlan, PlannedSection,
                       Section, cite_by_id, tidy_bold, unbold)
from agents import llm
from checks import article_check
import checkpoint, stopping, trail

PLAN_ROUNDS = 2                            # the plan, then one round of fixes; code settles what is left
SECTION_ROUNDS = 3                         # a section, then up to 2 rounds of fixes

SYSTEM = """You are an investigative writer who explains hard findings in plain language.
Write a short, clear article (500 to 900 words) from the research brief below. The brief is the only
thing you know: do not add facts, numbers, names, dates or quotes that are not in it.

A reader should get the big idea in ten seconds, care about it in thirty, and find every detail to check
at the end. So the article goes in this order:
1. The claim, in a box above the article: who did what, and why it matters, in 1 or 2 plain sentences.
2. The story: the first section opens with a short scene built from the brief's facts (a moment, a
   document, a decision, a person, a number) that shows what is at stake, then states the claim plainly.
3. The evidence: what the record shows, one point at a time, strongest first.
4. The other side: the brief's counterpoint, told fairly.
5. The fine print, last: what the sources do not establish, where their evidence stops short of their
   conclusions, and the open questions.

Plain language:
- Short sentences, most under 20 words. One idea per sentence. Active voice: say who did what.
- Everyday words: "used", not "utilized"; "shows", not "is indicative of". Explain any technical term in a
  few words the first time.
- No filler: never "it is worth noting", "notably", "underscores", "raises questions about", "sheds light
  on", "complex landscape". No stacked clauses: if a sentence needs two commas to make its point, split it.
- Attribute simply: "records show", "the agency says", "according to the filing".
- Bold the words that carry a claim with **double asterisks**: the actor and the action (who did what),
  and what it cost or changed, e.g. "**The county paid** the contractor **twice** [s2]." Bold one to three
  short phrases in the claim box and in each paragraph that states a claim, no more. Never bold inside
  quotation marks, in the title, dek or headings, a citation, or the words "It is possible that".

Truth rules (code checks these, so follow them exactly):
- Treat every source as part truth, part spin: report what it shows, and never repeat its framing as
  settled. Take no partisan side; aim at the evidence, not at a party. The only speculation allowed is the
  brief's claims with kind "possibility".
- Claims with kind "possibility" are unverified, and they are the heart of the article. Include every one,
  each written once as a plain sentence that begins "It is possible that", naming the evidence it rests on,
  and cited. The page labels each one "Possibility, not verified" for you, so add no more hedging ("may
  potentially", "unverified", "not confirmed", "it remains unclear whether"): say it once, simply, and let
  the facts around it do the work. Never state or imply a possibility as fact, including in the title, dek
  and claim box. Do not use the words "it is possible that" for anything else.
- Do not suggest that a named person committed a crime or acted in bad faith unless the brief says who
  made that allegation, and then name who made it.
- End every sentence that states a fact or a possibility with an inline citation of the source id(s) it
  comes from, in square brackets, e.g. "The bill passed 52-48 [senate]." or "[s1, s3]". Every paragraph
  needs one, the opening scene's included.
- Only use source ids listed in the brief. Never write URLs or Markdown links.
- Put words in quotation marks only when they are copied exactly from a source quote below.
- One section fairly presents the brief's counterpoint. Cite the sources with stance "contradicts" if the
  brief has any; if it has none, say plainly that no source found makes this case, and cite the sources
  whose evidence it questions.
- 3 to 5 sections, each with a short, plain heading.
- title: specific and direct: who did what. If it rests on a possibility, word it as a question or say
  "possible". dek: one plain sentence on why it matters, saying so if the finding is unverified.
{comments}{notes}
RESEARCH BRIEF (JSON; its claims are numbered from 1 by "n"):
{brief}"""

PLAN = """Plan the article before writing it. Output JSON matching the schema:
- title and dek: see the rules above.
- claim: the box above the article. 1 or 2 short sentences in plain words: who did what, and why it
  matters to an ordinary reader. Bold the actor and the action with **double asterisks**. End each
  sentence with its source ids in square brackets. If the big idea rests on a possibility, the sentence
  begins "It is possible that". Use no quotation marks.
- sections: 3 to 5, in order, each with a short plain heading, its point in one sentence, the numbers ("n")
  of the brief's claims it carries, and counterpoint set to true for the one section that presents the case
  against the thesis. The first section opens with a short scene and then states the claim: give it the
  claims that make the scene and the claim. The evidence comes next. The last section is the fine print:
  what the sources do not establish and the open questions (with only 3 sections, the counterpoint section
  goes last and carries the fine print too). Put every claim with kind "possibility" in the section it fits
  best."""

SECTION = """Write section {n} of {total}: "{heading}". Its point: {point}
{role}{claims}{counterpoint}{revise}Words you may put in quotation marks, copied exactly (anything else, paraphrase without quotation marks):
{quotes}
Output JSON matching the schema: the heading and 1 to 3 paragraphs (about {words} words in all). Plain words,
short sentences, active voice. Bold the actor and the action of each claim with **double asterisks**. Every
sentence that states a fact or a possibility ends with its source ids in square brackets."""

OPENING = """This is the opening. Start with a short scene in 2 to 4 sentences: one concrete moment from the brief
(who, where, when, what happened), told as a story but built only from the brief's facts, each cited: no
invented details, feelings or dialogue. Then state the claim plainly in 1 or 2 sentences: who did what, and
why it matters.
"""
EVIDENCE = """This section is evidence: one point at a time, strongest first, with the brief's concrete details
(names, dates, amounts). Don't repeat the opening.
"""
FINE_PRINT = """This is the last section: the fine print. Say plainly what the sources do not establish, where their
evidence stops short of their conclusions, and what is still open. Keep it concrete; no summary of the article.
"""

class WritingFailed(Exception):
    pass

def _comments(records: list[OutreachRecord]) -> str:
    replies = [r for rec in records for r in rec.replies if r.get("is_comment")]
    if not replies:
        return ""
    lines = "\n".join(f"- {r.get('from', 'a contact')}: {r.get('text', '')}" for r in replies)
    return f"\nComments received from points of contact (you may quote these exactly):\n{lines}\n"

def _notes(m: Mission) -> str:
    if not m.validator_notes:
        return ""
    failed = m.rejected_article
    if failed and failed.notes == m.validator_notes:   # the check's notes point at paragraphs of this draft
        if m.article_draft:                # the sections that failed are revised one by one instead
            return ""
        return (f"\nYour previous draft failed the article check. Revise it instead of starting over: keep what "
                f"works and fix every problem listed after it.\n{failed.article.model_dump_json()}\n"
                f"Problems:\n{m.validator_notes}\n")
    return f"\nYour previous draft was rejected. Fix every problem below:\n{m.validator_notes}\n"

def prompt(m: Mission, brief: ResearchBrief, records: list[OutreachRecord]) -> str:
    data = brief.model_dump(exclude={"pocs", "question_for_pocs"})
    data["claims"] = [{"n": n, **c} for n, c in enumerate(data["claims"], 1)]
    return SYSTEM.format(comments=_comments(records), notes=_notes(m), brief=json.dumps(data, indent=1))

def _settle_plan(plan: ArticlePlan, brief: ResearchBrief) -> ArticlePlan:
    """Code's fixes for a plan still wrong after its repair round: drop claim numbers that don't exist,
    put each unplaced possibility in the first section, add a counterpoint section if none is marked, and
    leave out a claim box that still fails its check (the article then opens without one)."""
    count = len(brief.claims)
    sections = [s.model_copy(update={"claims": [n for n in s.claims if 1 <= n <= count]}) for s in plan.sections]
    used = {n for s in sections for n in s.claims}
    missing = [n for n, c in enumerate(brief.claims, 1) if c.kind == "possibility" and n not in used]
    if missing:
        sections[0] = sections[0].model_copy(update={"claims": sections[0].claims + missing})
    if not any(s.counterpoint for s in sections):
        sections.append(PlannedSection(heading="The other side", point=brief.counterpoint, counterpoint=True))
    claim = plan.claim if plan.claim.strip() and not article_check.claim_problems(plan.claim, brief) else ""
    return plan.model_copy(update={"sections": sections, "claim": claim})

def _tidy_plan(plan: ArticlePlan, brief: ResearchBrief) -> ArticlePlan:
    """What code fixes safely: no bold where it can't show (title, dek, headings), stray asterisks dropped,
    and a claim box citing a brief source by URL cites it by id."""
    return plan.model_copy(update={
        "title": unbold(plan.title).strip(), "dek": unbold(plan.dek).strip(),
        "claim": cite_by_id(tidy_bold(plan.claim), brief).strip(),
        "sections": [s.model_copy(update={"heading": unbold(s.heading).strip()}) for s in plan.sections]})

async def _plan(base: list, brief: ResearchBrief) -> ArticlePlan:
    trail.record("note", "article step 1: planning")
    messages = base + [{"role": "user", "content": PLAN}]
    for round in range(PLAN_ROUNDS):
        plan = _tidy_plan(await llm.ask(messages, ArticlePlan, "writer", WritingFailed), brief)
        problems = article_check.plan_problems(plan, brief)
        if not problems:
            return plan
        if round < PLAN_ROUNDS - 1:
            messages += [{"role": "assistant", "content": plan.model_dump_json()},
                         {"role": "user", "content": _fix(problems, "the whole plan")}]
    return _settle_plan(plan, brief)

def _fix(problems: list[str], what: str) -> str:
    return ("Code checked it and found:\n" + "\n".join(f"- {p}" for p in problems)
            + f"\nReturn {what} again as JSON, with every problem fixed and everything else kept.")

def _section_ask(brief, records, plan: ArticlePlan, i: int, previous: Section | None, notes: list[str]) -> str:
    ps = plan.sections[i]
    claims = [(n, brief.claims[n - 1]) for n in ps.claims if 1 <= n <= len(brief.claims)]
    claim_lines = "".join(f"- claim {n} ({c.kind}): {c.text} [{', '.join(c.source_ids)}]\n" for n, c in claims)
    revise = ""
    if previous is not None:
        revise = ("Your last version of this section failed the article check. Revise it, keeping what works:\n"
                  + previous.model_dump_json() + "\nProblems:\n" + "\n".join(f"- {p}" for p in notes) + "\n")
    quotes = "\n".join(f'- {who}: "{words}"' for who, words in article_check.quotable(brief, records))
    words = max(article_check.MIN_WORDS // len(plan.sections) + 40, 120)
    last = len(plan.sections) - 1
    role = OPENING if i == 0 else FINE_PRINT if i == last else EVIDENCE
    return SECTION.format(n=i + 1, total=len(plan.sections), heading=ps.heading, point=ps.point, role=role,
                          claims=f"Claims it carries:\n{claim_lines}" if claim_lines else "",
                          counterpoint=("This is the counterpoint section: present the brief's counterpoint fairly "
                                        "(see the rules above).\n" if ps.counterpoint else ""),
                          revise=revise, quotes=quotes or "(none)", words=words)

async def _section(base, brief, records, plan: ArticlePlan, done: list[Section], i: int,
                   previous: Section | None = None, notes: list[str] = ()) -> Section:
    """One section, checked as soon as it is written; up to SECTION_ROUNDS - 1 rounds of fixes, then the
    version with the fewest problems is kept and the article check has the last word."""
    ps = plan.sections[i]
    trail.record("note", f"article step {i + 2}: section {i + 1} of {len(plan.sections)}, {ps.heading}")
    so_far = "".join(f"\n## {s.heading}\n" + "\n\n".join(s.paragraphs) for s in done)
    context = f"The plan:\n{plan.model_dump_json()}\n" + (f"\nWritten so far:{so_far}\n" if so_far else "")
    messages = base + [{"role": "user", "content": context + "\n" + _section_ask(brief, records, plan, i, previous,
                                                                                     list(notes))}]
    possibilities = [n for n in ps.claims if 1 <= n <= len(brief.claims) and brief.claims[n - 1].kind == "possibility"]
    best = None
    for round in range(SECTION_ROUNDS):
        sec = await llm.ask(messages, Section, "writer", WritingFailed)
        sec = sec.model_copy(update={"heading": ps.heading,     # a URL citation becomes its id; stray ** go
                                     "paragraphs": [cite_by_id(tidy_bold(p), brief) for p in sec.paragraphs if p.strip()]})
        problems = article_check.section_problems(sec, brief, records, possibilities, ps.counterpoint)
        if best is None or len(problems) < len(best[1]):
            best = (sec, problems)
        if not problems or stopping.requested():
            break
        if round < SECTION_ROUNDS - 1:
            trail.record("note", f"section {i + 1}: {len(problems)} problem(s); asking for a fix")
            messages += [{"role": "assistant", "content": sec.model_dump_json()},
                         {"role": "user", "content": _fix(problems, "the section")}]
    return best[0]

def _to_redo(m: Mission, draft: ArticleDraft) -> dict[int, list[str]]:
    """After a failed article check: the saved sections to write again, each with its own problems. A
    problem not tied to a section (too short, the opposing view uncited) sends every section back."""
    failed = m.rejected_article          # still the draft's last verdict after a crash in between
    if not failed or [s.heading for s in failed.article.sections] != [s.heading for s in draft.sections]:
        return {}
    lines = [l for l in failed.notes.splitlines() if l.strip()]
    redo: dict[int, list[str]] = {}
    for i, sec in enumerate(draft.sections):
        tag = f"'{sec.heading}' "
        mine = [l[len(tag):] for l in lines if l.startswith(tag)]   # "paragraph 2 has no citation; ..."
        if mine:
            redo[i] = mine
    general = [l for l in lines if not any(l.startswith(f"'{s.heading}' ") for s in draft.sections)]
    if general:
        for i in range(len(draft.sections)):
            redo.setdefault(i, []).extend(general)
    # A section already rewritten since that check (before a stop) is left alone: only the failed text goes back.
    return {i: notes for i, notes in redo.items() if draft.sections[i] == failed.article.sections[i]}

async def run_writer(m: Mission, brief: ResearchBrief, records: list[OutreachRecord]) -> Article:
    base = [{"role": "system", "content": prompt(m, brief, records)}]
    draft = m.article_draft
    redo = _to_redo(m, draft) if draft else {}
    try:
        if draft is None:
            draft = m.article_draft = ArticleDraft(plan=await _plan(base, brief))
            checkpoint.save(m)
        sections = list(draft.sections)
        for i in range(len(draft.plan.sections)):
            if i < len(sections) and i not in redo:
                continue                   # written and fine: kept as it is
            if stopping.requested():       # don't start a section only for the grace to cut it off
                raise stopping.Interrupted(None)
            previous = sections[i] if i < len(sections) else None
            sec = await _section(base, brief, records, draft.plan, sections[:i], i, previous, redo.get(i, []))
            sections[i:i + 1] = [sec]
            redo.pop(i, None)
            draft = m.article_draft = draft.model_copy(update={"sections": list(sections)})
            checkpoint.save(m)             # every finished section is on disk before the next one starts
    except stopping.Interrupted:
        raise stopping.Interrupted(None)   # the plan and finished sections are on the mission; writing resumes there
    return Article(title=draft.plan.title, dek=draft.plan.dek, sections=draft.sections, claim=draft.plan.claim)

def write(m: Mission, brief: ResearchBrief, records: list[OutreachRecord]) -> Article:
    """Sync entry point the orchestrator calls."""
    return asyncio.run(run_writer(m, brief, records))
