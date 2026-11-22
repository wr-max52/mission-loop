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

def test_about_one_post_a_pass_newest_first(sky):
    backlog("oldest", "middle", "newest")
    run_at(T0)
    assert [on_bluesky(s) is not None for s in ("newest", "middle", "oldest")] == [True, False, False]
    run_at(T0 + timedelta(minutes=5))                  # inside the gap: nothing, and no request made
    assert len(sky.posts) == 1
    published("fresh", T0 + timedelta(minutes=15))     # a new article jumps the backlog
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
    page = TestClient(ui.app).get("/").text
    assert "Bluesky: posting as <strong>loopdesk.bsky.social</strong>" in page and "1 queued" in page
    assert "Bluesky: waiting" in TestClient(ui.app).get("/m/waiting").text
    cli.status()
    assert "Bluesky as loopdesk.bsky.social: 1 queued" in capsys.readouterr().out
    run_at(T0)
    assert "bsky.app/profile/loopdesk.bsky.social/post/rk1" in TestClient(ui.app).get("/m/waiting").text

def test_ui_says_how_to_turn_it_on_when_off():
    published("a", T0)
    assert "check-bluesky" in TestClient(ui.app).get("/").text
