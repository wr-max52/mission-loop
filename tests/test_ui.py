from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from starlette.testclient import TestClient
import activity, agents, orchestrator, run as cli, store, ui
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

def test_push_through_parked_brief(client):
    store.save_mission(Mission(slug="p", topic="FAIL"))
    run_at(T0)
    assert 'action="/m/p/push"' in client.get("/m/p").text
    r = client.post("/m/p/push", follow_redirects=False)
    assert r.headers["location"] == "/m/p?done=pushed"
    html = client.get("/m/p").text
    assert store.get_mission("p").state == State.BRIEF_READY
    assert "failing the source check" in html and "stub: forced failure" in html
    assert "failed the source check)" in html and 'action="/m/p/approve"' in html
    assert "error=state" in client.post("/m/p/push", follow_redirects=False).headers["location"]

def test_push_through_parked_article(client):
    store.save_mission(Mission(slug="w", topic="BADARTICLE", approved=True))
    run_at(T0); run_at(T0 + timedelta(hours=48)); run_at(T0 + timedelta(hours=48))
    assert "Last article that failed its check" in client.get("/m/w").text
    client.post("/m/w/push")
    assert store.get_mission("w").state == State.ARTICLE_READY
    assert 'action="/m/w/publish"' in client.get("/m/w").text

def test_no_push_button_without_a_draft(client, monkeypatch):
    def boom(*args): raise RuntimeError("model down")
    monkeypatch.setattr(agents, "gather", boom)
    store.save_mission(Mission(slug="c", topic="x"))
    run_at(T0)
    html = client.get("/m/c").text
    assert 'action="/m/c/push"' not in html and "no attempt left a finished brief" in html
    assert "error=state" in client.post("/m/c/push", follow_redirects=False).headers["location"]

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

def test_loop_records_the_working_agent_then_clears_it(monkeypatch):
    seen, gather, write_brief, validate = [], agents.gather, agents.write_brief, agents.validate
    def spy(fn):
        def wrapped(*args):
            seen.append(activity.current()["agent"])
            return fn(*args)
        return wrapped
    monkeypatch.setattr(agents, "gather", spy(gather))
    monkeypatch.setattr(agents, "write_brief", spy(write_brief))
    monkeypatch.setattr(agents, "validate", spy(validate))
    store.save_mission(Mission(slug="b", topic="Outages"))
    run_at(T0)
    assert seen == ["sources", "brief", "source_check"]
    assert activity.current() is None

def test_crashed_agent_is_not_left_showing(monkeypatch):
    def boom(*args): raise RuntimeError("model down")
    monkeypatch.setattr(agents, "gather", boom)
    store.save_mission(Mission(slug="b", topic="Outages"))
    run_at(T0)
    assert activity.current() is None

def test_new_loop_clears_a_killed_loops_record():
    activity.begin("b", "writer")
    held = cli._lock()
    assert activity.current() is None
    held.close()

def test_dashboard_highlights_the_working_agent(client, monkeypatch):
    monkeypatch.setattr(ui, "loop_running", lambda: True)
    store.save_mission(Mission(slug="b", topic="Outages", state=State.RESEARCHING))
    store.save_mission(Mission(slug="q", topic="Other topic"))
    assert "No agent is working right now" in client.get("/").text
    before = ui.signature()
    activity.begin("b", "brief")
    html = client.get("/").text
    assert '<span class="agent on" aria-current="step">Brief writer</span>' in html
    assert html.count('class="agent on"') == 1 and html.count('class="card active"') == 1
    assert "Brief writer is working on this now." in html
    assert "Contact finder" not in html            # outreach is off
    assert ui.signature() != before                # open pages refresh when the agent changes
    assert "Brief writer is working on this now." in client.get("/m/b").text
    assert "working on this now" not in client.get("/m/q").text

def test_stale_record_is_ignored_when_the_loop_is_off(client):
    store.save_mission(Mission(slug="b", topic="Outages", state=State.RESEARCHING))
    activity.begin("b", "writer")
    html = client.get("/").text
    assert 'class="agent' not in html and "card active" not in html

def test_mission_page_drops_loop_panel_and_nav_shows_loop_state(client, monkeypatch):
    store.save_mission(Mission(slug="b", topic="Outages"))
    assert '<div class="loop">' in client.get("/").text
    html = client.get("/m/b").text
    assert '<div class="loop">' not in html
    assert "</span>Loop off</span>" in html and "</span>Running</span>" not in html
    monkeypatch.setattr(ui, "loop_running", lambda: True)
    assert '<span class="dot on" aria-hidden="true"></span>Running</span>' in client.get("/m/b").text

def test_status_colors_on_cards_and_pills(client):
    for slug, state, color in (("r", State.BRIEF_READY, "review"), ("w", State.ARTICLE_READY, "publish"),
                               ("p", State.PARKED, "parked")):
        store.save_mission(Mission(slug=slug, topic="T", state=state))
        assert f'<a class="card {color}" href="/m/{slug}">' in client.get("/").text
        assert f'<span class="badge {color}">' in client.get(f"/m/{slug}").text

def test_unknown_mission(client):
    assert "No such mission" in client.get("/m/nope").text
    assert client.post("/m/nope/approve").status_code == 404
    assert client.get("/m/nope/preview").status_code == 404

def test_theme_toggle_is_on_every_ui_page(client):
    brief_ready()
    for route in ("/", "/m/b"):
        html = client.get(route).text
        assert 'class="theme-toggle"' in html           # the button, in the nav
        assert "window.toggleTheme" in html              # the no-flash init script
# The dark theme wins over the system fallback: an explicit choice sets data-theme, and the
# system palette is gated behind :not([data-theme]) so a saved "dark" beats an OS light.
def test_system_dark_is_gated_behind_an_explicit_choice():
    from render import CSS
    assert ':root[data-theme="dark"]' in CSS and "--bg:#151517" in CSS
    assert "@media (prefers-color-scheme: dark) {" in CSS and ":root:not([data-theme])" in CSS

def test_pixel_researchers_wander_in_progress_cards_and_the_working_pill(client, monkeypatch):
    store.save_mission(Mission(slug="b", topic="Outages", state=State.RESEARCHING))
    store.save_mission(Mission(slug="q", topic="Other topic", state=State.WRITING))
    store.save_mission(Mission(slug="r", topic="Ready", state=State.BRIEF_READY))
    html = client.get("/").text
    assert '<script src="/assets/researchers.js" defer></script>' in html
    assert '<a class="card" href="/m/b" data-lab="idle" data-worker="assistant">' in html   # loop off: they nap
    assert 'href="/m/r" data-lab' not in html                             # only in-progress cards get one
    assert "data-lab>" not in html                                        # no agent working, no pill stage
    monkeypatch.setattr(ui, "loop_running", lambda: True)
    activity.begin("b", "sources")
    html = client.get("/").text
    assert '<a class="card active" href="/m/b" data-lab="busy" data-worker="assistant">' in html
    assert '<a class="card" href="/m/q" data-lab="waiting" data-worker="pi">' in html   # writing: tenured visitors
    assert '<div class="agents" data-lab>' in html
    js = client.get("/assets/researchers.js")
    assert js.status_code == 200 and "class Researcher" in js.text
    assert "researchers.js" not in client.get("/m/b").text                 # the mission page stays quiet
