from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
import agents, notify, orchestrator, store
from agents import stubs
from contracts import Mission, State

pytestmark = pytest.mark.usefixtures("stub_agents")
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)

def run_at(t): orchestrator.run_until_idle(lambda: t)
def get(slug): return store.get_mission(slug)
def flag(slug, **fields):
    m = get(slug)
    for k, v in fields.items():
        setattr(m, k, v)
    store.save_mission(m)

def to_article_ready(slug, topic="x"):
    store.save_mission(Mission(slug=slug, topic=topic))
    run_at(T0)
    flag(slug, approved=True)
    run_at(T0)
    run_at(T0 + timedelta(hours=48))

def test_review_publishes_to_static_site():
    to_article_ready("p")
    assert get("p").state == State.ARTICLE_READY
    assert Path("missions/p/preview.html").exists()
    flag("p", reviewed=True)
    run_at(T0 + timedelta(hours=49))
    m = get("p")
    assert m.state == State.DONE and m.published_at is not None
    page = Path("site/p/index.html").read_text()
    assert "Stub thesis" in page and 'href="#src-1"' in page
    assert 'href="p/"' in Path("site/index.html").read_text()

def test_no_outreach_skips_the_wait(monkeypatch):
    monkeypatch.setattr(agents, "interview", stubs.no_interview)
    store.save_mission(Mission(slug="n", topic="x"))
    run_at(T0)
    flag("n", approved=True)
    run_at(T0)                                        # same instant: nobody asked, nothing to wait for
    assert get("n").state == State.ARTICLE_READY

def test_reject_article_goes_back_to_writer():
    to_article_ready("r")
    orchestrator.reject(get("r"), "lede buries the vote count", T0)
    m = get("r")
    assert m.state == State.WRITING and m.write_attempts == 0 and not m.reviewed
    assert "lede buries the vote count" in m.validator_notes

def test_failed_article_parks_then_requeues_to_writing():
    to_article_ready("f", topic="BADARTICLE")
    m = get("f")
    assert m.state == State.PARKED and m.write_attempts == 3 and m.attempts == 1
    orchestrator.requeue(m, T0)
    assert get("f").state == State.WRITING             # research is not redone

def test_writer_crash_counts_as_failed_attempt(monkeypatch):
    def boom(m, brief, records): raise RuntimeError("ollama is not running")
    monkeypatch.setattr(agents, "write", boom)
    to_article_ready("c")
    m = get("c")
    assert m.state == State.PARKED and "ollama is not running" in m.validator_notes

def test_notifications_fire_once_per_state(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda title, msg: sent.append(title))
    store.save_mission(Mission(slug="q", topic="x"))
    run_at(T0)
    seen = set()
    notify.announce(store.all_missions(), seen)
    notify.announce(store.all_missions(), seen)
    assert sent == ["Mission Loop: q"]
    flag("q", approved=True)                           # approved brief no longer needs a human
    sent.clear()
    seen.clear()
    notify.announce(store.all_missions(), seen)
    assert sent == []

def test_source_numbers_follow_first_citation_without_gaps():
    from render import article_html
    from contracts import Article, Section
    brief = stubs.research(Mission(slug="x", topic="x"))
    a = Article(title="t", dek="d", sections=[
        Section(heading="A", paragraphs=["One [s3]. Two [s3, s1]. Three [s3]."]),
        Section(heading="B", paragraphs=["Four [s4]."])])
    html = article_html(brief, a, published_at=None)
    assert '<sup><a href="#src-1">[1]</a></sup>' in html and 'href="#src-5"' not in html
    assert 'id="src-1" value="1"><span class="stance">supports</span><br><a href="https://c.example.com"' in html
