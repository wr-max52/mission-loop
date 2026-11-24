from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
import agents, notify, orchestrator, settings, store
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

def test_theme_toggle_reaches_the_published_and_preview_pages():
    to_article_ready("p")
    preview = Path("missions/p/preview.html").read_text()
    assert 'class="theme-toggle"' in preview and "window.toggleTheme" in preview
    flag("p", reviewed=True)
    run_at(T0 + timedelta(hours=49))
    site = Path("site/p/index.html").read_text()
    index = Path("site/index.html").read_text()
    for html in (site, index):
        assert 'class="theme-toggle"' in html and "window.toggleTheme" in html

def test_the_published_site_has_the_favicon_and_the_mark_beside_its_name():
    # site/ is what GitHub Pages serves too (agents/github.py keeps its repo identical to it)
    to_article_ready("p")
    flag("p", reviewed=True)
    run_at(T0 + timedelta(hours=49))
    for html in (Path("site/p/index.html").read_text(), Path("site/index.html").read_text()):
        assert '<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,' in html
        assert '<a class="brand" href="' in html and '<svg class="logo" aria-hidden="true"' in html

def test_no_outreach_skips_the_wait(monkeypatch):
    monkeypatch.setattr(settings, "outreach_on", lambda: False)
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

def _brief_and_article(paragraph):
    from contracts import Article, Claim, Section
    brief = stubs.research(Mission(slug="x", topic="x"))
    brief.claims.append(Claim(text="It is possible that a vendor was shared", source_ids=["s1"], kind="possibility"))
    return brief, Article(title="t", dek="d", sections=[
        Section(heading="A", paragraphs=[paragraph]), Section(heading="B", paragraphs=["Other [s4]."])])

def test_possibilities_are_labeled_in_the_article():
    from render import article_html, article_markdown, POSSIBILITY_LABEL
    brief, a = _brief_and_article("A fact [s1]. It is possible that a vendor was shared [s2].")
    html = article_html(brief, a, published_at=None)
    assert f'<span class="stance">{POSSIBILITY_LABEL}</span> It is possible that' in html
    assert "not verified facts" in html                # the footer must not imply they were checked
    assert f"**[{POSSIBILITY_LABEL}]** It is possible that" in article_markdown("x", brief, a)

def test_the_claim_box_leads_the_article_and_bold_words_show():
    from render import article_html, article_markdown, article_text, CLAIM_LABEL, POSSIBILITY_LABEL
    brief, a = _brief_and_article("**The county paid** the vendor **twice** [s1].")
    a = a.model_copy(update={"claim": "It is possible that **the vendor was shared** [s2]."})
    html = article_html(brief, a, published_at=None)
    assert html.index('<aside class="claim">') < html.index("<h2>A</h2>")          # above the article body
    assert f'<span class="stance">{CLAIM_LABEL}</span>' in html
    assert f"{POSSIBILITY_LABEL}</span> It is possible that <strong>the vendor was shared</strong>" in html
    assert "<strong>The county paid</strong> the vendor <strong>twice</strong>" in html and "**" not in html
    assert '<sup><a href="#src-1">[1]</a></sup>.</p></aside>' in html               # the box's source is [1]
    assert f"> **{CLAIM_LABEL}:** **[{POSSIBILITY_LABEL}]** It is possible that" in article_markdown("x", brief, a)
    text = article_text(brief, a)
    assert text.index(f"{CLAIM_LABEL}: [{POSSIBILITY_LABEL}]") < text.index("## A")
    assert "not verified facts" in html                # the box's possibility counts for the footer too

def test_an_article_without_a_claim_box_has_none():
    from render import article_html, CLAIM_LABEL
    brief, a = _brief_and_article("A fact [s1].")
    assert 'class="claim"' not in article_html(brief, a, published_at=None)

def test_footer_stays_plain_without_possibilities():
    from render import article_html, POSSIBILITY_LABEL
    brief, a = _brief_and_article("A fact [s1].")
    html = article_html(brief, a, published_at=None)
    assert POSSIBILITY_LABEL not in html and "not verified facts" not in html

def test_brief_lists_facts_and_possibilities_separately():
    from render import brief_html, brief_markdown, FACTS_HEADING, POSSIBILITIES_HEADING
    brief, _ = _brief_and_article("x")
    for text in (brief_markdown("x", brief), brief_html(brief)):
        assert text.index(FACTS_HEADING) < text.index("Stub claim") < text.index(POSSIBILITIES_HEADING) \
            < text.index("a vendor was shared")

def test_counterpoint_without_a_contradicting_source_is_marked_unsourced():
    from render import brief_html, brief_markdown, UNSOURCED
    brief = stubs.research(Mission(slug="x", topic="x"))
    assert UNSOURCED not in brief_markdown("x", brief)       # the stub cites a contradicting source
    for s in brief.sources:
        s.stance = "supports"
    assert UNSOURCED in brief_markdown("x", brief) and UNSOURCED in brief_html(brief)

def test_briefs_saved_before_kinds_existed_still_load_as_facts():
    from contracts import ResearchBrief
    data = stubs.research(Mission(slug="x", topic="x")).model_dump()
    for claim in data["claims"]:
        del claim["kind"]
    assert ResearchBrief(**data).claims[0].kind == "fact"

def parked_at_writing(slug="w"):
    store.save_mission(Mission(slug=slug, topic="BADARTICLE", approved=True))
    run_at(T0); run_at(T0 + timedelta(hours=48)); run_at(T0 + timedelta(hours=48))
    m = store.get_mission(slug)
    assert m.state == State.PARKED and orchestrator.parked_while_writing(m)
    return m

def test_failed_article_is_kept_for_humans():
    m = parked_at_writing()
    assert m.rejected_article.notes == "stub: forced article failure"
    text = Path("missions/w/article.rejected.md").read_text()
    assert "Stub thesis" in text and "stub: forced article failure" in text

def test_push_through_article_reaches_review_then_publishes_marked_as_failed():
    orchestrator.push_through(parked_at_writing(), T0)
    m = store.get_mission("w")
    assert m.state == State.ARTICLE_READY and not m.reviewed and m.rejected_article is None
    assert m.unchecked_article == "stub: forced article failure"
    assert "Failed automated checks" in Path("missions/w/preview.html").read_text()
    run_at(T0)
    assert store.get_mission("w").state == State.ARTICLE_READY     # still waits for the human
    m.reviewed = True
    store.save_mission(m)
    run_at(T0)
    assert store.get_mission("w").state == State.DONE
    html = Path("site/w/index.html").read_text()
    assert "Failed automated checks" in html and "failed the automated check of its quotes" in html
    assert "Direct quotes and citations in the article were checked" not in html

def test_pushed_brief_is_not_described_as_verified_on_the_site():
    from render import article_html
    m = Mission(slug="x", topic="t")
    brief = stubs.research(m)
    article = stubs.write(m, brief, [])
    html = article_html(brief, article, published_at=None, brief_checked=False)
    assert "failed the automated source check" in html
    assert "Every source above was fetched" not in html
    clean = article_html(brief, article, published_at=None)
    assert "Every source above was fetched" in clean and "Failed automated checks" not in clean

def test_writer_does_not_get_a_pushed_briefs_notes_as_its_own():
    from agents.writer import prompt
    store.save_mission(Mission(slug="p", topic="FAIL"))
    run_at(T0)
    orchestrator.push_through(store.get_mission("p"), T0)
    m = store.get_mission("p")
    assert "stub: forced failure" not in prompt(m, stubs.research(m), [])
