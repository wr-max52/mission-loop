"""The Settings page: the site address the feeds use, and joining 1F916 from the UI (against a fake server)."""
import json
from datetime import datetime, timezone
from pathlib import Path
import pytest
from starlette.testclient import TestClient
import orchestrator, settings, store, ui
from agents import f916
from contracts import Mission, State
from test_channels import site, joined, no_outreach   # noqa: F401  (fixtures: a fake 1f916.ai)

pytestmark = pytest.mark.usefixtures("stub_agents", "no_outreach")
T0 = datetime(2026, 11, 22, 9, 0, tzinfo=timezone.utc)

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(orchestrator, "LOCK_FILE", Path("data/loop.lock"))
    monkeypatch.setattr(orchestrator, "WAKE_FLAG", Path("data/WAKE"))
    return TestClient(ui.app)

def published(slug="a"):
    store.save_mission(Mission(slug=slug, topic=slug, approved=True, reviewed=True))
    orchestrator.run_until_idle(lambda: T0)
    assert store.get_mission(slug).state == State.DONE

def feed_url():
    return json.loads(Path("site/feed.json").read_text())["items"][0]["url"]

def test_fresh_install_needs_no_settings(client):
    published()
    assert 'href="/settings"' in client.get("/").text
    html = client.get("/settings").text
    assert 'href="/site/feed.xml"' in html and "relative to the feed file" in html and "1 article" in html
    assert 'action="/settings/join-1f916"' in html and feed_url() == "a/"
    assert not settings.SETTINGS_FILE.exists()

def test_saving_the_site_address_rebuilds_the_feeds_now(client):
    published()
    r = client.post("/settings/site-url", data={"site_url": " https://example.com/news "}, follow_redirects=False)
    assert r.headers["location"] == "/settings?done=site_url_saved"
    assert feed_url() == "https://example.com/news/a/"
    assert json.loads(settings.SETTINGS_FILE.read_text()) == {"site_url": "https://example.com/news"}
    assert "https://example.com/news/feed.json" in client.get("/settings").text  # where it will be hosted
    client.post("/settings/site-url", data={"site_url": ""})
    assert feed_url() == "a/" and settings.site_url() == ""

def test_bad_address_is_refused_and_nothing_changes(client):
    for bad in ("example.com", "javascript:alert(1)", "https://exa mple.com"):
        r = client.post("/settings/site-url", data={"site_url": bad}, follow_redirects=False)
        assert r.headers["location"] == "/settings?error=site_url"
    assert settings.site_url() == ""

def test_env_address_works_and_the_saved_one_wins(client, monkeypatch):
    monkeypatch.setattr(settings, "SITE_URL", "https://env.example")
    published()
    assert feed_url() == "https://env.example/a/"
    assert "from MISSION_LOOP_SITE_URL in .env" in client.get("/settings").text
    client.post("/settings/site-url", data={"site_url": "https://ui.example/"})
    assert feed_url() == "https://ui.example/a/"

def test_join_from_the_ui_saves_the_key_like_the_cli(client, site):
    r = client.post("/settings/join-1f916", data={"handle": "loop-desk"}, follow_redirects=False)
    assert r.headers["location"] == "/settings?done=joined"
    assert f916.key()["secret"] == "1f916_sk_test"
    assert oct(f916.KEY_FILE.stat().st_mode & 0o777) == "0o600"
    html = client.get("/settings").text
    assert "posting as <strong>loop-desk</strong>" in html and "join-1f916" not in html

def test_join_failures_are_shown_and_bad_handles_never_reach_the_site(client, joined):
    r = client.post("/settings/join-1f916", data={"handle": "a"}, follow_redirects=False)
    assert r.headers["location"] == "/settings?error=handle"
    f916.KEY_FILE.unlink()
    f916.KEY_FILE.write_text("{}")                     # a broken key file: register refuses to overwrite it
    html = client.post("/settings/join-1f916", data={"handle": "second"}).text
    assert "1F916 did not join" in html and "already exists" in html

def test_settings_forms_refuse_cross_site_posts(client):
    evil = {"origin": "https://evil.example"}
    assert client.post("/settings/site-url", data={"site_url": "https://x.example"}, headers=evil).status_code == 403
    assert client.post("/settings/join-1f916", data={"handle": "loop-desk"}, headers=evil).status_code == 403
    assert settings.site_url() == "" and not f916.KEY_FILE.exists()

def test_animations_toggle_is_on_by_default_and_hides_the_researchers_when_off(client, monkeypatch):
    store.save_mission(Mission(slug="b", topic="Outages", state=State.RESEARCHING))
    assert settings.animations() and 'name="animations" value="on" checked' in client.get("/settings").text
    assert "researchers.js" in client.get("/").text and 'data-lab="idle"' in client.get("/").text
    r = client.post("/settings/animations", data={}, follow_redirects=False)   # unticked: the box sends nothing
    assert r.status_code == 303 and "animations_off" in r.headers["location"]
    assert not settings.animations() and json.loads(settings.SETTINGS_FILE.read_text()) == {"animations": "off"}
    monkeypatch.setattr(ui, "loop_running", lambda: True)
    import activity
    activity.begin("b", "sources")
    html = client.get("/").text
    assert "researchers.js" not in html and 'data-lab' not in html.split("</style>")[1]
    assert 'value="on" checked' not in client.get("/settings").text
    client.post("/settings/animations", data={"animations": "on"})
    assert settings.animations() and settings.saved() == {}              # back on: nothing stored
    assert '<div class="agents" data-lab>' in client.get("/").text
    evil = {"origin": "https://evil.example"}
    assert client.post("/settings/animations", data={}, headers=evil).status_code == 403 and settings.animations()
