"""The cork board (#67): notes placed by code with no model, strings read from the desk, mentions found by code,
and the page, API and drag endpoint in the UI."""
import math
from pathlib import Path
import pytest
from starlette.testclient import TestClient
import board, orchestrator, related, store, ui
from contracts import (Article, Claim, Link, Mission, ResearchBrief, Section, Source, State)

def brief(thesis: str, urls: list[str], claim: str = "The filing says so.") -> ResearchBrief:
    return ResearchBrief(thesis=thesis, counterpoint="Skeptics disagree.",
                         sources=[Source(id=f"s{i}", url=u, title="t", quote="q", stance="supports")
                                  for i, u in enumerate(urls, 1)],
                         claims=[Claim(text=claim, source_ids=["s1"])])

def note(data: dict, id_: str) -> dict:
    return next(n for n in data["notes"] if n["id"] == id_)

def gap(a: dict, b: dict) -> float:
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(orchestrator, "LOCK_FILE", Path("data/loop.lock"))
    return TestClient(ui.app)

# ---------- placement ----------

def test_a_new_mission_lands_next_to_its_epic_with_a_string_and_no_model(monkeypatch):
    import agents
    for name in ("theorize", "find_missions", "gather", "write_brief", "write"):
        monkeypatch.setattr(agents, name, lambda *a, **k: pytest.fail("the board called a model"))
    grid = store.create_epic("Grid Power")
    store.save_mission(Mission(slug="texas", topic="Texas grid rule", epic=grid.slug))
    data = board.layout()
    epic, m = note(data, f"e:{grid.slug}"), note(data, "m:texas")
    assert gap(epic, m) == pytest.approx(board.SLOT)                       # the first ring round its epic
    assert {"a": f"e:{grid.slug}", "b": "m:texas", "kind": "epic"} in data["strings"]

def test_placement_is_the_same_every_time_and_kept_once_made():
    a, b = store.create_epic("Grid Power"), store.create_epic("Ocean Data")
    for i in range(8):
        store.save_mission(Mission(slug=f"m{i}", topic=f"Topic {i}", epic=(a if i % 2 else b).slug))
    first = {n["id"]: (n["x"], n["y"]) for n in board.layout()["notes"]}
    assert {n["id"]: (n["x"], n["y"]) for n in board.layout()["notes"]} == first   # stored, so it never jumps
    with store.conn() as c:
        c.execute("DELETE FROM board")
    assert {n["id"]: (n["x"], n["y"]) for n in board.layout()["notes"]} == first   # and placed the same again
    spots = list(first.values())
    assert all(math.hypot(x1 - x2, (y1 - y2) * board.TALL) >= board.MIN_GAP - 1e-6
               for i, (x1, y1) in enumerate(spots) for (x2, y2) in spots[i + 1:])  # nothing on top of anything

def test_missions_in_no_epic_gather_round_unsorted_and_combined_ones_round_their_mission():
    store.save_mission(Mission(slug="loose", topic="No epic yet"))
    store.save_mission(Mission(slug="big", topic="The bigger one"))
    store.save_mission(Mission(slug="old", topic="Folded in", state=State.COMBINED, combined_into="big"))
    data = board.layout()
    unsorted = note(data, f"e:{board.UNSORTED}")
    assert unsorted["kind"] == "unsorted"
    assert gap(unsorted, note(data, "m:loose")) == pytest.approx(board.SLOT)
    assert gap(note(data, "m:big"), note(data, "m:old")) < 3 * board.SLOT
    assert note(data, "m:old")["size"] == board.STAGE_SIZE[State.COMBINED]

def test_further_along_is_bigger_and_a_set_size_wins():
    store.save_mission(Mission(slug="q", topic="Queued"))
    store.save_mission(Mission(slug="d", topic="Published", state=State.DONE))
    data = board.layout()
    assert note(data, "m:d")["size"] > note(data, "m:q")["size"]
    board.move("mission", "q", 10, 20, size=9)                             # clamped to the board's range
    q = note(board.layout(), "m:q")
    assert (q["x"], q["y"], q["size"], q["by"]) == (10, 20, board.SIZE_MAX, "human")
    board.move("mission", "q", 30, 40)                                     # a move keeps the size it had
    assert note(board.layout(), "m:q")["size"] == board.SIZE_MAX

def test_move_refuses_what_is_not_on_the_board():
    with pytest.raises(ValueError):
        board.move("mission", "nope", 0, 0)
    with pytest.raises(ValueError):
        board.move("table", "x", 0, 0)
    store.save_mission(Mission(slug="q", topic="Queued"))
    with pytest.raises(ValueError):
        board.move("mission", "q", float("nan"), 0)

# ---------- strings and mentions ----------

def test_links_and_mentions_are_strings_once_per_pair():
    store.save_mission(Mission(slug="a", topic="Alpha"))
    store.save_mission(Mission(slug="b", topic="Beta"))
    store.save_mission(Mission(slug="c", topic="Gamma"))
    store.save_link(Link(a="a", b="b", why="same money"))
    store.save_artifact("a", "brief", brief("A thesis.", ["https://www.ex.com/page/", "https://other.org/x"]))
    store.save_artifact("c", "brief", brief("C thesis.", ["http://ex.com/page"]))
    store.save_artifact("b", "brief", brief("B thesis.", ["https://ex.com/page"]))
    assert board.find_all_mentions() > 0
    lines = board.strings()
    assert {"a": "m:a", "b": "m:b", "kind": "link", "why": "same money", "by": "theorist"} in lines
    assert not any(s["kind"] == "mention" and {s["a"], s["b"]} == {"m:a", "m:b"} for s in lines)   # the link covers it
    ac = next(s for s in lines if {s["a"], s["b"]} == {"m:a", "m:c"})
    assert ac["kind"] == "mention" and ac["sub"] == ["source"] and "ex.com/page" in ac["why"]

def test_an_archived_copy_counts_as_the_same_page():
    assert board.page_key("https://web.archive.org/web/2024id_/https://www.ex.com/a/") == board.page_key("http://ex.com/a")

def test_naming_another_mission_is_a_mention():
    store.save_mission(Mission(slug="buoys", topic="NOAA buoy outages during hurricane season"))
    store.save_mission(Mission(slug="cables", topic="Undersea cables"))
    store.save_artifact("cables", "brief", brief("Repairs lag.", ["https://a.org"],
                                                 claim="Like the NOAA buoy outages during hurricane season, repairs lag."))
    board.find_mentions("cables")
    [k] = store.all_mentions()
    assert (k.a, k.b, k.kind) == ("buoys", "cables", "name") and "noaa buoy outages" in k.detail

def test_short_names_and_partial_words_never_count():
    store.save_mission(Mission(slug="grid", topic="Grid"))                 # one word: too short to count
    store.save_mission(Mission(slug="x", topic="Something else"))
    store.save_artifact("x", "brief", brief("The grid failed.", ["https://a.org"]))
    assert board.find_mentions("x") == 0

def test_saving_a_brief_finds_its_mentions(monkeypatch, stub_agents):
    store.save_mission(Mission(slug="old", topic="Old one", state=State.DONE))
    store.save_artifact("old", "brief", brief("Old.", ["https://stub.example/source"]))
    calls = []
    monkeypatch.setattr(board, "find_mentions", lambda slug: calls.append(slug))
    store.save_mission(Mission(slug="new", topic="New one"))
    from datetime import datetime, timezone
    orchestrator.run_until_idle(lambda: datetime(2026, 11, 2, tzinfo=timezone.utc))
    assert store.get_mission("new").state == State.BRIEF_READY and "new" in calls

def test_research_shown_a_related_article_strings_them_and_keeps_other_missions_rows():
    for slug, topic in [("past", "grid outage report texas winter"), ("now", "texas winter grid outage"),
                        ("third", "texas winter grid outage again")]:
        store.save_mission(Mission(slug=slug, topic=topic, state=State.DONE if slug == "past" else State.QUEUED))
    store.save_artifact("past", "brief", brief("Texas winter grid outage thesis.", ["https://a.org"]))
    store.save_artifact("past", "article", Article(title="Texas winter grid outage", dek="d",
                        sections=[Section(heading="h", paragraphs=["p"]), Section(heading="h2", paragraphs=["p"])]))
    related.similar_block(store.get_mission("now"))
    related.similar_block(store.get_mission("third"))
    related.similar_block(store.get_mission("now"))                       # seen again: replaces only its own rows
    found = sorted((k.a, k.b, k.detail) for k in store.all_mentions() if k.kind == "background")
    assert found == [("now", "past", "now"), ("past", "third", "third")]

# ---------- the UI ----------

def test_board_is_in_the_nav_and_every_card_points_to_it(client):
    store.save_mission(Mission(slug="q", topic="Queued topic"))
    home = client.get("/").text
    assert home.index('href="/published"') < home.index('href="/board"') < home.index('href="/settings"')
    assert 'data-board="q"' in home
    assert 'href="/board?focus=q"' in client.get("/m/q").text

def test_board_page_and_api_carry_the_words(client):
    e = store.create_epic("Grid Power")
    store.save_mission(Mission(slug="b", topic="Who funds the critics?", epic=e.slug, state=State.BRIEF_READY))
    store.save_artifact("b", "brief", brief("Three groups got $4.1 million.", ["https://a.org", "https://b.org"]))
    html = client.get("/board?focus=b").text
    assert 'id="board"' in html and 'data-focus="b"' in html and "/assets/board.js" in html
    data = client.get("/api/board").json()
    m = note(data, "m:b")
    assert (m["title"], m["thesis"], m["thesis_label"], m["paper"], m["epic"], m["open"], m["url"]) == (
        "Who funds the critics?", "Three groups got $4.1 million.", "Thesis", "review", "Grid Power", "Open brief", "/m/b")
    assert m["meta"].startswith("2 sources")
    assert note(data, f"e:{e.slug}")["count_text"] == "1 mission"
    assert client.get(f"/api/board?sig={data['sig']}").json() == {"same": True}   # nothing changed

def test_a_queued_mission_shows_its_objective(client):
    store.save_mission(Mission(slug="q", topic="Queued topic", objective="Find the money."))
    m = note(client.get("/api/board").json(), "m:q")
    assert (m["thesis"], m["thesis_label"], m["open"], m["stage"]) == ("Find the money.", "Objective", "Open mission", "Queued")

def test_dragging_a_note_is_saved(client):
    store.save_mission(Mission(slug="q", topic="Queued topic"))
    r = client.post("/board/move", json={"id": "m:q", "x": 123.4, "y": -50, "size": 1.2})
    assert r.status_code == 200
    q = note(client.get("/api/board").json(), "m:q")
    assert (q["x"], q["y"], q["size"], q["by"]) == (123.4, -50, 1.2, "human")
    assert client.post("/board/move", json={"id": "m:nope", "x": 0, "y": 0}).status_code == 400
    assert client.post("/board/move", json={"id": "q", "x": 0, "y": 0}).status_code == 400
    other = client.post("/board/move", json={"id": "m:q", "x": 0, "y": 0}, headers={"origin": "https://evil.example"})
    assert other.status_code == 403

# ---------- the board's look (#68) ----------

def test_board_style_saves_only_what_is_not_the_default():
    import settings
    assert settings.board_style() == settings.BOARD_DEFAULT
    style = settings.set_board_style(cards="magnets", string="laser")
    assert style == {"surface": "cork", "cards": "magnets", "string": "laser", "color": "#ff3344"}  # laser's own red
    assert settings.saved()["board_style"] == {"cards": "magnets", "string": "laser"}
    settings.set_board_style(color="#123abc")
    assert settings.set_board_style(string="wire")["color"] == "#123abc"   # a human's own colour stays
    settings.set_board_style(surface="cork", cards="sticky", string="string", color="#c0242b")
    assert "board_style" not in settings.saved()
    for bad in ({"surface": "moon"}, {"cards": "tarot"}, {"string": "rope"}, {"color": "red"}):
        with pytest.raises(ValueError):
            settings.set_board_style(**bad)

def test_the_gear_and_the_settings_page_change_the_same_look(client):
    store.save_mission(Mission(slug="q", topic="Queued topic"))
    r = client.post("/board/style", json={"surface": "dark", "cards": "hologram", "string": "laser"})
    assert r.json() == {"surface": "dark", "cards": "hologram", "string": "laser", "color": "#ff3344"}
    html = client.get("/board").text
    assert "surface-dark cards-hologram str-laser" in html and "--string:#ff3344" in html
    assert client.get("/api/board").json()["style"]["cards"] == "hologram"
    page = client.get("/settings").text
    assert 'id="board"' in page and 'name="cards" value="hologram" checked' in page
    client.post("/settings/board-style", data={"cards": "magnets"})
    assert client.get("/api/board").json()["style"]["cards"] == "magnets"
    assert client.post("/board/style", json={"cards": "tarot"}).status_code == 400
    assert client.post("/board/style", json={"cards": "sticky"}, headers={"origin": "https://evil.example"}).status_code == 403

def test_a_note_a_model_works_on_names_whose_crew_visits_it(client, monkeypatch):
    import activity
    store.save_mission(Mission(slug="w", topic="Being written", state=State.WRITING))
    store.save_mission(Mission(slug="q", topic="Queued topic"))
    monkeypatch.setattr(ui, "running_now", lambda: [{"worker": "pi", "slug": "w", "agent": "writer", "since": 0}])
    data = client.get("/api/board").json()
    assert (note(data, "m:w")["live"], note(data, "m:w")["worker"]) == (True, "pi")
    assert (note(data, "m:q")["live"], note(data, "m:q")["worker"]) == (False, "")
    assert "/assets/researchers.js" in client.get("/board").text          # animations are on by default
    import settings
    settings.set_animations(False)
    assert "/assets/researchers.js" not in client.get("/board").text

def test_pixel_cards_arcade_and_pixel_string():
    import settings
    style = settings.set_board_style(surface="arcade", cards="pixel", string="pixel")
    assert style == {"surface": "arcade", "cards": "pixel", "string": "pixel", "color": "#ff004d"}

# ---------- the Theorist's board turns (#69) ----------

from datetime import datetime, timezone
from ollama import Message
import agents
from agents import llm, stubs, theorist

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)

def small_desk():
    store.set_inspiration("Who really runs the grid?")
    grid = store.create_epic("Grid Power")
    store.save_mission(Mission(slug="a", topic="Texas winter rule", epic=grid.slug, state=State.DONE))
    store.save_mission(Mission(slug="b", topic="PJM auction critics", epic=grid.slug))
    store.save_mission(Mission(slug="c", topic="Loose idea"))
    board.layout()                                                       # placed by code, as an open board would
    return grid

def rows():
    return {k: (n.x, n.y, n.size, n.by) for k, n in store.board_notes().items()}

def one_turn(now=NOW):
    """The Theorist's turn the way the loop takes it: think on a worker, then apply between tasks."""
    result = orchestrator.think(now)
    if result is not None and hasattr(result, "moves"):
        return orchestrator.apply_board(result, now)
    return orchestrator.apply_theory(result, now) if result else 0

def test_every_third_turn_only_arranges_the_board(monkeypatch):
    small_desk()
    monkeypatch.setattr(agents, "theorize", stubs.theorize)
    monkeypatch.setattr(agents, "tidy_board", theorist.tidy_board)
    seen = []
    async def fake_chat(messages, tools=None, format=None, model=None):
        seen.append(messages)
        reply = theorist.BoardReply(why="Kept the grid work together.", moves=[
            theorist.NoteMove(id="m:b", x=1200, y=-900, size=9), theorist.NoteMove(id="m:ghost", x=0, y=0)])
        return Message(role="assistant", content=reply.model_dump_json())
    monkeypatch.setattr(llm, "chat", fake_chat)
    before, missions = rows(), len(store.all_missions())
    for n in (1, 2):
        one_turn()
        assert rows() == before and store.last_theory()["turn"] == n and store.last_theory()["kind"] == "ideas"
    assert not seen                                                      # ideas turns never see the board
    one_turn()
    after = rows()
    assert {k for k in after if after[k] != before[k]} == {("mission", "b")}   # only board rows change
    x, y, size, by = after[("mission", "b")]
    assert (size, by) == (board.SIZE_MAX, "theorist")                    # clamped, and saved as the Theorist's
    last = store.last_theory()
    assert (last["turn"], last["kind"], last["why"]) == (3, "board", "Kept the grid work together.")
    assert any("m:ghost" in n for n in last["notes"])                    # the note it invented is dropped
    assert last["ideas"]["patterns"] == ["Stub pattern"] and store.last_ideas()["turn"] == 2
    assert len(store.all_missions()) == missions and store.get_inspiration() == "Who really runs the grid?"
    prompt = seen[0][0]["content"] + seen[0][1]["content"]
    assert "m:b | queued | PJM auction critics" in prompt and "e:grid-power" in prompt and "E epic" in prompt
    one_turn()
    assert store.last_theory()["kind"] == "ideas" and store.last_theory()["turn"] == 4

def test_settle_board_holds_moves_to_the_board():
    small_desk()
    data = theorist.board_desk()
    epic = next(n for n in data["notes"] if n["kind"] == "epic")
    far = theorist.NoteMove(id="m:c", x=10 ** 7, y=0, size=0.1)
    on_top = theorist.NoteMove(id="m:a", x=epic["x"], y=epic["y"])
    twice = theorist.NoteMove(id="m:c", x=0, y=0)
    tidy = theorist.settle_board(theorist.BoardReply(moves=[far, on_top, twice]), data)
    assert [m.id for m in tidy.moves] == ["m:c", "m:a"] and "m:c: moved twice" in tidy.dropped
    c, a = tidy.moves
    xs = [n["x"] for n in data["notes"]]
    assert c.x == max(xs) + theorist.BOARD_MARGIN and c.size == board.SIZE_MIN   # kept near the board, in range
    assert math.hypot(a.x - epic["x"], (a.y - epic["y"]) * board.TALL) >= board.MIN_GAP   # nudged off the epic
    many = [theorist.NoteMove(id=n["id"], x=i * 1000, y=0) for i, n in enumerate(data["notes"])] * 5
    assert len(theorist.settle_board(theorist.BoardReply(moves=many), data).moves) <= theorist.MAX_MOVES

def test_the_loop_takes_a_board_turn_when_it_is_due(monkeypatch, stub_agents):
    small_desk()
    monkeypatch.setattr(agents, "tidy_board", stubs.tidy_board)
    store.save_theory({"at": "2026-01-01T00:00:00+00:00", "fingerprint": "old", "turn": 2, "kind": "ideas",
                       "patterns": ["Earlier pattern"]})
    orchestrator.run_until_idle(lambda: NOW)
    last = store.last_theory()
    assert (last["turn"], last["kind"], last["moved"]) == (3, "board", [])
    assert last["ideas"]["patterns"] == ["Earlier pattern"]

def test_asking_the_theorist_on_a_board_turn_gets_ideas(monkeypatch):
    small_desk()
    monkeypatch.setattr(agents, "tidy_board", lambda now: pytest.fail("an ask went to the board"))
    asked = []
    monkeypatch.setattr(agents, "theorize", lambda now: asked.append(now))
    store.save_theory({"at": "2026-01-01T00:00:00+00:00", "turn": 2, "kind": "ideas", "patterns": ["P"]})
    orchestrator.THINK_FLAG.parent.mkdir(exist_ok=True)
    orchestrator.THINK_FLAG.touch()
    orchestrator.think(NOW)
    assert asked == [NOW] and not orchestrator.THINK_FLAG.exists()

def test_merged_or_dropped_epics_leave_no_board_row():
    rows = lambda slug: store.conn().execute("SELECT COUNT(*) FROM board WHERE kind = 'epic' AND id = ?",
                                             (slug,)).fetchone()[0]
    a, b = store.create_epic("Dam Money"), store.create_epic("Water Rights")
    store.save_mission(Mission(slug="m1", topic="One", epic=a.slug))
    store.save_mission(Mission(slug="m2", topic="Two", epic=b.slug))
    board.layout()
    assert rows(a.slug) == 1
    orchestrator.merge_epic(a.slug, b.slug, NOW)
    assert rows(a.slug) == 0
    p = store.create_epic("Permit Money")
    with store.conn() as c:
        c.execute("UPDATE epics SET status = 'proposed' WHERE slug = ?", (p.slug,))
    board.layout()
    assert rows(p.slug) == 1
    orchestrator._drop_empty_proposals()
    assert rows(p.slug) == 0

def test_a_failed_board_turn_counts_and_keeps_the_ideas(monkeypatch):
    small_desk()
    store.save_theory({"at": "2026-01-01T00:00:00+00:00", "turn": 2, "kind": "ideas", "patterns": ["P"]})
    def boom(now): raise RuntimeError("no model")
    monkeypatch.setattr(agents, "tidy_board", boom)
    assert orchestrator.think(NOW) is None
    last = store.last_theory()
    assert (last["turn"], last["kind"], last["failed"], last["ideas"]["patterns"]) == (3, "board", "RuntimeError: no model", ["P"])

def test_the_notes_say_what_a_board_turn_moved(client):
    small_desk()
    store.save_theory({"at": "2026-11-02T09:00:00+00:00", "turn": 3, "kind": "board", "why": "Grouped the grid.",
                       "moved": [{"id": "m:b", "x": 1, "y": 2, "size": 1.2}], "notes": [],
                       "ideas": {"at": "2026-11-01T09:00:00+00:00", "turn": 2, "kind": "ideas", "patterns": ["Old pattern"]}})
    html = client.get("/").text
    assert "tidied the" in html and "moved 1 note, resizing 1" in html and "Grouped the grid." in html
    assert "Old pattern" in html
    import status_tools
    text = status_tools.theorist_notes()
    assert "tidied the cork board, moving 1 note(s)" in text and "Old pattern" in text

# ---------- review fixes (#71) ----------

def test_a_slugs_words_in_prose_are_not_a_mention():
    store.save_mission(Mission(slug="the-state-of-open-source", topic="The state of open source AI funding in 2026"))
    store.save_mission(Mission(slug="lic", topic="Licences"))
    store.save_artifact("lic", "brief", brief("The state of open source licensing is shifting.", ["https://a.org"]))
    assert board.find_mentions("lic") == 0
    store.save_artifact("lic", "brief", brief("See the-state-of-open-source for the money.", ["https://a.org"]))
    assert board.find_mentions("lic") == 1                                  # the slug as written still counts

def test_one_broken_artifact_never_stops_the_backfill(client):
    store.save_mission(Mission(slug="a", topic="Alpha"))
    store.save_mission(Mission(slug="b", topic="Beta"))
    store.save_mission(Mission(slug="bad", topic="Bad"))
    store.save_artifact("a", "brief", brief("A.", ["https://ex.com/p"]))
    store.save_artifact("b", "brief", brief("B.", ["https://ex.com/p"]))
    with store.conn() as c:                                                 # a brief from an older schema
        c.execute("INSERT OR REPLACE INTO artifacts VALUES (?, ?, ?)", ("bad", "brief", '{"thesis": "only"}'))
    assert board.find_all_mentions() == 1                                   # a and b still found
    ui._MENTIONS_FOUND = False
    assert client.get("/board").status_code == 200

def test_a_drag_keeps_the_note_growing_and_odd_numbers_are_refused(client):
    store.save_mission(Mission(slug="q", topic="Queued"))
    assert client.post("/board/move", json={"id": "m:q", "x": 1e308, "y": 0}).json()["x"] == board.MAX_POS
    assert client.post("/board/move", json={"id": "m:q", "x": True, "y": 0}).status_code == 400
    m = store.get_mission("q")
    m.state = State.DONE
    store.save_mission(m)
    assert note(board.layout(), "m:q")["size"] >= board.STAGE_SIZE[State.DONE]   # no size was fixed by the drag

def test_a_mission_combined_later_moves_beside_its_new_home():
    far = store.create_epic("Far Away")
    store.save_mission(Mission(slug="big", topic="The big question"))
    for i in range(8):
        store.create_epic(f"Filler {i}")
    store.save_mission(Mission(slug="small", topic="A small question", epic=far.slug))
    before = board.layout()
    assert gap(note(before, "m:small"), note(before, "m:big")) > 2.5 * board.SLOT
    m = store.get_mission("small")
    m.state, m.combined_into = State.COMBINED, "big"
    store.save_mission(m)
    data = board.layout()
    assert gap(note(data, "m:small"), note(data, "m:big")) <= 2.5 * board.SLOT

def test_the_board_data_cannot_end_its_script(client):
    store.save_mission(Mission(slug="x", topic="Odd <!--<script> title </script>"))
    html = client.get("/board").text
    data = html.split('id="board-data">')[1].split("</script>")[0]
    assert "<" not in data and "\\u003c!--" in data

# ---------- loose ends (#74) ----------

def test_a_rejected_proposed_epic_leaves_no_board_row():
    store.save_mission(Mission(slug="a", topic="One"))
    epic = store.create_epic("Permit Money")
    with store.conn() as c:
        c.execute("UPDATE epics SET status = 'proposed' WHERE slug = ?", (epic.slug,))
    board.layout()
    rows = lambda: store.conn().execute("SELECT COUNT(*) FROM board WHERE kind = 'epic' AND id = ?",
                                        (epic.slug,)).fetchone()[0]
    assert rows() == 1
    orchestrator.reject_epic(epic.slug, None)
    assert rows() == 0

def test_api_board_survives_an_epic_added_mid_read(client, monkeypatch):
    grid = store.create_epic("Grid Power")
    store.save_mission(Mission(slug="texas", topic="Texas grid rule", epic=grid.slug))
    real = store.all_epics
    calls = []
    def later():                             # place() reads first; by the next read a new epic has landed
        calls.append(1)
        if len(calls) == 2:
            store.create_epic("Ocean Data")
        return real()
    monkeypatch.setattr(store, "all_epics", later)
    r = client.get("/api/board")
    assert r.status_code == 200
    ids = {n["id"] for n in r.json()["notes"]}
    assert f"e:{grid.slug}" in ids and "m:texas" in ids

def test_settings_saves_a_custom_string_colour(client):
    import settings
    page = client.get("/settings").text
    assert 'name="own"' in page and "Your own" in page
    client.post("/settings/board-style", data={"surface": "cork", "cards": "sticky", "string": "string",
                                               "own": "#12ab34"})
    assert settings.board_style()["color"] == "#12ab34"
    assert 'class="swatch own set"' in client.get("/settings").text
    client.post("/settings/board-style", data={"surface": "cork", "cards": "sticky", "string": "string",
                                               "color": "#c0242b", "own": "#12ab34"})
    assert settings.board_style()["color"] == "#c0242b"         # a ticked preset wins over the picker
