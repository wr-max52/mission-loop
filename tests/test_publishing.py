"""The published side of the UI: the Missions page as the flow chart top to bottom, publishing as one step on
the strip, the Published page, publishing an article again, and custom channels."""
import json, re
from datetime import timedelta
from pathlib import Path
import httpx, pytest
import activity, agents, orchestrator, settings, store, ui
from agents import bluesky, custom, f916
from contracts import Mission, State
from starlette.testclient import TestClient
from test_bluesky import FakeBluesky, backlog, on_bluesky
from test_channels import published, run_at, on_1f916, T0

pytestmark = pytest.mark.usefixtures("stub_agents", "no_outreach")

@pytest.fixture
def no_outreach(monkeypatch):
    monkeypatch.setattr(settings, "outreach_on", lambda: False)

@pytest.fixture
def client():
    return TestClient(ui.app)

@pytest.fixture
def sky(monkeypatch):
    fake = FakeBluesky()
    monkeypatch.setattr(bluesky, "TRANSPORT", httpx.MockTransport(fake))
    monkeypatch.setenv("BLUESKY_HANDLE", "@loopdesk.bsky.social")
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "app-pass-test")
    return fake

# ---------- the Missions page and the strip ----------

def test_missions_page_runs_the_flow_top_to_bottom_and_only_links_to_what_is_published(client):
    for slug, state, extra in (("queued", State.QUEUED, {}), ("search", State.RESEARCHING, {}),
                               ("brief", State.RESEARCHING, {"gathered": True}),
                               ("approved", State.BRIEF_READY, {"approved": True}), ("writing", State.WRITING, {}),
                               ("ready", State.ARTICLE_READY, {"reviewed": True}),
                               ("parked", State.PARKED, {}), ("review-brief", State.BRIEF_READY, {}),
                               ("review-article", State.ARTICLE_READY, {}),
                               ("done", State.DONE, {"published_at": T0})):
        store.save_mission(Mission(slug=slug, topic=slug, state=state, **extra))
    html = client.get("/").text
    order = re.findall(r'<a class="(?:card[^"]*|topic)" href="/m/([a-z-]+)"', html)   # decisions, then cards
    assert order == ["review-article", "review-brief", "parked",                       # needs you, at the top
                     "writing", "approved", "brief", "search", "queued"]                # then the flow, new last
    assert "Published (" not in html and 'href="/m/done"' not in html and 'href="/m/ready"' not in html
    queue = html.split('<a class="card publish" href="/published#queue">')[1].split("</a>")[0]   # one card instead
    assert "1 article queued" in queue and "Local site 1" in queue
    assert html.index("#queue") < html.index('href="/m/writing"')                      # publishing first
    assert '<a class="card site-link" href="/published">' in html and "1 article</span>" in html
    assert html.index('href="/m/queued"') < html.index('class="card site-link"')
    assert html.index('action="/missions"') < html.index('class="card site-link"')    # the very bottom
    assert f'<a href="/published">{ui.ICON_SITE}Published</a>' in html.split("</nav>")[0]   # and a button at the top
    assert f'{ui.ICON_SITE}Published</span>' in html

def test_publishing_is_one_step_on_the_strip_whatever_the_channels(client, monkeypatch, sky):
    monkeypatch.setattr(ui, "loop_running", lambda: True)
    backlog("a")
    track = client.get("/").text.split('class="flow-track"')[1].split("</section>")[0]
    assert re.findall(r'data-step="([a-z0-9_]+)"', track)[-1] == "publisher"
    assert "Bluesky" not in track and "1F916" not in track
    activity.begin("a", "bluesky")                     # posting to a channel lights Publish
    flow = client.get("/api/flow").json()
    assert flow["working"][0]["step"] == "publisher" and "Bluesky poster" in flow["now"]
    assert '<span class="step on" data-step="publisher"' in client.get("/").text

# ---------- the Published page ----------

def test_nav_goes_to_the_published_page_which_leads_back(client, sky):
    backlog("a")
    assert '<a href="/published">' in client.get("/").text and 'href="/site/" target="_blank">' not in \
        client.get("/").text.split("</nav>")[0]
    html = client.get("/published").text
    assert '<a class="back" href="/">&larr; Missions</a>' in html
    assert '<a class="btn" href="/site/" target="_blank">Open the local site</a>' in html
    assert "<strong>Local site</strong>: always on" in html and "<strong>Bluesky</strong>: queued" in html
    assert 'action="/m/a/republish"' in html
    assert '<input type="checkbox" name="to" value="site" checked> Local site' in html
    assert '<input type="checkbox" name="to" value="bluesky" checked> Bluesky' in html   # not posted yet: ticked
    run_at(T0)
    html = client.get("/published").text
    assert "<strong>Bluesky</strong>: posted" in html
    post = store.get_mission("a").channels["bluesky"].where
    assert post.startswith("https://bsky.app/")      # posted: a link to the post where its box was, no second post
    assert f'<a class="tick posted" href="{post}" target="_blank" rel="noopener">Bluesky &#8599;</a>' in html
    assert 'value="bluesky"' not in html and "(again)" not in html
    assert '<input type="checkbox" name="to" value="site" checked> Local site' in html   # our own page: rebuilt

def test_a_channel_with_no_web_address_shows_posted_without_a_box(client, tmp_path):
    published("a", T0)
    client.post("/settings/custom-channel", data={"name": "Outbox", "kind": "folder",
                                                  "target": str(tmp_path / "out"), "format": "markdown"})
    run_at(T0 + timedelta(minutes=1))
    assert "custom-outbox" in store.get_mission("a").channels
    html = client.get("/published").text
    assert '<span class="tick posted">Outbox<span class="hint"> (posted)</span></span>' in html
    assert 'value="custom-outbox"' not in html

# ---------- publishing again ----------

def test_publishing_again_skips_bluesky_gap_and_goes_ahead_of_the_queue(client, sky, monkeypatch):
    monkeypatch.setattr(ui, "loop_running", lambda: True)
    monkeypatch.setattr(ui, "wake", lambda: None)
    backlog("old", "newer")
    run_at(T0)                                         # newest first; the gap holds the other back
    assert on_bluesky("newer") and not on_bluesky("old") and len(sky.roots) == 1
    r = client.post("/m/old/republish", data={"to": ["bluesky"], "back": "/published"}, follow_redirects=False)
    assert r.headers["location"] == "/published?done=republish"
    m = store.get_mission("old")
    assert set(m.publish_requests) == {"bluesky"}
    assert ui.label(m) == "Publishing again"
    home = client.get("/").text                        # one queue card on the Missions page, not the article's
    assert 'href="/m/old"' not in home and "1 article queued" in home and "Bluesky 1" in home
    queue = client.get("/published").text.split('id="queue"')[1].split('<h2')[0]
    assert '<a href="#a-old">' in queue and "<strong>requested</strong>" in queue
    run_at(T0 + timedelta(minutes=1))                  # inside the 10-minute gap, but a human asked
    assert on_bluesky("old") and len(sky.roots) == 2 and not store.get_mission("old").publish_requests

def test_a_channel_already_posted_to_is_never_asked_again(client, sky, monkeypatch):
    monkeypatch.setattr(ui, "loop_running", lambda: True)
    monkeypatch.setattr(ui, "wake", lambda: None)
    backlog("a")
    run_at(T0)
    assert on_bluesky("a") and len(sky.roots) == 1
    r = client.post("/m/a/republish", data={"to": "bluesky", "back": "/published"}, follow_redirects=False)
    assert r.headers["location"] == "/published?error=places"   # a hand-made post can't do it either
    assert not store.get_mission("a").publish_requests
    client.post("/m/a/republish", data={"to": ["site", "bluesky"], "back": "/published"})
    assert set(store.get_mission("a").publish_requests) == {"site"}   # the page is rebuilt, no second post
    run_at(T0 + timedelta(minutes=30))
    assert len(sky.roots) == 1

@pytest.fixture
def publish_waits(monkeypatch):
    """The UI publishes in the background while the loop is off; wait for it so the test sees the result."""
    real = ui.publish_now
    monkeypatch.setattr(ui, "publish_now", lambda: real().join())

def test_the_queue_is_on_the_published_page_with_one_card_on_the_missions_page(client, sky):
    backlog("old", "newer")
    store.save_mission(Mission(slug="ready", topic="Ready to go", state=State.ARTICLE_READY, reviewed=True))
    home = client.get("/").text
    assert "3 articles queued" in home and "Local site 1 · Bluesky 2" in home
    page = client.get("/published").text
    queue = page.split('id="queue"')[1].split('<h2')[0]
    assert re.findall(r'<a href="([^"]+)">', queue) == ["/m/ready", "#a-newer", "#a-old"]   # in posting order
    assert page.index('id="queue"') < page.index("Articles</span>")
    store.save_mission(store.get_mission("ready").model_copy(update={"state": State.DONE, "published_at": T0}))
    run_at(T0)
    run_at(T0 + timedelta(hours=1))
    run_at(T0 + timedelta(hours=2))
    assert "articles queued" not in client.get("/").text and "article queued" not in client.get("/").text
    assert "Nothing queued" in client.get("/published").text

def test_publishing_again_needs_no_loop_and_leaves_the_backlog_to_it(client, sky, monkeypatch, publish_waits):
    backlog("old", "middle", "newer")
    run_at(T0)                                         # newest first; the gap holds the others back
    assert len(sky.roots) == 1 and not on_bluesky("old")
    orchestrator.STOP_FLAG.parent.mkdir(exist_ok=True)
    orchestrator.STOP_FLAG.touch()                     # left behind by the last stop: no matter here
    Path("site/old/index.html").write_text("stale")
    monkeypatch.setattr(ui.clock, "now", lambda: T0 + timedelta(minutes=1))   # inside the gap, but a human asked
    r = client.post("/m/old/republish", data={"to": ["site", "bluesky"], "back": "/published"},
                    follow_redirects=False)
    assert r.headers["location"] == "/published?done=republish_off"
    assert on_bluesky("old") and len(sky.roots) == 2 and not store.get_mission("old").publish_requests
    assert Path("site/old/index.html").read_text() != "stale"
    assert not on_bluesky("middle")                    # nobody asked for it: it waits for the loop
    assert "<strong>Bluesky</strong>: queued" in client.get("/published").text

def test_a_failed_request_without_the_loop_stays_requested(client, sky, monkeypatch, publish_waits):
    backlog("a")
    sky.limited = True
    client.post("/m/a/republish", data={"to": "bluesky", "back": "/published"})
    assert "bluesky" in store.get_mission("a").publish_requests and not sky.roots
    sky.limited = False
    run_at(T0 + timedelta(minutes=60))                 # the loop picks it up
    assert not store.get_mission("a").publish_requests and len(sky.roots) == 1

def test_a_failed_request_waits_for_the_next_publishing_pass(client, sky):
    backlog("a")
    run_at(T0)
    store.save_mission(store.get_mission("a").model_copy(update={"publish_requests": {"bluesky": T0}}))
    sky.limited = True
    run_at(T0 + timedelta(minutes=30))
    assert "bluesky" in store.get_mission("a").publish_requests   # kept, not lost
    sky.limited = False
    run_at(T0 + timedelta(minutes=60))
    assert not store.get_mission("a").publish_requests and len(sky.roots) == 2

def test_a_new_request_gets_a_pass_while_the_loop_is_busy(monkeypatch, sky):
    """A request made mid-task is done on the next sweep, not after the model work."""
    backlog("a")
    run_at(T0)
    store.save_mission(Mission(slug="new", topic="Anything"))
    calls, real = [], agents.gather
    def gather(m, now=None):
        orchestrator.request_publish(store.get_mission("a"), ["site", "bluesky"], T0)
        calls.append("gather")
        return real(m, now)
    monkeypatch.setattr(agents, "gather", gather)
    posts = []
    monkeypatch.setattr(orchestrator, "post_to", lambda c, now, n=3: posts.append(list(calls)) or 0)
    run_at(T0 + timedelta(minutes=1))
    assert posts[0] == [] and posts[1] == ["gather"]   # the first sweep, then the request, before writing

def test_rewriting_the_local_page(client, monkeypatch):
    published("a", T0)
    page = Path("site/a/index.html")
    page.write_text("stale")
    orchestrator.request_publish(store.get_mission("a"), ["site", "nowhere"], T0 + timedelta(hours=1))
    assert set(store.get_mission("a").publish_requests) == {"site"}    # unknown places are dropped
    run_at(T0 + timedelta(hours=1))
    m = store.get_mission("a")
    assert page.read_text() != "stale" and not m.publish_requests and m.channels["site"].at == T0 + timedelta(hours=1)
    assert m.published_at == T0                        # still dated when it first came out
    assert client.post("/m/a/republish", data={"back": "/published"}, follow_redirects=False).headers["location"] \
        == "/published?error=places"
    store.save_mission(Mission(slug="b", topic="b"))
    assert client.post("/m/b/republish", data={"to": "site"}, follow_redirects=False).headers["location"] \
        == "/?error=state"

# ---------- custom channels ----------

def test_custom_folder_channel_from_the_settings_page(client, tmp_path):
    published("a", T0)
    out = tmp_path / "outbox"
    r = client.post("/settings/custom-channel", data={"name": "Team outbox", "kind": "folder",
                                                      "target": str(out), "format": "markdown"}, follow_redirects=False)
    assert r.headers["location"] == "/settings?done=custom_added"
    assert settings.custom_channels()[0]["id"] == "team-outbox"
    html = client.get("/settings").text
    assert "<h3>Team outbox</h3>" in html and "Custom · Write a file into a folder" in html and "1 queued" in html
    run_at(T0 + timedelta(minutes=1))
    text = (out / "a.md").read_text()
    assert text.startswith("# ") and "Sources" in text
    m = store.get_mission("a")
    assert m.channels["custom-team-outbox"].where == str(out / "a.md")
    assert "<strong>Team outbox</strong>: posted" in client.get("/published").text
    client.post("/settings/custom-channel/remove", data={"id": "team-outbox"})
    assert settings.custom_channels() == [] and "Team outbox" not in client.get("/settings").text

def test_custom_channel_settings_are_checked(client):
    for data in ({"name": "", "kind": "folder", "target": "/tmp/x", "format": "json"},
                 {"name": "x", "kind": "folder", "target": "relative/dir", "format": "json"},
                 {"name": "x", "kind": "webhook", "target": "ftp://nope", "format": "json"},
                 {"name": "x", "kind": "carrier-pigeon", "target": "/tmp/x", "format": "json"}):
        assert "Channel not added" in client.post("/settings/custom-channel", data=data).text
    assert settings.custom_channels() == []
    evil = {"origin": "https://evil.example"}
    assert client.post("/settings/custom-channel", data={"name": "x"}, headers=evil).status_code == 403

def test_custom_webhook_sends_the_feed_item_and_handles_refusals(monkeypatch):
    got = []
    def hook(request):
        item = json.loads(request.content)
        got.append((request.headers["content-type"], item))
        if item["id"] == "bad":
            return httpx.Response(422, text="no thanks")
        return httpx.Response(200, json={"url": f"https://chat.example/m/{item['id']}"})
    monkeypatch.setattr(custom, "TRANSPORT", httpx.MockTransport(hook))
    settings.add_custom_channel("Chat", "webhook", "https://hooks.example/secret-token", "json")
    published("bad", T0 - timedelta(hours=1))
    published("good", T0)
    m = store.get_mission("good")
    assert m.channels["custom-chat"].where == "https://chat.example/m/good"
    assert got[0][0] == "application/json" and {"id", "title", "summary", "content_text"} <= got[0][1].keys()
    bad = store.get_mission("bad")
    assert "custom-chat" not in bad.channels and "HTTP 422" in bad.channel_errors["custom-chat"]
    assert "secret-token" not in json.dumps([m.model_dump(mode="json") for m in store.all_missions()])

def test_custom_formats():
    entry = {"slug": "a", "title": "Title", "dek": "Dek", "published_at": T0.isoformat(), "text": "Body", "epic": "E"}
    assert json.loads(custom.render(entry, "json"))["tags"] == ["E"]
    assert custom.render(entry, "markdown") == "# Title\n\n*Dek*\n\nBody\n"
    settings.set_site_url("https://example.com/news/")
    assert custom.render(entry, "text") == "Title\n\nDek\n\nhttps://example.com/news/a/\n\nBody\n"

# ---------- working through the backlog ----------

def test_an_uncapped_channel_gets_the_whole_backlog_in_one_pass(tmp_path, sky):
    backlog(*[f"old-{i}" for i in range(7)])           # published before any channel was set up
    settings.add_custom_channel("Site repo", "folder", str(tmp_path / "repo"), "json")
    run_at(T0)
    assert len(list((tmp_path / "repo").glob("*.json"))) == 7
    assert len(sky.roots) == 1                         # Bluesky: one, then its 10-minute gap
    run_at(T0 + timedelta(minutes=11))
    assert len(sky.roots) == 2

def test_1f916_takes_the_backlog_one_a_day(monkeypatch):
    from test_channels import FakeSite
    fake = FakeSite()
    monkeypatch.setattr(f916, "TRANSPORT", httpx.MockTransport(fake))
    backlog("a", "b", "c")
    Path("data").mkdir(exist_ok=True)
    f916.KEY_FILE.write_text(json.dumps({"handle": "loop-desk", "secret": "1f916_sk_test"}))
    for day in range(3):
        fake.today = (T0 + timedelta(days=day)).date()
        run_at(T0 + timedelta(days=day))
        assert len(fake.posts) == day + 1          # as the allowance comes, newest first
    assert all(on_1f916(s) for s in "abc")

def test_a_channel_refusing_everything_stops_after_a_few(monkeypatch):
    tries = []
    def hook(request):
        tries.append(1)
        return httpx.Response(400, text="nope")
    monkeypatch.setattr(custom, "TRANSPORT", httpx.MockTransport(hook))
    backlog(*[f"old-{i}" for i in range(6)])
    settings.add_custom_channel("Picky", "webhook", "https://hooks.example/x", "text")
    orchestrator.post_to_channels(T0)
    assert len(tries) == orchestrator.MAX_REFUSALS

def test_long_model_work_still_gets_publishing_passes(monkeypatch, tmp_path):
    backlog("a")
    store.save_mission(Mission(slug="new", topic="Anything"))
    passes = []
    monkeypatch.setattr(orchestrator, "PASS_EVERY", 0)  # every sweep counts as due
    monkeypatch.setattr(orchestrator, "post_to_channels", lambda now, *a: passes.append(now) or 0)
    run_at(T0)
    assert len(passes) > 2                             # not only at the start and the end

# ---------- how long each queue takes (#119) ----------

def test_a_paced_channel_says_how_long_its_queue_takes_and_one_without_pace_says_nothing():
    import types
    from agents import channel
    spent = T0.replace(hour=0) + timedelta(days=1)     # today's post is spent until the next UTC day
    daily = types.SimpleNamespace(PACE=timedelta(days=1), wait_until=lambda: spent)
    assert channel.estimate(daily, 21, T0) == "about 3 weeks (one a day)"
    assert channel.backlog_time(daily, 21, T0) == spent - T0 + timedelta(days=20)   # wait, then one a day
    uncapped = types.SimpleNamespace(wait_until=lambda: None)
    assert channel.backlog_time(uncapped, 21, T0) is None and channel.estimate(uncapped, 21, T0) == ""
    gap = types.SimpleNamespace(PACE=bluesky.POST_GAP, wait_until=lambda: T0 - timedelta(hours=1))   # long past
    assert channel.estimate(gap, 4, T0) == "about 30 minutes (one every 10 minutes)"
    assert channel.estimate(gap, 1, T0) == "under a minute (one every 10 minutes)"
    assert channel.estimate(gap, 0, T0) == ""
    assert f916.PACE == timedelta(days=1) and bluesky.PACE == bluesky.POST_GAP
    assert not hasattr(custom.Channel({"id": "x", "name": "X"}), "PACE")

def test_durations_round_to_the_unit_that_reads_well():
    import clock
    m = lambda n: timedelta(minutes=n)
    words = [clock.about(t) for t in (timedelta(seconds=10), m(1), m(40), m(59.6), m(150), timedelta(hours=23.6),
                                      timedelta(days=9), timedelta(days=13.6), timedelta(days=20.6))]
    assert words == ["under a minute", "about 1 minute", "about 40 minutes", "about 1 hour", "about 3 hours",
                     "about 1 day", "about 9 days", "about 2 weeks", "about 3 weeks"]
    assert [clock.every(t) for t in (timedelta(days=1), m(10), timedelta(hours=2), m(90), m(1), timedelta(weeks=1))] \
        == ["one a day", "one every 10 minutes", "one every 2 hours", "one every 90 minutes", "one a minute", "one a week"]
    assert clock.backlog(None, timedelta(days=1)) == "" and clock.backlog(m(5), None) == ""

def test_the_queue_card_published_page_and_status_say_how_long_each_backlog_takes(client, sky, monkeypatch, capsys):
    import run as cli
    monkeypatch.setattr(ui.clock, "now", lambda: T0)
    slugs = [f"old-{i:02}" for i in range(21)]
    backlog(*slugs)
    store.save_mission(Mission(slug="ready", topic="Ready to go", state=State.ARTICLE_READY, reviewed=True))
    Path("data").mkdir(exist_ok=True)
    f916.KEY_FILE.write_text(json.dumps({"handle": "loop-desk", "secret": "1f916_sk_test"}))
    f916.save_state({"spent_until": f916.next_utc_day(T0).isoformat()})   # today's post already went
    home = client.get("/").text
    card = home.split('<a class="card publish" href="/published#queue">')[1].split("</a>")[0]
    assert ("Local site 1 · 1F916 21 · about 3 weeks (one a day) · "
            "Bluesky 21 · about 3 hours (one every 10 minutes)") in card      # the local site has no cap
    queue = client.get("/published").text.split('id="queue"')[1].split("<h2")[0]
    assert '<span>1F916</span><span class="n">21</span><span class="hint">about 3 weeks (one a day)</span>' in queue
    assert '<span>Local site</span><span class="n">1</span></h3>' in queue
    cli.status()
    out = capsys.readouterr().out
    assert "1F916 as loop-desk: 21 queued · about 3 weeks (one a day), next post after" in out
    assert "Bluesky as loopdesk.bsky.social: 21 queued · about 3 hours (one every 10 minutes)" in out

def test_the_estimate_is_translated_with_the_ui_and_status_stays_english(client, sky, monkeypatch, capsys):
    import run as cli
    monkeypatch.setattr(ui.clock, "now", lambda: T0)
    backlog("a", "b", "c", "d")
    client.post("/settings/language", data={"language": "fr"})
    assert "Bluesky 4 · environ 30 minutes (un toutes les 10 minutes)" in client.get("/").text
    cli.status()
    assert "Bluesky as loopdesk.bsky.social: 4 queued · about 30 minutes (one every 10 minutes)" in capsys.readouterr().out
