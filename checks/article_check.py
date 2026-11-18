"""Article gate. Like the source validator, code decides; the model never grades its own work.

What code can check: every paragraph is cited, every citation points at a verified source in the
approved brief, no outside links, every direct quote is copied from a source quote or a POC comment,
the opposing view is cited, and possibilities appear only if the brief has them. Whether a citation
actually supports its sentence is the human's job at the ARTICLE_READY review.
"""
import re
from rapidfuzz import fuzz
from contracts import Mission, ResearchBrief, Article, OutreachRecord, cited_ids, POSSIBILITY
from checks.validator import _norm

MIN_WORDS = 300
QUOTE_MATCH = 90                           # rapidfuzz partial_ratio threshold, same as the validator
MIN_QUOTE_WORDS = 5                        # shorter quoted phrases are treated as terms, not quotations
URL = re.compile(r"https?://|www\.|\]\(", re.I)
QUOTED = re.compile(r"[\"“]([^\"“”]+)[\"”]")

def check_article(m: Mission, brief: ResearchBrief, article: Article,
                  records: list[OutreachRecord] | None = None) -> tuple[bool, str]:
    problems: list[str] = []
    ids = {s.id for s in brief.sources}
    contradicting = {s.id for s in brief.sources if s.stance == "contradicts"}
    quotable = [_norm(s.quote) for s in brief.sources]
    quotable += [_norm(r.get("text", "")) for rec in records or [] for r in rec.replies if r.get("is_comment")]

    if not article.title.strip():
        problems.append("Title is empty")
    if not article.dek.strip():
        problems.append("Dek (one-sentence summary) is empty")

    all_cited: set[str] = set()
    words = 0
    for sec in article.sections:
        for n, p in enumerate(sec.paragraphs, 1):
            where = f"'{sec.heading}' paragraph {n}"
            words += len(p.split())
            cited = cited_ids(p)
            all_cited.update(cited)
            if not cited:
                problems.append(f"{where} has no citation; cite a source id like [s1]")
            for sid in cited:
                if sid not in ids:
                    problems.append(f"{where} cites unknown source id '{sid}'")
            if URL.search(p):
                problems.append(f"{where} contains a URL or link; cite by source id instead")
            for q in QUOTED.findall(p):
                if len(q.split()) < MIN_QUOTE_WORDS:
                    continue
                nq = _norm(q)
                best = max((fuzz.partial_ratio(nq, t) for t in quotable if t), default=0)
                if best < QUOTE_MATCH:
                    problems.append(f"{where} quotes words not found in any source quote: \"{q[:80]}\" "
                                    f"(best match {best:.0f}%). Quote exactly or paraphrase without quotation marks")

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
