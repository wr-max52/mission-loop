"""Article gate. Like the source validator, code decides; the model never grades its own work.

What code can check: every paragraph is cited, every citation points at a verified source in the
approved brief, no outside links, every direct quote is copied from a source quote or a POC comment,
the opposing view is cited, and possibilities appear only if the brief has them. Whether a citation
actually supports its sentence is the human's job at the ARTICLE_READY review.

The same checks run on each piece as the writer goes (plan_problems, section_problems), so a problem is
fed back while the piece is still small; check_article is the gate on the finished article.
"""
import re
from rapidfuzz import fuzz
from contracts import (Mission, ResearchBrief, Article, ArticlePlan, OutreachRecord, Section, cited_ids, POSSIBILITY,
                       unbold)
from checks.validator import _norm

MIN_WORDS = 300
MIN_SECTIONS, MAX_SECTIONS = 3, 5
QUOTE_MATCH = 90                           # rapidfuzz partial_ratio threshold, same as the validator
MIN_QUOTE_WORDS = 5                        # shorter quoted phrases are treated as terms, not quotations
CLAIM = "The claim at the top"             # how the checks name the box above the article
URL = re.compile(r"https?://|www\.|\]\(", re.I)
QUOTED = re.compile(r"[\"“]([^\"“”]+)[\"”]")

def quotable(brief: ResearchBrief, records: list[OutreachRecord] | None = None) -> list[tuple[str, str]]:
    """Everything an article may put in quotation marks, as (where it is from, its exact words)."""
    out = [(s.id, s.quote) for s in brief.sources]
    out += [("a contact's comment", r.get("text", "")) for rec in records or [] for r in rec.replies
            if r.get("is_comment")]
    return [(who, words) for who, words in out if words.strip()]

def quote_problem(where: str, q: str, allowed: list[tuple[str, str]]) -> str | None:
    """A quotation that isn't copied from anything quotable, with the nearest thing that is."""
    q = unbold(q)                          # bolding a word inside a quotation doesn't change the words
    if len(q.split()) < MIN_QUOTE_WORDS:
        return None
    nq = _norm(q)
    scored = [(fuzz.partial_ratio(nq, _norm(words)), who, words) for who, words in allowed]
    best, who, words = max(scored, default=(0, "", ""))
    if best >= QUOTE_MATCH:
        return None
    hint = f' The closest source quote is {who}: "{words[:200]}".' if words else ""
    return (f"{where} quotes words not found in any source quote: \"{q[:80]}\" (best match {best:.0f}%).{hint} "
            "Quote exactly or paraphrase without quotation marks")

def paragraph_problems(where: str, p: str, ids: set[str], allowed: list[tuple[str, str]]) -> list[str]:
    problems = []
    cited = cited_ids(p)
    if not cited:
        problems.append(f"{where} has no citation; end each sentence with the source ids it rests on, like [s1]")
    for sid in cited:
        if sid not in ids:
            problems.append(f"{where} cites unknown source id '{sid}'; use only {', '.join(sorted(ids))}")
    if URL.search(p):
        problems.append(f"{where} contains a URL or link; cite by source id instead")
    problems += [x for q in QUOTED.findall(p) if (x := quote_problem(where, q, allowed))]
    return problems

def section_problems(sec: Section, brief: ResearchBrief, records: list[OutreachRecord] | None = None,
                     possibilities: list[int] = (), counterpoint: bool = False) -> list[str]:
    """One section's problems as it is written: its paragraphs, plus what its plan asked of it
    (the possibility claims it must carry, numbered from 1, and the counterpoint if it is that section)."""
    ids, allowed = {s.id for s in brief.sources}, quotable(brief, records)
    problems = []
    if not sec.paragraphs or not any(p.strip() for p in sec.paragraphs):
        return ["The section has no paragraphs"]
    for n, p in enumerate(sec.paragraphs, 1):
        problems += paragraph_problems(f"Paragraph {n}", p, ids, allowed)
    text = " ".join(sec.paragraphs)
    if possibilities and not POSSIBILITY.search(text):
        which = ", ".join(str(n) for n in possibilities)
        problems.append(f"This section must carry claim(s) {which}, each worded 'It is possible that ...' and cited")
    if POSSIBILITY.search(text) and not any(c.kind == "possibility" for c in brief.claims):
        problems.append("The brief has no possibilities; do not write 'it is possible that'")
    contradicting = {s.id for s in brief.sources if s.stance == "contradicts"}
    if counterpoint and contradicting and not set(cited_ids(text)) & contradicting:
        problems.append(f"This is the counterpoint section: cite at least one of {sorted(contradicting)}")
    return problems

def claim_problems(claim: str, brief: ResearchBrief) -> list[str]:
    """The box at the top: checked like a paragraph, and it may only use 'it is possible that' if the brief can."""
    problems = paragraph_problems(CLAIM, claim, {s.id for s in brief.sources}, quotable(brief))
    if POSSIBILITY.search(claim) and not any(c.kind == "possibility" for c in brief.claims):
        problems.append(f"{CLAIM} says 'it is possible that' but the brief has no possibilities")
    return problems

def plan_problems(plan: ArticlePlan, brief: ResearchBrief) -> list[str]:
    problems = []
    if not plan.title.strip():
        problems.append("Title is empty")
    if not plan.dek.strip():
        problems.append("Dek (one-sentence summary) is empty")
    if not plan.claim.strip():
        problems.append(f"{CLAIM} is empty; state who did what and why it matters in 1 or 2 cited sentences")
    else:
        problems += claim_problems(plan.claim, brief)
    if not MIN_SECTIONS <= len(plan.sections) <= MAX_SECTIONS:
        problems.append(f"The plan has {len(plan.sections)} sections; plan {MIN_SECTIONS} to {MAX_SECTIONS}")
    count = len(brief.claims)
    used = {n for s in plan.sections for n in s.claims}
    if bad := sorted(n for n in used if not 1 <= n <= count):
        problems.append(f"Claim numbers {bad} don't exist; the brief's claims are numbered 1 to {count}")
    missing = [n for n, c in enumerate(brief.claims, 1) if c.kind == "possibility" and n not in used]
    if missing:
        problems.append(f"Possibility claim(s) {missing} are in no section; put each in the section it fits")
    if not any(s.counterpoint for s in plan.sections):
        problems.append("No section is marked counterpoint; one section must present the case against the thesis")
    return problems

def check_article(m: Mission, brief: ResearchBrief, article: Article,
                  records: list[OutreachRecord] | None = None) -> tuple[bool, str]:
    problems: list[str] = []
    ids = {s.id for s in brief.sources}
    contradicting = {s.id for s in brief.sources if s.stance == "contradicts"}
    allowed = quotable(brief, records)

    if not article.title.strip():
        problems.append("Title is empty")
    if not article.dek.strip():
        problems.append("Dek (one-sentence summary) is empty")

    if article.claim.strip():              # older articles have none
        problems += claim_problems(article.claim, brief)

    all_cited: set[str] = set()
    words = 0
    for sec in article.sections:
        for n, p in enumerate(sec.paragraphs, 1):
            words += len(p.split())
            all_cited.update(cited_ids(p))
            problems += paragraph_problems(f"'{sec.heading}' paragraph {n}", p, ids, allowed)

    if contradicting and not (all_cited & contradicting):
        problems.append(f"The opposing view is not cited; cite at least one of {sorted(contradicting)}")
    if words < MIN_WORDS:
        problems.append(f"Article is {words} words; write at least {MIN_WORDS}")

    brief_has_possibilities = any(c.kind == "possibility" for c in brief.claims)
    article_has_possibilities = any(POSSIBILITY.search(p) for sec in article.sections for p in sec.paragraphs)
    if article_has_possibilities and not brief_has_possibilities:
        problems.append("Article says 'it is possible that' but the brief has no possibilities; "
                        "use only what is in the brief")
    if brief_has_possibilities and not article_has_possibilities:
        problems.append("The brief's possibilities are missing; include them, worded 'It is possible that ...'")

    return (not problems, "\n".join(problems))
