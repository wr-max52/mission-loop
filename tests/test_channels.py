"""Publishing channels: the local site and its feed, and 1F916 posting from the feed with its one-post-a-day
queue (against a fake server)."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx, pytest
import clock, orchestrator, run as cli, settings, store, ui
from agents import f916, publisher, stubs
from contracts import Mission, State, Article, Section
from starlette.testclient import TestClient

pytestmark = pytest.mark.usefixtures("stub_agents", "no_outreach")
T0 = datetime(2026, 11, 22, 9, 0, tzinfo=timezone.utc)

class FakeSite:
    """Answers like 1f916.ai: one post per UTC day of the fake clock, then a JSON 429."""
    def __init__(self):
        self.posts, self.day_used, self.refuse, self.today = [], None, set(), T0.date()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/post":
            assert request.headers["authorization"] == "Bearer 1f916_sk_test"
            body = json.loads(request.content)
            if body["title"] in self.refuse:
                return httpx.Response(409, json={"error": "near-duplicate of a recent post"})
            if self.day_used == self.today:
                return httpx.Response(429, json={"error": "one post per UTC day; yours is spent"})
            self.day_used = self.today
            self.posts.append(body)
            return httpx.Response(201, json={"post_id": 100 + len(self.posts)})
        if request.url.path == "/api/register":
            return httpx.Response(201, json={"handle": json.loads(request.content)["handle"],
                                             "secret": "1f916_sk_test", "citizen_id": 7, "now_utc": "x"})
        if request.url.path == "/api/me":
            ok = request.headers.get("authorization") == "Bearer 1f916_sk_test"
            return httpx.Response(200 if ok else 401, json={"handle": "loop-desk"} if ok else {"error": "bad"})
        return httpx.Response(404, json={"error": "no route"})

@pytest.fixture
def no_outreach(monkeypatch):
    import agents
    monkeypatch.setattr(settings, "outreach_on", lambda: False)   # as with outreach off: no reply wait

@pytest.fixture
def site(monkeypatch):
    fake = FakeSite()
    monkeypatch.setattr(f916, "TRANSPORT", httpx.MockTransport(fake))
    return fake

@pytest.fixture
def joined(site):
    Path("data").mkdir(exist_ok=True)
    f916.KEY_FILE.write_text(json.dumps({"handle": "loop-desk", "secret": "1f916_sk_test"}))
    return site

def run_at(t): orchestrator.run_until_idle(lambda: t)

def published(slug, at, topic=None):
    """A mission published to the local site at `at`, the way a pass leaves it."""
    store.save_mission(Mission(slug=slug, topic=topic or slug, approved=True, reviewed=True))
    run_at(at)
    assert store.get_mission(slug).state == State.DONE

def on_1f916(slug):
    post = store.get_mission(slug).channels.get("1f916")
    return post.where if post else None

def test_local_site_is_recorded_as_a_channel_and_1f916_stays_off_without_a_key():
    published("a", T0)                                 # the no-network transport would fail the test
    m = store.get_mission("a")
    assert m.channels["site"].where == "site/a/index.html" and "1f916" not in m.channels
    assert orchestrator.channels(m).keys() == {"site"}

def test_old_missions_count_as_published_to_the_site():
    m = Mission(slug="old", topic="t", state=State.DONE, published_at=T0)
    assert orchestrator.channels(m)["site"].where == "site/old/index.html"

def test_article_goes_to_1f916_in_the_same_pass_it_is_published(joined):
    published("a", T0)
    assert on_1f916("a") == "https://1f916.ai/api/post/101"
    assert joined.posts[0]["title"] == stubs.research(Mission(slug="a", topic="a")).thesis
    assert f916.spent_until() == datetime(2026, 11, 23, tzinfo=timezone.utc)
    assert any("posted to 1F916" in (e["note"] or "") for e in store.events("a"))

def test_one_post_a_day_newest_first_then_the_backlog(joined):
    for i, slug in enumerate(["oldest", "middle", "newest"]):   # backlog from before the channel existed
        store.save_mission(Mission(slug=slug, topic=slug, state=State.DONE,
                                   published_at=T0 - timedelta(days=3 - i)))
        m = store.get_mission(slug)
        brief, article = stubs.research(m), stubs.write(m, stubs.research(m), [])
        store.save_artifact(slug, "brief", brief)
        store.save_artifact(slug, "article", article)
        publisher.write_page(m, brief, article, m.published_at)    # on the site and in the feed
    run_at(T0)
    run_at(T0 + timedelta(hours=5))                    # same UTC day: nothing more, and no request made
    assert [on_1f916(s) is not None for s in ("newest", "middle", "oldest")] == [True, False, False]
    assert len(joined.posts) == 1
    published("fresh", T0 + timedelta(hours=6))        # a new article waits for tomorrow too
    assert on_1f916("fresh") is None
    for day in (1, 2, 3):
        joined.today = (T0 + timedelta(days=day)).date()
        run_at(T0 + timedelta(days=day))
    order = sorted(["newest", "middle", "oldest", "fresh"], key=lambda s: on_1f916(s) or "~")
    assert order == ["newest", "fresh", "middle", "oldest"]
    assert orchestrator.queue(f916) == []

def test_daily_cap_from_the_site_waits_until_the_next_utc_day(joined):
    joined.day_used = joined.today                     # e.g. someone posted by hand with the same key
    published("a", T0)
    assert on_1f916("a") is None
    assert f916.spent_until() == datetime(2026, 11, 23, tzinfo=timezone.utc)
    joined.today = (T0 + timedelta(days=1)).date()
    run_at(T0 + timedelta(days=1, minutes=1))
    assert on_1f916("a")

def test_edge_rate_limit_does_not_spend_the_day(monkeypatch, joined):
    monkeypatch.setattr(f916, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(429, text="slow down")))
    published("a", T0)
    assert on_1f916("a") is None and f916.spent_until() is None

def test_refused_article_costs_nothing_and_goes_to_the_back(joined):
    joined.day_used = joined.today
    published("older", T0)
    published("refused-one", T0 + timedelta(hours=1))  # newest, but the site will refuse it
    joined.today = (T0 + timedelta(days=1)).date()
    joined.refuse.add("Stub thesis about refused-one")
    run_at(T0 + timedelta(days=1))
    m = store.get_mission("refused-one")
    assert "near-duplicate" in m.channel_errors["1f916"] and on_1f916("refused-one") is None
    assert on_1f916("older")                           # the next in line went out the same pass
    assert [e["slug"] for e in orchestrator.queue(f916)] == ["refused-one"]

def test_network_failure_loses_nothing(monkeypatch, joined):
    def down(request): raise httpx.ConnectError("no route to host")
    monkeypatch.setattr(f916, "TRANSPORT", httpx.MockTransport(down))
    published("a", T0)
    assert on_1f916("a") is None and "ConnectError" in f916.load_state()["note"]
    monkeypatch.setattr(f916, "TRANSPORT", httpx.MockTransport(joined))
    run_at(T0 + timedelta(minutes=15))
    assert on_1f916("a")

def test_long_article_fits_the_limit_and_keeps_sources_and_labels():
    m = Mission(slug="x", topic="x", unchecked_article="bad quote")
    brief = stubs.research(m)
    long = "🤖 It is possible that a vendor was shared [s1]. " + "Padding sentence [s2]. " * 600
    article = Article(title="T" * 200, dek="d", sections=[Section(heading="A", paragraphs=[long]),
                                                          Section(heading="B", paragraphs=["x [s4]."])])
    from render import article_text
    entry = {"slug": "x", "title": article.title,
             "text": article_text(brief, article, article_checked=False)}
    title, body = f916.post_content(entry)
    assert f916._units(title) <= 120 and f916._units(body) <= 8000
    assert "[Possibility, not verified] It is possible that" in body
    assert "Shortened to fit" in body and "## Sources" in body and "https://d.example.com" in body
    assert "Failed automated checks" in body and "failed the automated check of its quotes" in body

def test_join_saves_the_key_privately_and_checks_it(site):
    me = f916.register("loop-desk", "big-test:30b")
    assert me["handle"] == "loop-desk"
    assert f916.key()["secret"] == "1f916_sk_test"
    assert f916.key()["api"] == "https://1f916.ai"     # the key remembers the server that issued it
    assert oct(f916.KEY_FILE.stat().st_mode & 0o777) == "0o600"
    with pytest.raises(FileExistsError):
        f916.register("second", "m")

def test_ui_and_status_show_where_it_was_published(joined, capsys, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: T0 + timedelta(hours=2))   # status reads the clock: the same day as T0
    published("a", T0)
    published("b", T0 + timedelta(hours=1))
    client = TestClient(ui.app)
    assert "1F916: <strong>loop-desk</strong>" not in client.get("/").text   # the Missions page only links there
    pub = client.get("/published").text
    assert "1F916: <strong>loop-desk</strong>" in pub and "1 queued" in pub
    assert "<strong>1F916</strong>: queued, #1 of 1" in pub
    assert "<strong>1F916</strong>: posted" in pub and "https://1f916.ai/api/post/101" in pub
    page = client.get("/m/a").text
    assert "https://1f916.ai/api/post/101" in page and "site/a/index.html" in page
    assert "queued, #1 of 1" in client.get("/m/b").text
    cli.status()
    out = capsys.readouterr().out
    assert "published: Local site, 1F916" in out and "1F916 as loop-desk: 1 queued, next post after" in out

def test_ui_says_how_to_join_when_off():
    published("a", T0)
    client = TestClient(ui.app)
    assert 'href="/settings#publishing"' in client.get("/published").text
    assert "<strong>1F916</strong> · built in" in client.get("/settings").text and "join-1f916" in client.get("/settings").text

def test_1f916_posts_the_feed_entry(site):
    published("a", T0)                                 # on the site before 1F916 is set up
    meta = Path("site/a/meta.json")
    entry = json.loads(meta.read_text())
    meta.write_text(json.dumps(entry | {"title": "Feed title", "text": "Feed text\n\n## Sources\n\n[1] x"}))
    f916.KEY_FILE.write_text(json.dumps({"handle": "loop-desk", "secret": "1f916_sk_test"}))
    run_at(T0 + timedelta(minutes=15))
    assert site.posts == [{"title": "Feed title", "body": "Feed text\n\n## Sources\n\n[1] x"}]

def test_nothing_is_posted_twice_and_entries_without_text_wait(joined):
    published("a", T0)
    assert on_1f916("a") and orchestrator.queue(f916) == []
    joined.today = (T0 + timedelta(days=1)).date()
    run_at(T0 + timedelta(days=1))
    assert len(joined.posts) == 1
    m = store.get_mission("a")
    m.channels.pop("1f916")
    store.save_mission(m)
    meta = Path("site/a/meta.json")
    meta.write_text(json.dumps({k: v for k, v in json.loads(meta.read_text()).items() if k != "text"}))
    assert orchestrator.queue(f916) == []              # no text yet: the next pass's refresh_feed adds it
