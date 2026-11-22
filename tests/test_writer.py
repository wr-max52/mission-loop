import re
import pytest
from ollama import Message
import stopping
from agents import llm, writer
from agents.stubs import research as stub_brief, write as stub_article
from contracts import ArticlePlan, Claim, Mission, PlannedSection, RejectedArticle, Section

M = Mission(slug="w", topic="Heat pumps")
BRIEF = stub_brief(M)
PLAN = ArticlePlan(title="Do heat pumps hold up?", dek="A look at the record.",
                   claim="**Heat pumps** hold up in the cold, the record shows [s1].", sections=[
    PlannedSection(heading="Finding", point="The thesis", claims=[1]),
    PlannedSection(heading="The record", point="What the sources show"),
    PlannedSection(heading="The other side", point="The counterpoint", counterpoint=True)])

def section(heading, cite="s1"):
    return Section(heading=heading, paragraphs=[f"The record shows something about heat pumps [{cite}]. " * 12])

def fake(monkeypatch, replies=None):
    """llm.chat answering each schema the writer asks for: the plan, then good sections, unless `replies`
    gives the replies in order. Records the system prompt, the last message and the schema of every call."""
    seen, it = [], iter(replies or [])
    async def fake_chat(messages, tools=None, format=None):
        seen.append({"system": messages[0]["content"], "last": messages[-1]["content"], "tools": tools,
                     "format": format})
        if replies is not None:
            reply = next(it)
            if isinstance(reply, Exception):
                raise reply
            return Message(role="assistant", content=reply if isinstance(reply, str) else reply.model_dump_json())
        if format["title"] == "ArticlePlan":
            return Message(role="assistant", content=PLAN.model_dump_json())
        heading = re.search(r'Write section \d+ of \d+: "(.*?)"', messages[-1]["content"]).group(1)
        return Message(role="assistant", content=section(heading, "s4" if heading == "The other side" else "s1")
                       .model_dump_json())
    monkeypatch.setattr(llm, "chat", fake_chat)
    return seen

@pytest.fixture
def m():
    return M.model_copy(deep=True)

async def test_writer_plans_then_writes_each_section(monkeypatch, m):
    seen = fake(monkeypatch)
    a = await writer.run_writer(m, BRIEF, [])
    assert a.title == PLAN.title and [s.heading for s in a.sections] == ["Finding", "The record", "The other side"]
    assert [c["format"]["title"] for c in seen] == ["ArticlePlan", "Section", "Section", "Section"]
    assert all(c["tools"] is None for c in seen)                        # structured output, no tools
    assert "Written so far:\n## Finding" in seen[2]["last"]             # each section sees the ones before it
    assert "claim 1 (fact): Stub claim [s1, s2]" in seen[1]["last"]     # and the claims the plan gave it

async def test_bad_json_gets_one_repair(monkeypatch, m):
    good = [section("Finding"), section("The record"), section("The other side", "s4")]
    fake(monkeypatch, ["{nope", PLAN, *good])
    assert len((await writer.run_writer(m, BRIEF, [])).sections) == 3

async def test_bad_json_twice_raises(monkeypatch, m):
    fake(monkeypatch, ["{nope", "{still nope"])
    with pytest.raises(writer.WritingFailed):
        await writer.run_writer(m, BRIEF, [])

async def test_prompt_keeps_possibilities_labeled(monkeypatch, m):
    seen = fake(monkeypatch)
    await writer.run_writer(m, BRIEF, [])
    assert 'kind "possibility" are unverified' in seen[0]["system"]

async def test_prompt_has_notes_and_no_contact_emails(monkeypatch, m):
    seen = fake(monkeypatch)
    m.validator_notes = "paragraph 2 has no citation"
    await writer.run_writer(m, BRIEF, [])
    assert "paragraph 2 has no citation" in seen[0]["system"]
    assert "poc1@example.com" not in seen[0]["system"]                  # POC emails never reach the article

def test_a_retry_after_a_failed_check_revises_the_draft_it_failed():
    draft = stub_article(M, BRIEF, [])
    m = M.model_copy(update={"validator_notes": "'Intro' paragraph 2 has no citation",
                             "rejected_article": RejectedArticle(article=draft, notes="'Intro' paragraph 2 has no citation")})
    system = writer.prompt(m, BRIEF, [])
    assert "Revise it" in system and draft.title in system
    human = M.model_copy(update={"validator_notes": "Human reviewer feedback: shorter"})
    assert "Revise it" not in writer.prompt(human, BRIEF, [])

async def test_a_section_that_fails_its_check_gets_its_problems_back(monkeypatch, m):
    uncited = Section(heading="Finding", paragraphs=["Heat pumps work, see https://x.example.com."])
    seen = fake(monkeypatch, [PLAN, uncited, section("Finding"), section("The record"), section("The other side", "s4")])
    a = await writer.run_writer(m, BRIEF, [])
    assert "Paragraph 1 has no citation" in seen[2]["last"] and "contains a URL" in seen[2]["last"]
    assert a.sections[0] == section("Finding")

async def test_a_quote_not_in_any_source_is_shown_the_closest_real_one(monkeypatch, m):
    brief = BRIEF.model_copy(deep=True)
    brief.sources[0].quote = "The pumps held their temperature through the coldest week on record."
    misquoted = Section(heading="Finding", paragraphs=[
        'The report said "the pumps kept their heat through the coldest month ever recorded" [s1].'])
    seen = fake(monkeypatch, [PLAN, misquoted, section("Finding"), section("The record"), section("The other side", "s4")])
    await writer.run_writer(m, brief, [])
    assert 'The closest source quote is s1: "The pumps held their temperature' in seen[2]["last"]

async def test_the_counterpoint_section_must_cite_a_contradicting_source(monkeypatch, m):
    seen = fake(monkeypatch, [PLAN, section("Finding"), section("The record"),
                              section("The other side", "s1"), section("The other side", "s4")])
    a = await writer.run_writer(m, BRIEF, [])
    assert "cite at least one of ['s4']" in seen[4]["last"] and "[s4]" in a.sections[2].paragraphs[0]

async def test_a_stop_keeps_the_plan_and_finished_sections_and_writing_resumes_there(monkeypatch, m):
    fake(monkeypatch, [PLAN, section("Finding"), llm.Cut("")])
    with pytest.raises(stopping.Interrupted) as stop:
        await writer.run_writer(m, BRIEF, [])
    assert stop.value.draft is None and m.article_draft.plan == PLAN and len(m.article_draft.sections) == 1
    seen = fake(monkeypatch)
    a = await writer.run_writer(m, BRIEF, [])
    assert [c["format"]["title"] for c in seen] == ["Section", "Section"]   # no new plan, no rewriting section 1
    assert len(a.sections) == 3

async def test_after_a_failed_check_only_the_failing_section_is_written_again(monkeypatch, m):
    fake(monkeypatch)
    article = await writer.run_writer(m, BRIEF, [])
    notes = "'The record' paragraph 1 has no citation; end each sentence with the source ids it rests on, like [s1]"
    m.validator_notes, m.rejected_article = notes, RejectedArticle(article=article, notes=notes)
    seen = fake(monkeypatch)
    await writer.run_writer(m, BRIEF, [])
    assert len(seen) == 1 and '"The record"' in seen[0]["last"]
    assert "paragraph 1 has no citation" in seen[0]["last"] and "Revise it" in seen[0]["last"]
    assert "Your previous draft failed" not in seen[0]["system"]           # revised section by section instead

async def test_a_plan_still_wrong_after_its_fix_is_settled_by_code(monkeypatch, m):
    brief = BRIEF.model_copy(deep=True)
    brief.claims.append(Claim(text="It is possible that X, based on s2.", source_ids=["s2"], kind="possibility"))
    lazy = ArticlePlan(title="T", dek="D", sections=[PlannedSection(heading="A", point="a", claims=[1, 7]),
                                                     PlannedSection(heading="B", point="b"),
                                                     PlannedSection(heading="C", point="c")])
    carries = Section(heading="A", paragraphs=["It is possible that X, based on s2 [s2]. " + "More [s1]. " * 20])
    seen = fake(monkeypatch, [lazy, lazy, carries, section("B"), section("C"), section("The other side", "s4")])
    a = await writer.run_writer(m, brief, [])
    assert "Possibility claim(s) [2] are in no section" in seen[1]["last"] and "don't exist" in seen[1]["last"]
    plan = m.article_draft.plan
    assert plan.sections[0].claims == [1, 2] and plan.sections[-1].counterpoint   # code placed claim 2, added the other side
    assert [s.heading for s in a.sections] == ["A", "B", "C", "The other side"]

async def test_a_crash_after_a_failed_check_still_rewrites_the_failing_section(monkeypatch, m):
    fake(monkeypatch)
    article = await writer.run_writer(m, BRIEF, [])
    notes = "'Finding' paragraph 1 contains a URL or link; cite by source id instead"
    m.rejected_article = RejectedArticle(article=article, notes=notes)
    m.validator_notes = "writer crashed: ResponseError: boom"                 # the attempt after the failure crashed
    seen = fake(monkeypatch)
    await writer.run_writer(m, BRIEF, [])
    assert len(seen) == 1 and '"Finding"' in seen[0]["last"] and "contains a URL" in seen[0]["last"]

async def test_a_section_citing_a_source_by_its_url_is_given_the_id(monkeypatch, m):
    by_url = Section(heading="Finding", paragraphs=["The record shows it [https://a.example.com]. " * 12])
    seen = fake(monkeypatch, [PLAN, by_url, section("The record"), section("The other side", "s4")])
    a = await writer.run_writer(m, BRIEF, [])
    assert "[s1]" in a.sections[0].paragraphs[0] and "https://" not in a.sections[0].paragraphs[0]
    assert len(seen) == 4                  # fixed by code, so no round of feedback was needed

# --- the claim at the top, the story first, the fine print last, bold key words -----------------------------

async def test_the_article_carries_the_plans_claim_box(monkeypatch, m):
    fake(monkeypatch)
    a = await writer.run_writer(m, BRIEF, [])
    assert a.claim == PLAN.claim

async def test_a_plan_without_a_claim_box_is_asked_for_one(monkeypatch, m):
    bare = PLAN.model_copy(update={"claim": ""})
    seen = fake(monkeypatch, [bare, PLAN, section("Finding"), section("The record"), section("The other side", "s4")])
    a = await writer.run_writer(m, BRIEF, [])
    assert "The claim at the top is empty" in seen[1]["last"] and a.claim == PLAN.claim

async def test_a_claim_box_still_failing_after_its_fix_is_left_out(monkeypatch, m):
    uncited = PLAN.model_copy(update={"claim": "Heat pumps hold up."})
    seen = fake(monkeypatch, [uncited, uncited, section("Finding"), section("The record"),
                              section("The other side", "s4")])
    a = await writer.run_writer(m, BRIEF, [])
    assert "The claim at the top has no citation" in seen[1]["last"]
    assert a.claim == "" and len(a.sections) == 3        # the article opens without a box instead of failing

async def test_code_tidies_bold_where_it_cannot_show(monkeypatch, m):
    loud = PLAN.model_copy(update={"title": "Do **heat pumps** hold up?", "dek": "**Why** it matters.",
                                   "claim": "**Heat pumps hold up [s1]."})
    stray = Section(heading="Finding", paragraphs=["**The county paid twice [s1]. " * 12])
    fake(monkeypatch, [loud, stray, section("The record"), section("The other side", "s4")])
    a = await writer.run_writer(m, BRIEF, [])
    assert a.title == "Do heat pumps hold up?" and a.dek == "Why it matters."
    assert a.claim == "Heat pumps hold up [s1]." and "**" not in a.sections[0].paragraphs[0]

async def test_the_opening_tells_a_story_and_the_last_section_is_the_fine_print(monkeypatch, m):
    seen = fake(monkeypatch)
    await writer.run_writer(m, BRIEF, [])
    first, middle, last = (c["last"] for c in seen[1:])
    assert "This is the opening. Start with a short scene" in first
    assert "This section is evidence" in middle
    assert "This is the last section: the fine print" in last
    assert "**double asterisks**" in seen[0]["system"] and "plain language" in seen[0]["system"]
