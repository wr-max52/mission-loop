"""Auto mode: the top bar's switch approves every brief and proposed epic, and never publishes."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import pytest
from starlette.testclient import TestClient
import notify, orchestrator, render, run as cli, settings, status_tools, store, ui
from contracts import Mission, State

pytestmark = pytest.mark.usefixtures("stub_agents")
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(orchestrator, "LOCK_FILE", Path("data/loop.lock"))
    monkeypatch.setattr(orchestrator, "WAKE_FLAG", Path("data/WAKE"))
    return TestClient(ui.app)

def run_at(t): orchestrator.run_until_idle(lambda: t)

def proposed(slug="m1"):
    epic = store.create_epic("Swap Lines", status="proposed")
    store.save_mission(Mission(slug=slug, topic="Swap lines", epic=epic.slug, epic_fallback=""))
    return epic

def track(html: str) -> str:
    return html.split('class="flow-track"')[1].split("</section>")[0]

def test_off_by_default_and_nothing_is_stored():
    assert not settings.auto_mode() and orchestrator.auto_approve(T0) == 0
    settings.set_auto_mode(True)
    assert settings.auto_mode() and settings.saved() == {"auto_mode": "on"}
    settings.set_auto_mode(False)
    assert settings.saved() == {}

def test_the_loop_approves_briefs_and_epics_but_never_publishes():
    settings.set_auto_mode(True)
    epic = proposed()
    store.save_mission(Mission(slug="b", topic="Outages"))
    run_at(T0); run_at(T0 + timedelta(hours=48))
    m = store.get_mission("b")
    assert m.approved and m.state == State.ARTICLE_READY and not m.reviewed   # stops at your review
    assert store.get_epic(epic.slug).status == "active"
    notes = [e["note"] for e in store.events("b")]
    assert "brief approved by auto mode" in notes
    assert any(n == "new epic approved by auto mode: Swap Lines" for n in (e["note"] for e in store.events("m1")))
    run_at(T0 + timedelta(hours=49))
    assert store.get_mission("b").state == State.ARTICLE_READY                 # still waiting on a human

def test_off_the_loop_still_waits_for_you():
    store.save_mission(Mission(slug="b", topic="Outages"))
    epic = proposed()
    run_at(T0); run_at(T0 + timedelta(hours=48))
    assert store.get_mission("b").state == State.BRIEF_READY and not store.get_mission("b").approved
    assert store.get_epic(epic.slug).status == "proposed"

def test_an_epic_a_worker_is_busy_with_waits_for_a_later_sweep():
    settings.set_auto_mode(True)
    epic = proposed("busy")
    orchestrator.auto_approve(T0, busy={"busy"})
    assert store.get_epic(epic.slug).status == "proposed"
    orchestrator.auto_approve(T0)
    assert store.get_epic(epic.slug).status == "active"

def test_a_pushed_through_brief_is_approved_but_an_article_is_not():
    store.save_mission(Mission(slug="p", topic="FAIL"))   # both park first, with auto mode off
    store.save_mission(Mission(slug="w", topic="BADARTICLE", approved=True))
    run_at(T0); run_at(T0 + timedelta(hours=48)); run_at(T0 + timedelta(hours=48))
    assert store.get_mission("p").state == store.get_mission("w").state == State.PARKED
    settings.set_auto_mode(True)
    for slug in ("p", "w"):
        orchestrator.push_through(store.get_mission(slug), T0)
    assert store.get_mission("p").approved and store.get_mission("p").unchecked_brief
    assert store.get_mission("w").state == State.ARTICLE_READY and not store.get_mission("w").reviewed

def test_no_brief_notification_in_auto_mode():
    m = Mission(slug="b", topic="T", state=State.BRIEF_READY)
    assert notify.needs_attention(m)
    settings.set_auto_mode(True)
    assert notify.needs_attention(m) is None
    assert notify.needs_attention(Mission(slug="a", topic="T", state=State.ARTICLE_READY))   # articles still ask

def test_switch_is_in_the_top_bar_of_every_page(client):
    store.save_mission(Mission(slug="b", topic="T"))
    for route in ("/", "/published", "/settings", "/m/b"):
        html = client.get(route).text
        nav = html.split('<nav class="top">')[1].split("</nav>")[0]
        assert 'action="/auto"' in nav and 'name="auto" value="on"' in nav and " checked" not in nav

def test_turning_it_on_approves_what_waits_and_goes_back(client):
    store.save_mission(Mission(slug="b", topic="T", state=State.BRIEF_READY))
    store.save_mission(Mission(slug="a", topic="T", state=State.ARTICLE_READY))
    epic = proposed()
    r = client.post("/auto", data={"auto": "on", "back": "/m/b"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/m/b?done=auto_on"
    assert settings.auto_mode() and store.get_mission("b").approved
    assert not store.get_mission("a").reviewed and store.get_epic(epic.slug).status == "active"
    html = client.get("/").text
    assert 'name="auto" value="on" checked' in html and "Nothing is waiting on you" not in html   # the article is
    r = client.post("/auto", data={"back": "//evil.example"}, follow_redirects=False)   # unticked: nothing sent
    assert r.headers["location"] == "/?done=auto_off" and not settings.auto_mode()

def test_cross_site_switch_is_refused(client):
    r = client.post("/auto", data={"auto": "on"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and not settings.auto_mode()

def steps(html: str) -> list[tuple[str, str]]:
    """(key, kind) for every step on the strip, in order: agent, check, human or auto."""
    kinds = {"step": "agent", "check": "check", "human review": "human", "human propose": "human",
             "human publish": "human", "human auto": "auto"}
    return [(key, kinds[re.sub(r" (on|waiting)$", "", cls)])
            for cls, key in re.findall(r'class="([a-z ]+)"(?: href="[^"]*")? data-step="([a-z0-9_]+)"', track(html))]

def test_every_code_check_and_human_review_is_on_the_flow_chart(client, monkeypatch):
    """Each agent whose output code checks has a check right after it, and each human decision sits after the
    check that leads to it: the epics from the Theorist and the finder, the brief, the article."""
    proposed()
    assert steps(client.get("/").text) == [
        ("theorist", "agent"), ("theorist_check", "check"), ("finder", "agent"), ("finder_check", "check"),
        ("epic_review", "human"), ("sources", "agent"), ("brief", "agent"), ("source_check", "check"),
        ("brief_review", "human"), ("writer", "agent"), ("article_check", "check"), ("article_review", "human"),
        ("publisher", "agent")]
    html = track(client.get("/").text)
    assert '<a class="human propose waiting" href="/#needs-you" data-step="epic_review"' in html
    assert "Failures are dropped." in html               # the desk's checks park nothing
    assert client.get("/api/flow").json()["waiting"]["epic_review"] == 1
    monkeypatch.setattr(settings, "OUTREACH", True)                              # contacts get their own check
    keys = [k for k, _ in steps(client.get("/").text)]
    assert keys[keys.index("sources"):keys.index("source_check")] == ["sources", "contacts", "contact_check", "brief"]

def test_the_flow_chart_snakes_with_a_review_at_every_turn(client):
    """Lane 0 runs left to right from the top left to the first review, a square end on the right that spans the
    two lanes it joins; lane 1 comes back right to left one level lower to the next review on the left; and so on."""
    html = track(client.get("/").text)
    lanes = re.findall(r'<span class="lane( rtl)?" style="grid-row:(\d); grid-column:([\d /]+)">'
                       r'(.*?)(?=<span class="lane|<a class="human|<span class="human|</div>)', html)
    assert [(d == " rtl", int(r), c) for d, r, c, _ in lanes] == [
        (False, 1, "1 / 3"),               # starts in the top left corner: no review there
        (True, 2, "2 / 3"), (False, 3, "2 / 3"),
        (True, 4, "1 / 3")]                # the last lane, Publish alone, reaches the bottom left corner
    firsts = [re.findall(r'data-step="([a-z_]+)"', body) for *_, body in lanes]
    assert firsts[0][0] == "theorist" and firsts[1][0] == "sources" and firsts[2][0] == "writer" and firsts[3] == ["publisher"]
    ends = re.findall(r'data-step="([a-z_]+)" title="[^"]*" style="grid-row:(\d) / span (\d); grid-column:(\d)"', html)
    assert ends == [("epic_review", "1", "2", "3"), ("brief_review", "2", "2", "1"), ("article_review", "3", "2", "3")]

def test_auto_mode_turns_exactly_the_two_reviews_into_approvals(client):
    proposed()
    before = steps(client.get("/").text)
    shape = re.search(r'data-shape="([^"]+)"', client.get("/").text).group(1)
    client.post("/auto", data={"auto": "on"})
    page = client.get("/").text
    after = steps(page)
    assert [k for k, _ in after] == [k for k, _ in before]                       # nothing moves, nothing goes
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    assert changed == [(("epic_review", "human"), ("epic_review", "auto")),
                       (("brief_review", "human"), ("brief_review", "auto"))]   # every check stays; publishing too
    html = track(page)
    assert "Auto-approve epics" in html and "Auto-approve brief" in html
    assert '<a class="human publish" href="/#needs-you" data-step="article_review"' in html
    assert re.search(r'data-shape="([^"]+)"', page).group(1) != shape           # an open page redraws its strip

def test_signature_changes_with_the_switch(client):
    before = client.get("/api/signature").text
    settings.set_auto_mode(True)
    assert client.get("/api/signature").text != before

def test_cli_and_chat_tool():
    store.save_mission(Mission(slug="b", topic="T", state=State.BRIEF_READY))
    cli.auto("on")
    assert settings.auto_mode() and store.get_mission("b").approved
    assert "Auto mode: on" in status_tools.loop_status()
    cli.auto("off")
    assert not settings.auto_mode() and "Auto mode: off" in status_tools.loop_status()

def test_the_published_about_page_always_shows_the_human_reviews():
    settings.set_auto_mode(True)
    html = render.about_html()
    assert "Auto-approve" not in html and '<span class="human review" data-step="brief_review"' in html
    assert '<span class="human propose" data-step="epic_review"' in html    # no link: the site has no review desk
