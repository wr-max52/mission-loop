"""The Bluesky channel, against a fake server: login, pacing, newest first, links, labels and failures."""
import json
from datetime import timedelta
from pathlib import Path
import httpx, pytest
import agents, orchestrator, run as cli, settings, store, ui
from agents import bluesky, f916, publisher, stubs
from contracts import Mission, State
from starlette.testclient import TestClient
from test_channels import FakeSite, published, run_at, T0

pytestmark = pytest.mark.usefixtures("stub_agents", "no_outreach")

class FakeBluesky:
    """Answers like a Bluesky PDS: createSession with the right app password, then createRecord."""
    def __init__(self):
        self.posts, self.refuse, self.limited = [], set(), False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "bsky.social"
        body = json.loads(request.content)
        if request.url.path == "/xrpc/com.atproto.server.createSession":
            if body != {"identifier": "loopdesk.bsky.social", "password": "app-pass-test"}:
                return httpx.Response(401, json={"error": "AuthenticationRequired",
                                                 "message": "Invalid identifier or password"})
            return httpx.Response(200, json={"did": "did:plc:desk", "handle": "loopdesk.bsky.social",
                                             "accessJwt": "jwt"})
        if request.url.path == "/xrpc/com.atproto.repo.createRecord":
            assert request.headers["authorization"] == "Bearer jwt"
            if self.limited:
                return httpx.Response(429, json={"error": "RateLimitExceeded", "message": "slow down"})
            if any(t in body["record"]["text"] for t in self.refuse):
                return httpx.Response(400, json={"error": "InvalidRequest", "message": "Record/text too long"})
            assert body["repo"] == "did:plc:desk" and body["collection"] == "app.bsky.feed.post"
            self.posts.append(body["record"])
            return httpx.Response(200, json={"uri": f"at://did:plc:desk/app.bsky.feed.post/rk{len(self.posts)}",
                                             "cid": "c"})
        return httpx.Response(404, json={"error": "MethodNotImplemented"})

@pytest.fixture
def no_outreach(monkeypatch):
    monkeypatch.setattr(agents, "interview", stubs.no_interview)

@pytest.fixture
def sky(monkeypatch):
    fake = FakeBluesky()
    monkeypatch.setattr(bluesky, "TRANSPORT", httpx.MockTransport(fake))
    monkeypatch.setenv("BLUESKY_HANDLE", "@loopdesk.bsky.social")
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "app-pass-test")
    return fake

def on_bluesky(slug):
    post = store.get_mission(slug).channels.get("bluesky")
    return post.where if post else None

def backlog(*slugs):
    """Articles on the site and in the feed from before Bluesky was switched on, oldest first."""
    for i, slug in enumerate(slugs):
        store.save_mission(Mission(slug=slug, topic=slug, state=State.DONE, published_at=T0 - timedelta(days=9 - i)))
        m = store.get_mission(slug)
        brief, article = stubs.research(m), stubs.write(m, stubs.research(m), [])
        store.save_artifact(slug, "brief", brief)
        store.save_artifact(slug, "article", article)
        publisher.write_page(m, brief, article, m.published_at)

def entry(slug):
    return json.loads(Path(f"site/{slug}/meta.json").read_text())

def test_off_without_credentials(monkeypatch):
    published("a", T0)                                 # the no-network transport would fail the test
    assert on_bluesky("a") is None and not agents.bluesky_enabled()
    monkeypatch.setenv("BLUESKY_HANDLE", "x.bsky.social")
    assert not bluesky.enabled()                       # a handle alone is not enough
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "p")
    assert bluesky.enabled() and bluesky in agents.CHANNELS

def test_article_is_announced_in_the_same_pass_with_title_and_dek(sky):
    published("a", T0)
    assert on_bluesky("a") == "https://bsky.app/profile/loopdesk.bsky.social/post/rk1"
    e, record = entry("a"), sky.posts[0]
    assert record["text"].startswith(e["title"] + "\n\n") and len(record["text"]) <= 300
    assert record["createdAt"] == "2026-11-22T09:00:00Z" and "embed" not in record   # no site URL, no 1F916
    assert any("posted to Bluesky" in (ev["note"] or "") for ev in store.events("a"))

def test_link_card_points_at_the_site_when_its_url_is_set(sky, monkeypatch):
    monkeypatch.setattr(settings, "SITE_URL", "https://example.com/news/")
    published("a", T0)
    card = sky.posts[0]["embed"]
    assert card["$type"] == "app.bsky.embed.external" and card["external"]["uri"] == "https://example.com/news/a/"

def test_without_a_site_url_the_link_is_the_fresh_1f916_post(sky, monkeypatch):
    monkeypatch.setattr(f916, "TRANSPORT", httpx.MockTransport(FakeSite()))
    Path("data").mkdir(exist_ok=True)
    f916.KEY_FILE.write_text(json.dumps({"handle": "loop-desk", "secret": "1f916_sk_test"}))
    published("a", T0)
    assert sky.posts[0]["embed"]["external"]["uri"] == "https://1f916.ai/api/post/101"

def test_one_post_per_gap_newest_first(sky):
    backlog("oldest", "middle", "newest")
    run_at(T0)
    assert [on_bluesky(s) is not None for s in ("newest", "middle", "oldest")] == [True, False, False]
    run_at(T0 + timedelta(minutes=5))                  # inside the gap: nothing, and no request made
    assert len(sky.posts) == 1
    published("fresh", T0 + timedelta(minutes=8))      # a new article jumps the backlog
    assert len(sky.posts) == 1                         # still inside the gap
    run_at(T0 + timedelta(minutes=15))
    run_at(T0 + timedelta(minutes=30))
    run_at(T0 + timedelta(minutes=45))
    order = sorted(["newest", "middle", "oldest", "fresh"], key=lambda s: on_bluesky(s) or "~")
    assert order == ["newest", "fresh", "middle", "oldest"]
    assert orchestrator.queue(bluesky) == [] and len(sky.posts) == 4

def test_labels_survive_the_short_post():
    text = bluesky.post_text({"slug": "s", "title": "Who runs the port?",
                              "dek": "It is possible that the authority answers to the shippers. " * 20,
                              "text": "dek\n\n[Failed automated checks: see the note at the end.]\n\n..."})
    assert len(text) <= 300 and text.endswith("…")
    assert "[Failed automated checks] [Possibility, not verified] It is possible that" in text
    plain = bluesky.post_text({"slug": "s", "title": "T", "dek": "A plain dek.", "text": "A plain dek."})
    assert plain == "T\n\nA plain dek."

def test_refused_article_goes_to_the_back_and_the_next_one_posts(sky):
    backlog("older", "newer")
    sky.refuse.add(entry("newer")["title"])
    run_at(T0)
    assert on_bluesky("newer") is None and on_bluesky("older")
    assert "too long" in store.get_mission("newer").channel_errors["bluesky"]
    assert [e["slug"] for e in orchestrator.queue(bluesky)] == ["newer"]

def test_rate_limit_and_bad_login_lose_nothing(sky, monkeypatch):
    sky.limited = True
    published("a", T0)
    assert on_bluesky("a") is None and "RateLimitExceeded" in bluesky.load_state()["note"]
    assert "bluesky" not in store.get_mission("a").channel_errors
    sky.limited = False
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "wrong")
    run_at(T0 + timedelta(minutes=15))
    assert on_bluesky("a") is None and "BadLogin" in bluesky.load_state()["note"]
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "app-pass-test")
    run_at(T0 + timedelta(minutes=30))
    assert on_bluesky("a") and bluesky.load_state()["note"] == ""

def test_check_bluesky_logs_in_without_posting(sky, capsys, monkeypatch):
    cli.check_bluesky()
    assert "loopdesk.bsky.social" in capsys.readouterr().out and sky.posts == []
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "wrong")
    with pytest.raises(Exception):
        cli.check_bluesky()
    assert "AuthenticationRequired" in capsys.readouterr().out

def test_ui_and_status_show_the_channel(sky, capsys):
    backlog("waiting")
    page = TestClient(ui.app).get("/published").text
    assert "Bluesky: posting as <strong>loopdesk.bsky.social</strong>" in page and "1 queued" in page
    assert "<strong>Bluesky</strong>: waiting, #1 of 1 in the queue" in page
    assert "<strong>Bluesky</strong>: waiting" in TestClient(ui.app).get("/m/waiting").text
    cli.status()
    assert "Bluesky as loopdesk.bsky.social: 1 queued" in capsys.readouterr().out
    run_at(T0)
    assert "bsky.app/profile/loopdesk.bsky.social/post/rk1" in TestClient(ui.app).get("/m/waiting").text

def test_ui_says_how_to_turn_it_on_when_off():
    published("a", T0)
    client = TestClient(ui.app)
    assert "No channels yet" in client.get("/published").text
    html = client.get("/settings").text                # Add a channel: Bluesky
    assert "<strong>Bluesky</strong> · built in" in html and 'name="app_password"' in html

@pytest.fixture
def sky_server(monkeypatch):
    """A fake Bluesky with nothing in .env: the only way on is the Settings page."""
    fake = FakeBluesky()
    monkeypatch.setattr(bluesky, "TRANSPORT", httpx.MockTransport(fake))
    return fake

def test_settings_page_turns_bluesky_on_after_checking_the_login(sky_server):
    client = TestClient(ui.app)
    assert 'action="/settings/bluesky"' in client.get("/settings").text and not agents.bluesky_enabled()
    r = client.post("/settings/bluesky", data={"handle": "@loopdesk.bsky.social", "app_password": "app-pass-test"},
                    follow_redirects=False)
    assert r.headers["location"] == "/settings?done=bluesky_on"
    assert agents.bluesky_enabled() and oct(bluesky.CRED_FILE.stat().st_mode & 0o777) == "0o600"
    assert "posting as <strong>loopdesk.bsky.social</strong>" in client.get("/settings").text
    published("a", T0)
    assert on_bluesky("a") and len(sky_server.posts) == 1   # posted with the saved login
    client.post("/settings/bluesky", data={"forget": "1"})
    assert not agents.bluesky_enabled() and not bluesky.CRED_FILE.exists()

def test_a_login_bluesky_refuses_is_never_saved(sky_server):
    html = TestClient(ui.app).post("/settings/bluesky", data={"handle": "loopdesk.bsky.social",
                                                              "app_password": "wrong"}).text
    assert "Bluesky did not accept that login" in html and "Invalid identifier or password" in html
    assert not bluesky.CRED_FILE.exists() and "wrong" not in html

def test_bluesky_settings_refuse_cross_site_posts(sky_server):
    r = TestClient(ui.app).post("/settings/bluesky", headers={"origin": "https://evil.example"},
                                data={"handle": "loopdesk.bsky.social", "app_password": "app-pass-test"})
    assert r.status_code == 403 and not bluesky.CRED_FILE.exists()

def test_bluesky_links_use_the_site_address_saved_in_settings(sky_server):
    settings.set_site_url("https://example.com/news/")
    assert bluesky.article_link({"slug": "a"}) == "https://example.com/news/a/"
