from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from starlette.testclient import TestClient
import orchestrator, store, ui
from contracts import Mission, State

pytestmark = pytest.mark.usefixtures("stub_agents")
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(orchestrator, "LOCK_FILE", Path("data/loop.lock"))
    monkeypatch.setattr(orchestrator, "WAKE_FLAG", Path("data/WAKE"))
    return TestClient(ui.app)

def run_at(t): orchestrator.run_until_idle(lambda: t)

def brief_ready(slug="b", topic="Outages"):
    store.save_mission(Mission(slug=slug, topic=topic))
    run_at(T0)
    assert store.get_mission(slug).state == State.BRIEF_READY

def article_ready(slug="a"):
    brief_ready(slug)
    m = store.get_mission(slug); m.approved = True; store.save_mission(m)
    run_at(T0); run_at(T0 + timedelta(hours=48))
    assert store.get_mission(slug).state == State.ARTICLE_READY

def test_dashboard_groups_missions(client):
    brief_ready("b", "Brief topic")
    store.save_mission(Mission(slug="q", topic="Queued topic"))
    html = client.get("/").text
    assert "Needs you (1)" in html and "Brief topic" in html
    assert "In progress (1)" in html and "Queued topic" in html
    assert "Loop is off" in html

def test_mission_page_shows_brief_and_actions(client):
    brief_ready()
    html = client.get("/m/b").text
    assert "Research brief" in html and "Stub thesis" in html
    assert 'action="/m/b/approve"' in html and 'action="/m/b/reject"' in html

def test_approve_then_loop_writes(client):
    brief_ready()
    r = client.post("/m/b/approve", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/m/b?done=approved"
    assert store.get_mission("b").approved
    run_at(T0); run_at(T0 + timedelta(hours=48))
    assert store.get_mission("b").state == State.ARTICLE_READY

def test_approve_twice_is_refused(client):
    brief_ready()
    client.post("/m/b/approve")
    r = client.post("/m/b/approve", follow_redirects=False)
    assert "error=state" in r.headers["location"]

def test_reject_needs_notes_and_sends_back(client):
    brief_ready()
    assert "error=notes" in client.post("/m/b/reject", data={"notes": " "}, follow_redirects=False).headers["location"]
    client.post("/m/b/reject", data={"notes": "find the FAA report"})
    m = store.get_mission("b")
    assert m.state == State.RESEARCHING and "find the FAA report" in m.validator_notes

def test_article_page_and_publish(client):
    article_ready()
    html = client.get("/m/a").text
    assert '<iframe class="article" src="/m/a/preview"' in html and 'action="/m/a/publish"' in html
    assert "Stub thesis" in client.get("/m/a/preview").text
    client.post("/m/a/publish")
    run_at(T0 + timedelta(hours=49))
    assert store.get_mission("a").state == State.DONE
    assert client.get("/site/a/").status_code == 200

def test_requeue_parked(client):
    store.save_mission(Mission(slug="p", topic="FAIL"))
    run_at(T0)
    assert "Requeue" in client.get("/m/p").text
    client.post("/m/p/requeue")
    assert store.get_mission("p").state == State.QUEUED

def test_add_mission_makes_slug_from_topic(client):
    r = client.post("/missions", data={"topic": "Who funds the transit levy?", "objective": "o"}, follow_redirects=False)
    assert r.headers["location"] == "/m/who-funds-the-transit-levy?done=added"
    m = store.get_mission("who-funds-the-transit-levy")
    assert m.state == State.QUEUED and m.objective == "o"

def test_add_mission_validation(client):
    assert "error=topic" in client.post("/missions", data={"topic": ""}, follow_redirects=False).headers["location"]
    assert "error=slug" in client.post("/missions", data={"topic": "x", "slug": "Bad Slug!"},
                                       follow_redirects=False).headers["location"]
    client.post("/missions", data={"topic": "x", "slug": "dup"})
    assert "error=exists" in client.post("/missions", data={"topic": "y", "slug": "dup"},
                                         follow_redirects=False).headers["location"]

def test_cross_site_post_refused(client):
    brief_ready()
    r = client.post("/m/b/approve", headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and not store.get_mission("b").approved

def test_loop_start_stop_wake(client, monkeypatch):
    started = []
    monkeypatch.setattr(ui, "start_loop", lambda every: started.append(every))
    client.post("/loop/start", data={"every": "7", "back": "/"})
    assert started == [7.0]
    client.post("/loop/stop", data={"back": "/"})
    assert orchestrator.STOP_FLAG.exists()
    client.post("/loop/wake", data={"back": "//evil.example"}, follow_redirects=False)
    assert orchestrator.WAKE_FLAG.exists()

def test_loop_running_detects_the_lock(client):
    import fcntl
    assert not ui.loop_running()
    with orchestrator.LOCK_FILE.open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert ui.loop_running()
    assert not ui.loop_running()

def test_unknown_mission(client):
    assert "No such mission" in client.get("/m/nope").text
    assert client.post("/m/nope/approve").status_code == 404
    assert client.get("/m/nope/preview").status_code == 404
