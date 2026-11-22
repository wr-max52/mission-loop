"""The Theorist connecting dots: combining open missions into a bigger one, and linking missions (two open ones,
or an open one and a published article) while keeping them separate."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from ollama import Message
from starlette.testclient import TestClient
import orchestrator, related, settings, status_tools, store, ui
from agents import llm, stubs, theorist
from agents.theorist import NewCombine, NewLink, Theory, TheoryReply
from contracts import Combine, Link, Mission, RejectedBrief, State

pytestmark = pytest.mark.usefixtures("stub_agents")
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
LINE = "Who really decides what the Federal Reserve does?"

def run_at(t): orchestrator.run_until_idle(lambda: t)

def published(slug, topic):
    store.save_mission(Mission(slug=slug, topic=topic))
    run_at(T0)
    m = store.get_mission(slug); m.approved = True; store.save_mission(m)
    run_at(T0); run_at(T0 + timedelta(hours=48))
    m = store.get_mission(slug); m.reviewed = True; store.save_mission(m)
    run_at(T0 + timedelta(hours=49))
    assert store.get_mission(slug).state == State.DONE

def desk():
    """Three open missions (one with a brief), one being written, and a published article."""
    store.save_mission(Mission(slug="memo", topic="Who wrote the 2019 rate memo?", objective="Find the author."))
    store.save_mission(Mission(slug="lobby", topic="Did a lobbyist shape the stress tests?", state=State.BRIEF_READY))
    store.save_artifact("lobby", "brief", stubs.research(store.get_mission("lobby")))
    store.save_mission(Mission(slug="dam", topic="Who profits from the new dam permits?"))
    store.save_mission(Mission(slug="writing", topic="Why did the grid fail in July?", state=State.WRITING))
    store.save_mission(Mission(slug="old", topic="The Fed's 2018 audit fight", state=State.DONE, published_at=T0))
    store.save_artifact("old", "brief", stubs.research(store.get_mission("old")))
    store.save_artifact("old", "article", stubs.write(store.get_mission("old"), stubs.research(store.get_mission("old")), []))
    store.set_inspiration(LINE)

def combine(missions=("memo", "lobby"), topic="Did one bank lobbyist write both the memo and the stress tests?"):
    return NewCombine(missions=list(missions), topic=topic, objective="Both point at one lobbyist.", why="Same name.")

def settle(**kw):
    return theorist.settle(TheoryReply(patterns=["p"], inspiration=LINE, **kw), LINE, store.all_missions())

# ---------- what the Theorist sees and may do ----------

async def test_prompt_asks_for_combines_and_links_and_shows_the_connections(monkeypatch):
    desk()
    store.save_link(Link(a="memo", b="old", why="same author"))
    store.save_link(Link(a="dam", b="memo", status="removed"))
    store.save_combine(Combine(slug="x", topic="t", objective="o", members=["dam", "lobby"], status="rejected"))
    seen = []
    async def fake_chat(messages, tools=None, format=None, model=None):
        seen.append(messages[0]["content"])
        return Message(role="assistant", content=TheoryReply(patterns=["p"], inspiration=LINE).model_dump_json())
    monkeypatch.setattr(llm, "chat", fake_chat)
    await theorist.run_theorist(LINE, [], store.all_missions(), T0)
    system = seen[0]
    assert "- combine:" in system and "- links:" in system and "connect the dots" in system
    assert "Links between missions:\n- memo <-> old: same author" in system
    assert "- dam <-> memo (link removed)" in system and "- dam + lobby (combine rejected)" in system

def test_a_combined_mission_stands_in_for_the_ones_it_combines():
    store.save_mission(Mission(slug="big", topic="The bigger question", combines=["a", "b"]))
    store.save_mission(Mission(slug="a", topic="Small one", state=State.COMBINED, combined_into="big"))
    text = theorist.desk_text(store.all_missions())
    assert "- big | queued | The bigger question | combines a, b" in text and "Small one" not in text

def test_combine_kept_with_the_members_shared_epic():
    desk()
    fed = store.create_epic("Fed Capture")
    for s in ("memo", "lobby"):
        m = store.get_mission(s); m.epic = fed.slug; store.save_mission(m)
    theory = settle(combine=combine())
    assert theory.combine.missions == ["memo", "lobby"] and theory.combine.epic == "Fed Capture"
    assert theory.dropped == []

@pytest.mark.parametrize("members, topic, why", [
    (["memo", "old"], None, "only missions not yet being written"),
    (["memo", "writing"], None, "only missions not yet being written"),
    (["memo"], None, "2 to 4 missions"),
    (["memo", "nope"], None, "no such mission"),
    (["memo", "lobby"], "Who profits from the new dam permits?", "already has a mission like it"),
])
def test_combine_rules(members, topic, why):
    desk()
    theory = settle(combine=combine(members, topic or "Did one bank lobbyist write both the memo and the tests?"))
    assert theory.combine is None and why in theory.dropped[0]

def test_a_combine_the_editor_saw_is_not_proposed_again():
    desk()
    store.save_combine(Combine(slug="x", topic="t", objective="o", members=["lobby", "memo"], status="rejected"))
    assert "seen this combine already" in settle(combine=combine()).dropped[0]
    store.save_combine(Combine(slug="y", topic="t", objective="o", members=["memo", "dam"]))
    assert "already waiting" in settle(combine=combine(("lobby", "dam"))).dropped[0]

def test_links_open_mission_first_with_a_reason():
    desk()
    theory = settle(links=[NewLink(mission="old", to="memo", why="Same author."),
                           NewLink(mission="memo", to="dam", why="Same firm.")])
    assert [(k.mission, k.to) for k in theory.links] == [("memo", "old"), ("memo", "dam")]
    assert theory.dropped == []

@pytest.mark.parametrize("a, b, why, expected", [
    ("memo", "memo", "x", "itself"),
    ("memo", "nope", "x", "no such mission"),
    ("old", "old2", "x", "at least one open mission"),
    ("memo", "dam", " ", "needs a reason"),
    ("memo", "lobby", "x", "linked already"),
    ("memo", "writing", "x", "the editor removed this link"),
    ("gone", "memo", "x", "combined into another"),
])
def test_link_rules(a, b, why, expected):
    desk()
    store.save_mission(Mission(slug="old2", topic="Another old one", state=State.DONE))
    store.save_mission(Mission(slug="gone", topic="Folded", state=State.COMBINED, combined_into="memo"))
    store.save_link(Link(a="memo", b="lobby"))
    store.save_link(Link(a="memo", b="writing", status="removed"))
    theory = settle(links=[NewLink(mission=a, to=b, why=why)])
    assert theory.links == [] and expected in theory.dropped[0]

def test_at_most_three_links_and_none_inside_the_combine():
    desk()
    links = [NewLink(mission="memo", to="lobby", why="w"), NewLink(mission="memo", to="dam", why="w"),
             NewLink(mission="dam", to="lobby", why="w"), NewLink(mission="dam", to="old", why="w"),
             NewLink(mission="writing", to="old", why="w"), NewLink(mission="memo", to="old", why="w")]
    theory = settle(combine=combine(), links=links)
    assert len(theory.links) == 3
    assert any("the combine joins them already" in d for d in theory.dropped)
    assert any("at most 3 links" in d for d in theory.dropped)

# ---------- applying it ----------

def test_apply_proposes_the_combine_and_draws_the_links():
    desk()
    theory = Theory(patterns=[], inspiration=LINE, based_on=LINE, combine=combine(),
                    links=[NewLink(mission="dam", to="old", why="Same money.")])
    assert orchestrator.apply_theory(theory, T0) == 2
    c = store.all_combines("proposed")[0]
    assert c.members == ["memo", "lobby"] and c.slug == store.last_theory()["combine"]
    assert store.get_mission("memo").state == State.QUEUED              # nothing stops until the human approves
    assert "the Theorist proposes combining it" in store.events("memo")[-1]["note"]
    link = store.get_link("old", "dam")
    assert link.why == "Same money." and link.by == "theorist" and store.last_theory()["links"] == [["dam", "old"]]
    assert "linked to old by the Theorist: Same money." in store.events("dam")[-1]["note"]

def test_a_combine_whose_missions_moved_on_is_dropped():
    desk()
    m = store.get_mission("lobby"); m.state = State.WRITING; store.save_mission(m)
    orchestrator.apply_theory(Theory(patterns=[], inspiration=LINE, based_on=LINE, combine=combine()), T0)
    assert store.all_combines() == [] and "moved on" in store.last_theory()["notes"][0]

def test_new_links_and_combines_are_news_to_the_theorist():
    desk()
    before = orchestrator.desk_fingerprint()
    orchestrator.link("memo", "dam", "w", T0)
    assert orchestrator.desk_fingerprint() != before

# ---------- the human decides on a combine ----------

def proposed():
    desk()
    orchestrator.link("memo", "dam", "Same firm.", T0)
    orchestrator.link("lobby", "old", "Same lobbyist.", T0)
    orchestrator.apply_theory(Theory(patterns=[], inspiration=LINE, based_on=LINE, combine=combine()), T0)
    return store.all_combines("proposed")[0]

def test_approving_makes_the_bigger_mission_and_stops_the_others():
    c = proposed()
    big = orchestrator.approve_combine(c.slug, T0)
    assert big.state == State.QUEUED and big.combines == ["memo", "lobby"]
    assert "- memo: Who wrote the 2019 rate memo?" in big.objective
    for s in ("memo", "lobby"):
        m = store.get_mission(s)
        assert m.state == State.COMBINED and m.combined_into == big.slug
        assert f"combined into {big.slug}" in store.events(s)[-1]["note"]
    assert {k.other(big.slug) for k in store.links_of(big.slug)} == {"dam", "old"}   # links pass on
    assert store.get_combine(big.slug).status == "approved"
    assert all(m.slug not in ("memo", "lobby") for m in orchestrator.in_flow_order(store.all_missions()))

def test_a_combine_waits_while_a_worker_is_busy_and_drops_once_its_missions_moved_on():
    c = proposed()
    with pytest.raises(ValueError, match="busy"):
        orchestrator.approve_combine(c.slug, T0, busy={"memo"})
    for s in ("memo", "lobby"):
        m = store.get_mission(s); m.state = State.WRITING; store.save_mission(m)
    with pytest.raises(ValueError, match="moved on"):
        orchestrator.approve_combine(c.slug, T0)
    assert store.get_combine(c.slug) is None

def test_rejecting_keeps_them_apart_for_good():
    c = proposed()
    orchestrator.reject_combine(c.slug, T0)
    assert store.get_combine(c.slug).status == "rejected" and store.get_mission("memo").state == State.QUEUED
    assert "seen this combine already" in settle(combine=combine()).dropped[0]

def test_auto_mode_approves_a_combine_unless_a_worker_is_on_it():
    c = proposed()
    settings.set_auto_mode(True)
    orchestrator.auto_approve(T0, busy={"lobby"})
    assert store.get_combine(c.slug).status == "proposed"
    orchestrator.auto_approve(T0)
    assert store.get_mission("memo").state == State.COMBINED
    assert "approved by auto mode" in store.events("memo")[-1]["note"]

def test_the_loop_researches_the_bigger_mission_never_the_combined_ones():
    c = proposed()
    big = orchestrator.approve_combine(c.slug, T0)
    run_at(T0)
    assert store.get_mission(big.slug).state == State.BRIEF_READY
    assert store.get_mission("memo").state == store.get_mission("lobby").state == State.COMBINED

# ---------- what research sees ----------

def test_research_sees_the_combined_missions_and_the_linked_work():
    c = proposed()
    m = store.get_mission("lobby"); m.state, m.rejected_brief = State.PARKED, None; store.save_mission(m)
    big = orchestrator.approve_combine(c.slug, T0)
    block = related.reference_block(big)
    assert "This mission combines earlier missions" in block
    assert "- Who wrote the 2019 rate memo?\n    Its objective: Find the author." in block
    assert "Its thesis so far:" in block                              # lobby's brief
    assert "Open mission: Who profits from the new dam permits?" in block and "Linked because: Same firm." in block
    assert "Published article" in block and "Linked because: Same lobbyist." in block
    assert "old" not in [o.slug for o, _, _ in related.similar_published(big)]   # shown once, as a link

def test_a_parked_members_failed_brief_is_background_too():
    desk()
    m = store.get_mission("dam")
    m.state, m.rejected_brief = State.PARKED, RejectedBrief(brief=stubs.research(m), notes="n")
    store.save_mission(m)
    store.save_mission(Mission(slug="big", topic="Big", combines=["dam"]))
    assert "Its thesis so far:" in related.connections_block(store.get_mission("big"))

def test_nothing_connected_means_no_block():
    assert related.connections_block(Mission(slug="x", topic="Alone")) == ""

# ---------- the site ----------

def test_published_articles_point_at_each_other_once_both_are_out():
    published("first", "The Fed audit fight of 2018")
    store.save_mission(Mission(slug="second", topic="Who blocked the 2025 audit?"))
    orchestrator.link("second", "first", "Same senator.", T0)
    assert "Connected articles" not in Path("site/first/index.html").read_text()   # nothing to point at yet
    run_at(T0); m = store.get_mission("second"); m.approved = True; store.save_mission(m)
    run_at(T0); run_at(T0 + timedelta(hours=48))
    m = store.get_mission("second"); m.reviewed = True; store.save_mission(m)
    run_at(T0 + timedelta(hours=49))
    second, first = Path("site/second/index.html").read_text(), Path("site/first/index.html").read_text()
    assert 'Connected articles</h2><ul class="connected"><li><a href="../first/">' in second
    assert 'href="../second/"' in first and "Same senator." in first
    orchestrator.unlink("first", "second", T0)
    assert "Connected articles" not in Path("site/first/index.html").read_text()

def test_two_published_articles_cant_be_linked_by_hand():
    published("first", "The Fed audit fight of 2018")
    store.save_mission(Mission(slug="x", topic="Other", state=State.DONE))
    with pytest.raises(ValueError, match="at least one"):
        orchestrator.link("first", "x", "", T0)

# ---------- the UI, the chat assistant ----------

@pytest.fixture
def client():
    return TestClient(ui.app)

def test_ui_asks_about_the_combine_and_approving_opens_the_new_mission(client):
    c = proposed()
    html = client.get("/").text
    assert "Combine missions" in html and "Combine them" in html and "Keep them separate" in html
    assert "The Theorist proposes combining these 2 missions" in html
    r = client.post(f"/combines/{c.slug}/approve", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/m/{c.slug}?done=combined")
    page = client.get(f"/m/{c.slug}").text
    assert "Combines <a href=\"/m/memo\">" in page
    assert "Combined into" in client.get("/m/memo").text
    assert 'href="/m/memo"' not in client.get("/").text.split("In progress")[1].split("Inspiration")[0]
    assert client.post(f"/combines/{c.slug}/approve", headers={"origin": "https://evil.example"}).status_code == 403

def test_ui_reject_combine(client):
    c = proposed()
    r = client.post(f"/combines/{c.slug}/reject", follow_redirects=False)
    assert "done=combine_rejected" in r.headers["location"] and store.get_combine(c.slug).status == "rejected"
    assert "Combine missions" not in client.get("/").text

def test_ui_link_and_unlink(client):
    desk()
    r = client.post("/m/memo/link", data={"to": "old", "why": "Same author."}, follow_redirects=False)
    assert "done=linked" in r.headers["location"] and store.get_link("memo", "old").by == "human"
    page = client.get("/m/memo").text
    assert "Connections" in page and "Linked to <a href=\"/m/old\">" in page and "Same author." in page
    assert "Unlink" in page
    r = client.post("/m/memo/unlink", data={"other": "old"}, follow_redirects=False)
    assert "done=unlinked" in r.headers["location"] and store.get_link("memo", "old").status == "removed"
    r = client.post("/m/memo/link", data={"to": "memo"}, follow_redirects=False)
    assert "error=link" in r.headers["location"]

def test_theorist_notes_and_the_chat_assistant_name_the_connections(client):
    desk()
    orchestrator.apply_theory(Theory(patterns=["p"], inspiration=LINE, based_on=LINE, combine=combine(),
                                     links=[NewLink(mission="dam", to="old", why="Same money.")]), T0)
    html = client.get("/").text
    assert "proposed combining 2 missions into <strong>" in html and "linked <a href=\"/m/dam\">" in html
    notes = status_tools.theorist_notes()
    assert "Proposed combining memo, lobby into:" in notes and "Linked: dam <-> old" in notes
    assert "Linked to old by the theorist: Same money." in status_tools.mission_status("dam")
