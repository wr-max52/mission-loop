from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from starlette.testclient import TestClient
import agents, orchestrator, related, store, ui
from agents import finder, publisher
from contracts import Mission, MissionIdea, State

pytestmark = pytest.mark.usefixtures("stub_agents")
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)

def run_at(t): orchestrator.run_until_idle(lambda: t)

def published(slug, topic, epic=""):
    store.save_mission(Mission(slug=slug, topic=topic, epic=epic))
    run_at(T0)
    m = store.get_mission(slug); m.approved = True; store.save_mission(m)
    run_at(T0); run_at(T0 + timedelta(hours=48))
    m = store.get_mission(slug); m.reviewed = True; store.save_mission(m)
    run_at(T0 + timedelta(hours=49))
    assert store.get_mission(slug).state == State.DONE

def finder_returns(monkeypatch, ideas):
    seen = {}
    def fake(inspiration, topics, now, epics=None):
        seen["epics"] = epics
        return ideas
    monkeypatch.setattr(agents, "find_missions", fake)
    store.set_inspiration("Who runs the Fed?")
    return seen

def idea(topic, epic="", new_epic=""):
    return MissionIdea(topic=topic, objective="o", source_url="https://a.example.com/x", epic=epic, new_epic=new_epic)

@pytest.fixture
def client():
    return TestClient(ui.app)

# ---------- the epic records ----------

def test_titles_are_short_and_matched_ignoring_case():
    e = store.create_epic("  Fed  Policy Capture ")
    assert (e.slug, e.title, e.status) == ("fed-policy-capture", "Fed Policy Capture", "active")
    assert store.create_epic("fed policy capture").slug == e.slug
    with pytest.raises(ValueError):
        store.create_epic("far too many words for one epic title")

def test_rename_keeps_the_slug_and_refuses_a_taken_name():
    a, b = store.create_epic("Fed Capture"), store.create_epic("Grid Failures")
    orchestrator.rename_epic(a.slug, "Central Bank Capture", T0)
    assert store.get_epic(a.slug).title == "Central Bank Capture"
    with pytest.raises(ValueError):
        orchestrator.rename_epic(a.slug, "grid failures", T0)

def test_titles_are_cleaned_and_old_ones_tidied_at_start():
    assert store.create_epic("tech_influence").title == "Tech influence"
    assert store.create_epic("Fed capture").title == "Fed capture"           # not all lower case: left alone
    assert store.find_epic("tech_influence").title == "Tech influence"
    with store.conn() as c:                                                  # saved before titles were cleaned
        c.execute("INSERT INTO epics VALUES ('grid_power', 'grid_power', 'active')")
        c.execute("INSERT INTO epics VALUES ('dupe', 'tech_influence', 'active')")
    done = orchestrator.tidy_epic_titles(T0)
    assert done == [("grid_power", "Grid power")]
    assert store.get_epic("grid_power").title == "Grid power"
    assert store.get_epic("dupe").title == "tech_influence"                  # its clean title is taken
    assert orchestrator.tidy_epic_titles(T0) == []

def test_tidy_and_rename_leave_a_title_that_only_differs_by_case_from_another_epic():
    from contracts import Epic
    store.save_epic(Epic(slug="ai-policy", title="ai policy", status="active"))     # saved before titles were cleaned
    store.save_epic(Epic(slug="ai-policy-2", title="AI policy", status="active"))
    assert orchestrator.tidy_epic_titles(T0) == []
    assert store.get_epic("ai-policy").title == "ai policy"                  # left for a human to merge
    with pytest.raises(ValueError):
        orchestrator.rename_epic("ai-policy", "Ai Policy", T0)
    lone = store.create_epic("Water Rights")
    orchestrator.rename_epic(lone.slug, "water RIGHTS", T0)                  # its own title in another case is fine
    assert store.get_epic(lone.slug).title == "water RIGHTS"

def test_merge_moves_the_missions_and_removes_the_epic_and_its_board_note():
    import board
    a, b = store.create_epic("Dam Money"), store.create_epic("Water Rights")
    store.save_mission(Mission(slug="m1", topic="One", epic=a.slug))
    store.save_mission(Mission(slug="m2", topic="Two", epic=b.slug))
    p = store.create_epic("Dam Loans", status="proposed")
    store.save_mission(Mission(slug="m3", topic="Three", epic=p.slug, epic_fallback=a.slug))
    board.layout()
    with pytest.raises(ValueError):                                          # a worker is busy with m1
        orchestrator.merge_epic(a.slug, b.slug, T0, busy={"m1"})
    orchestrator.merge_epic(a.slug, b.slug, T0)
    assert store.get_epic(a.slug) is None and store.get_mission("m1").epic == b.slug
    assert store.get_mission("m3").epic_fallback == b.slug                   # a rejection goes to the survivor
    assert not store.conn().execute("SELECT 1 FROM board WHERE kind = 'epic' AND id = ?", (a.slug,)).fetchone()

def test_delete_only_an_empty_epic():
    full, empty = store.create_epic("Dam Money"), store.create_epic("Water Rights")
    store.save_mission(Mission(slug="m1", topic="One", epic=full.slug))
    with pytest.raises(ValueError, match="merge"):
        orchestrator.delete_epic(full.slug)
    orchestrator.delete_epic(empty.slug)
    assert store.get_epic(empty.slug) is None and store.get_epic(full.slug)

def test_epic_list_merges_and_deletes(client):
    full, other, empty = (store.create_epic(t) for t in ("Dam Money", "Water Rights", "Empty One"))
    store.save_mission(Mission(slug="m1", topic="One", epic=full.slug))
    store.save_mission(Mission(slug="m2", topic="Two", epic=other.slug))
    html = ui.epic_list(store.all_missions())
    assert f'action="/epics/{full.slug}/merge"' in html and f'action="/epics/{empty.slug}/delete"' in html
    assert f'action="/epics/{full.slug}/delete"' not in html
    r = client.post(f"/epics/{full.slug}/delete", follow_redirects=False)
    assert "epic_not_empty" in r.headers["location"]
    assert store.get_epic(full.slug)                                         # refused: it has a mission
    client.post(f"/epics/{empty.slug}/delete")
    assert store.get_epic(empty.slug) is None
    client.post(f"/epics/{full.slug}/merge", data={"into": other.slug})
    assert store.get_epic(full.slug) is None and store.get_mission("m1").epic == other.slug

def test_cli_merges_and_deletes_epics():
    from typer.testing import CliRunner
    import run as cli
    a, b, e = store.create_epic("Dam Money"), store.create_epic("Water Rights"), store.create_epic("Empty One")
    store.save_mission(Mission(slug="m1", topic="One", epic=a.slug))
    r = CliRunner().invoke(cli.app, ["delete-epic", "Dam Money"])
    assert r.exit_code == 1 and "merge" in r.output
    assert CliRunner().invoke(cli.app, ["merge-epic", "dam money", "Water Rights"]).exit_code == 0
    assert store.get_mission("m1").epic == b.slug and store.get_epic(a.slug) is None
    assert CliRunner().invoke(cli.app, ["delete-epic", "empty_one"]).exit_code == 0
    assert store.get_epic(e.slug) is None

# ---------- the finder ----------

def test_finder_sees_each_epic_with_its_missions_and_assigns_the_best_fit(monkeypatch):
    fed = store.create_epic("Fed Capture")
    store.save_mission(Mission(slug="old", topic="Old Fed topic", epic=fed.slug, state=State.DONE))
    seen = finder_returns(monkeypatch, [idea("Who audits the Fed?", epic="fed capture")])
    orchestrator.find_new_missions(T0)
    assert seen["epics"] == {"Fed Capture": ["Old Fed topic"]}
    new = [m for m in store.all_missions() if m.slug != "old"][0]
    assert (new.epic, new.epic_fallback) == (fed.slug, "")

def test_new_epic_needs_two_missions_and_waits_for_the_human(monkeypatch):
    fed = store.create_epic("Fed Capture")
    finder_returns(monkeypatch, [idea("Lead one about swaps", "Fed Capture", "Swap Lines"),
                                 idea("Lead two about swaps", "Fed Capture", "swap lines"),
                                 idea("Lead three about gold", "Fed Capture", "Gold Vaults")])
    orchestrator.find_new_missions(T0)
    swap = store.find_epic("Swap Lines")
    assert swap.status == "proposed" and store.find_epic("Gold Vaults") is None
    by_topic = {m.topic: m for m in store.all_missions()}
    assert by_topic["Lead one about swaps"].epic == swap.slug
    assert by_topic["Lead one about swaps"].epic_fallback == fed.slug
    assert by_topic["Lead three about gold"].epic == fed.slug   # a lone proposal falls back to the best fit

def test_finder_prompt_lists_the_epics(monkeypatch):
    seen = []
    async def fake_tool_loop(system, tb, budget, limit, **kw):
        return [{"role": "tool", "content": "Result: https://a.example.com/x"}]
    async def fake_chat(messages, tools=None, format=None, model=None):
        seen.append(messages[-1]["content"])
        from ollama import Message
        return Message(role="assistant", content='{"missions": []}')
    monkeypatch.setattr(finder, "tool_loop", fake_tool_loop)
    monkeypatch.setattr(finder.llm, "chat", fake_chat)
    class Box:
        def __init__(self, enabled): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): pass
    import asyncio
    asyncio.run(finder.run_finder("x", [], T0, {"Fed Capture": ["Who audits the Fed?"]}, toolbox_factory=Box))
    assert "- Fed Capture\n    · Who audits the Fed?" in seen[0] and "new_epic" in seen[0]

# ---------- human decisions on a proposal ----------

def proposal():
    fed = store.create_epic("Fed Capture")
    swap = store.create_epic("Swap Lines", status="proposed")
    for s in ("a", "b"):
        store.save_mission(Mission(slug=s, topic=s, epic=swap.slug, epic_fallback=fed.slug))
    return fed, swap

def test_approve_with_a_new_name():
    fed, swap = proposal()
    orchestrator.approve_epic(swap.slug, "Dollar Swap Lines", T0)
    e = store.get_epic(swap.slug)
    assert (e.title, e.status) == ("Dollar Swap Lines", "active")
    assert store.get_mission("a").epic_fallback == ""

def test_reject_sends_missions_back_to_the_finders_pick():
    fed, swap = proposal()
    orchestrator.reject_epic(swap.slug, T0)
    assert store.get_epic(swap.slug) is None
    assert [store.get_mission(s).epic for s in "ab"] == [fed.slug, fed.slug]

def test_move_to_a_different_epic():
    fed, swap = proposal()
    grid = store.create_epic("Grid Failures")
    orchestrator.merge_epic(swap.slug, grid.slug, T0)
    assert store.get_epic(swap.slug) is None
    assert [store.get_mission(s).epic for s in "ab"] == [grid.slug, grid.slug]

def test_emptied_proposal_disappears():
    fed, swap = proposal()
    for s in "ab":
        orchestrator.assign_epic(store.get_mission(s), "Fed Capture", T0)
    assert store.get_epic(swap.slug) is None

# ---------- research sees similar published articles ----------

def test_research_gets_similar_published_articles_not_open_missions():
    published("fed", "Federal Reserve audit secrecy")
    store.save_mission(Mission(slug="open", topic="Federal Reserve audit secrecy again"))
    m = Mission(slug="new", topic="Who hid the Federal Reserve audit?")
    found = related.similar_published(m)
    assert [o.slug for o, _, _ in found] == ["fed"]
    block = related.reference_block(m)
    assert "never" in block and "cite them" in block
    assert "Cited: A - https://a.example.com" in block
    assert related.reference_block(Mission(slug="x", topic="Unrelated grid outage")) == ""

def test_reference_block_reaches_the_research_prompt(monkeypatch):
    from agents import researcher
    published("fed", "Federal Reserve audit secrecy")
    prompts = []
    async def fake_tool_loop(system, tb, budget, limit=None, **kw):
        prompts.append(system)
        raise RuntimeError("stop here")
    monkeypatch.setattr(researcher, "tool_loop", fake_tool_loop)
    class Box:
        def __init__(self, enabled): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): pass
    import asyncio
    with pytest.raises(RuntimeError):
        asyncio.run(researcher.run_research(Mission(slug="n", topic="Who hid the Federal Reserve audit?"),
                                            toolbox_factory=Box))
    assert "earlier published articles" in prompts[0]

# ---------- pills ----------

def test_published_pages_show_the_pill_and_follow_a_rename():
    fed = store.create_epic("Fed Capture")
    published("p", "Fed topic", epic=fed.slug)
    assert '<a class="epic" href="../?tag=Fed%20Capture"' in Path("site/p/index.html").read_text()
    assert "Fed Capture" in Path("site/index.html").read_text()
    orchestrator.rename_epic(fed.slug, "Central Bank Capture", T0)
    assert "Central Bank Capture" in Path("site/p/index.html").read_text()
    assert "Central Bank Capture" in Path("site/index.html").read_text()

def test_proposed_epic_is_not_on_the_site_until_approved():
    fed, swap = proposal()
    m = store.get_mission("a"); m.state, m.published_at = State.DONE, T0; store.save_mission(m)
    published("c", "c topic", epic=swap.slug)
    assert "Swap Lines" not in Path("site/c/index.html").read_text()
    orchestrator.approve_epic(swap.slug, "Swap Lines", T0)
    assert "Swap Lines" in Path("site/c/index.html").read_text()

def test_ui_shows_pills_and_the_proposal(client):
    fed, swap = proposal()
    html = client.get("/").text
    assert '<div class="ask propose">' in html and "New epic" in html and "Approve epic" in html and "if rejected: Fed Capture" in html
    assert 'class="epic proposed"' in html
    r = client.post(f"/epics/{swap.slug}/approve", data={"title": "Dollar Swaps"}, follow_redirects=False)
    assert r.status_code == 303 and store.get_epic(swap.slug).title == "Dollar Swaps"
    assert '<span class="epic">Dollar Swaps</span>' in client.get("/").text

def test_ui_add_and_change_epic(client):
    client.post("/missions", data={"topic": "Grid topic", "slug": "g", "epic": "Grid Failures"})
    assert store.get_epic(store.get_mission("g").epic).title == "Grid Failures"
    r = client.post("/m/g/epic", data={"epic": "way too many words in this title"}, follow_redirects=False)
    assert "error=epic" in r.headers["location"]
    client.post("/m/g/epic", data={"epic": ""})
    assert store.get_mission("g").epic == ""

# ---------- the site: tags, a way back, the About page ----------

def test_site_filters_by_tag_and_every_article_leads_back():
    fed = store.create_epic("Fed Capture")
    published("p", "Fed topic", epic=fed.slug)
    published("q", "Loose topic")
    index = Path("site/index.html").read_text()
    assert '<nav class="tags"' in index and 'data-tag="Fed Capture"' in index   # one chip per tag, plus All
    assert '<li data-tag="Fed Capture">' in index and '<li data-tag="">' in index
    page = Path("site/p/index.html").read_text()
    assert '<a class="back" href="../">' in page and 'href="../about/"' in page   # back to the list, and the About page
    assert '<nav class="pager" data-slug="p" data-feed="../feed.json"' in page   # newer / older, read from the feed
    assert '"id": "p"' in Path("site/feed.json").read_text()
    preview = Path("missions/p/preview.html").read_text()
    assert 'class="back"' not in preview and 'href="../about/"' not in preview and '<nav class="pager"' not in preview   # not on the site yet

def test_about_page_always_draws_outreach(monkeypatch):
    # #155: the About page shows the whole line, outreach included, whatever this desk has switched on; the UI's
    # strip still follows the setup.
    import flowchart, render, settings
    monkeypatch.setattr(settings, "outreach_on", lambda: False)
    strip = render.english_track()
    assert all(f'data-step="{k}"' in strip for k in flowchart.OUTREACH_STEPS)
    assert 'data-step="outreach"' not in flowchart.track({}, {}, {})
    about = render.about_html()
    assert 'id="step-outreach"' in about and 'id="step-replies"' in about

def test_about_page_plays_the_pipeline_and_explains_the_agents():
    published("p", "Fed topic")
    about = Path("site/about/index.html").read_text()
    assert 'id="flow" class="flow" data-demo' in about and 'data-step="brief_review"' in about
    assert 'data-step="publisher"' in about and "Source finder" in about and "Theorist" in about
    assert 'data-tour' in about and '<div class="flow-track" data-lab>' in about   # one mission runs it, carried by researchers
    assert about.count('class="card stepcard"') == 14 and "lab-floor" not in about  # a card a step; researchers drop in
    assert "RESEARCHERS_PACE" in about
    for name in ("researchers.js", "flow.js", "about.js"):                         # it plays wherever the site is hosted
        assert Path("site/assets", name).read_bytes() == (publisher.ASSETS / name).read_bytes()
        assert f'src="../assets/{name}"' in about
    assert 'aria-current="page">About' in about
    Path("site/assets/researchers.js").write_text("an older drawing")              # the code moved on since
    assert publisher.refresh_assets() == ["researchers.js"]                        # only what differs is copied
    assert Path("site/assets/researchers.js").read_bytes() == (publisher.ASSETS / "researchers.js").read_bytes()
    assert publisher.refresh_assets() == []

def test_who_does_what_is_a_card_for_each_step_in_strip_order():
    import re, render, flowchart
    about = render.about_html()
    cards = re.findall(r'<div class="card stepcard" id="step-(\w+)" data-kind="(\w+)" data-lab="\1"[^>]*>'
                       r'<div class="head"><span class="badge"><span class="ico"', about)
    strip = [k for k, kind, _ in flowchart.FLOW]                                 # outreach too: the About strip always has it
    told = {"theorist_check", "finder_check", "outreach_check", "rating_check"}  # on the Theorist's, finder's, outreach's and rater's cards
    assert [k for k, _ in cards] == [k for k in strip if k not in told]
    assert dict(cards)["source_check"] == "check" and dict(cards)["brief_review"] == "human" and dict(cards)["writer"] == "agent"
    assert 'id="step-sources" data-kind="agent" data-lab="sources" data-worker="assistant"' in about   # its own crew mostly visits

def test_no_mission_takes_a_site_folder_as_its_slug():
    assert orchestrator._new_slug("About", set()) == "about-2"
    assert orchestrator._new_slug("Assets", set()) == "assets-2"
