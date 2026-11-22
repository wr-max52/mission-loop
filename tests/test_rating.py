"""The article rating: the Research Assistant scores an article that passed the article check, and a total of 20 or
less sends it back to the writer before any human review."""
from datetime import datetime, timezone
from pathlib import Path
import pytest
from starlette.testclient import TestClient
import agents, orchestrator, stopping, store, ui
from agents import llm, rater, stubs, writer
from contracts import ArticleRating, Mission, RatingReply, RejectedArticle, State

pytestmark = pytest.mark.usefixtures("stub_agents")    # the stub article passes the stub check; the stub rater gives 24

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)

def rating(logic, intrigue, sources) -> ArticleRating:
    return ArticleRating(logic=logic, logic_why="the claim leaps", intrigue=intrigue, intrigue_why="flat opening",
                         sources=sources, sources_why="one blog")

def writing(slug="w", topic="Heat pumps") -> Mission:
    """A mission at the writing stage, with its brief saved."""
    m = Mission(slug=slug, topic=topic, approved=True, state=State.WRITING)
    store.save_mission(m)
    store.save_artifact(slug, "brief", stubs.research(m))
    store.save_artifact(slug, "outreach", [])
    return m

def test_scores_are_clamped_and_twenty_or_less_fails():
    r = ArticleRating(logic=14, intrigue="6", sources=0)
    assert (r.logic, r.intrigue, r.sources) == (10, 6, 1)
    assert not rating(7, 7, 6).passed and rating(7, 7, 7).passed     # 20 is sent back, 21 goes on
    assert rating(7, 7, 6).notes().splitlines()[0] == "Rated 20/30 (20 or less is sent back to the writer):"
    assert "- Intrigue 7/10: flat opening" in rating(7, 7, 6).notes()
    assert ArticleRating(error="boom").passed                         # a rater that couldn't rate holds nothing back

def test_the_rater_reads_the_article_and_its_cited_sources_on_the_small_model(monkeypatch):
    m = Mission(slug="r", topic="Heat pumps")
    brief = stubs.research(m)
    article = stubs.write(m, brief, [])
    seen = {}

    async def ask(messages, schema, who, failure, model=None):
        seen.update(messages=messages, schema=schema, model=model)
        return RatingReply(logic=8, logic_why="tight", intrigue=6, intrigue_why="ok", sources=7, sources_why="records")

    monkeypatch.setattr(llm, "ask", ask)
    r = rater.rate(m, brief, article)
    assert isinstance(r, ArticleRating) and r.total == 21 and r.passed
    assert seen["model"] == "small-test:1b" and seen["schema"] is RatingReply   # the Research Assistant's model
    text = seen["messages"][1]["content"]
    assert article.title in text and article.sections[0].paragraphs[0] in text
    cited = {"s1", "s2", "s4"}                                        # what the stub article cites
    for s in brief.sources:
        assert (f"[{s.id}]" in text) == (s.id in cited)

def test_a_checked_article_is_the_research_assistants_next_task():
    m = writing()
    assert orchestrator.role(m) == "pi"
    note = orchestrator.step(m, T0)
    assert "the rating is next" in note and m.checked_article and m.state == State.WRITING
    assert orchestrator.role(m) == "assistant" and m.write_attempts == 1
    note = orchestrator.step(m, T0)
    assert m.state == State.ARTICLE_READY and m.checked_article is None and m.write_attempts == 1
    assert m.article_rating.total == 24 and "rated 24/30 on attempt 1" in note
    assert store.load_artifact("w", "article")["title"] == stubs.research(m).thesis

def test_a_low_rating_sends_the_article_back_and_parks_after_three(monkeypatch):
    monkeypatch.setattr(agents, "rate_article", lambda *a: rating(7, 7, 6))
    store.save_mission(writing())
    orchestrator.run_until_idle(lambda: T0)
    m = store.get_mission("w")
    assert m.state == State.PARKED and m.write_attempts == 3 and store.load_artifact("w", "article") is None
    assert m.rejected_article.rating.total == 20 and m.validator_notes.startswith("Rated 20/30")
    assert "## Rating\nRated 20/30" in Path("missions/w/article.rejected.md").read_text()
    flow = TestClient(ui.app).get("/api/flow").json()
    assert flow["parked"] == {"source_check": 0, "article_check": 0, "rating_check": 1}
    orchestrator.push_through(m, T0)                                  # a human may still push it to review
    m = store.get_mission("w")
    assert m.state == State.ARTICLE_READY and m.unchecked_article.startswith("Rated 20/30")
    assert m.article_rating.total == 20

def test_a_sent_back_article_is_rewritten_from_the_raters_reasons(monkeypatch):
    ratings = iter([rating(5, 5, 5), rating(8, 8, 8)])
    monkeypatch.setattr(agents, "rate_article", lambda *a: next(ratings))
    prompts = []

    def write(m, brief, records):
        prompts.append(writer.prompt(m, brief, records))
        return stubs.write(m, brief, records)

    monkeypatch.setattr(agents, "write", write)
    store.save_mission(writing())
    orchestrator.run_until_idle(lambda: T0)
    m = store.get_mission("w")
    assert m.state == State.ARTICLE_READY and m.write_attempts == 2 and m.article_rating.total == 24
    assert "editor" not in prompts[0]
    assert "rated it too weak" in prompts[1] and "Intrigue 5/10: flat opening" in prompts[1]
    assert m.validator_notes == "" and m.rejected_article is None

def test_a_crashed_rater_sends_the_article_to_review_unrated(monkeypatch):
    def crash(*a):
        raise RuntimeError("no model")
    monkeypatch.setattr(agents, "rate_article", crash)
    m = writing()
    orchestrator.step(m, T0)
    note = orchestrator.step(m, T0)
    assert m.state == State.ARTICLE_READY and m.article_rating.error.endswith("RuntimeError: no model")
    assert "not rated" in note

def test_a_stop_mid_rating_keeps_the_article_for_the_next_start(monkeypatch):
    def stopped(*a):
        raise stopping.Interrupted(None)
    monkeypatch.setattr(agents, "rate_article", stopped)
    m = writing()
    orchestrator.step(m, T0)
    note = orchestrator.step(m, T0)
    assert "stopped mid-rating" in note and m.checked_article and m.state == State.WRITING and m.write_attempts == 1

def test_the_review_shows_the_rating():
    m = writing()
    orchestrator.step(m, T0)
    orchestrator.step(m, T0)
    store.save_mission(m)
    client = TestClient(ui.app)
    page = client.get("/m/w").text
    assert "Rated 24/30</strong> by the Research Assistant" in page and "Credibility of the sources" in page
    assert "Rated 24/30 by the Research Assistant (claim and logic 8, intrigue 8, sources 8)." in page
    assert "Rated 24/30" in client.get("/").text                     # on the review card under Needs you
