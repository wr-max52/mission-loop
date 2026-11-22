"""The desk's library: passages from the desk's own work, ranked by words (BM25) and, with an embedding model, by
meaning; its MCP server; the Theorist's lookups; and the chat assistant doing whatever the Theorist does."""
import asyncio, json
from datetime import datetime, timezone
from pathlib import Path
import httpx, pytest
from ollama import Message
from starlette.testclient import TestClient
from typer.testing import CliRunner
import library, orchestrator, run as cli, settings, status_tools, store, trail, ui
from agents import assistant, llm, stubs, theorist
from agents.theorist import NewCombine, Theory, TheoryReply
from agents.toolbox import Toolbox
from contracts import Article, Claim, Mission, ResearchBrief, Section, Source, State

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
LINE = "Who really decides what the Federal Reserve does?"

@pytest.fixture(autouse=True)
def flags(monkeypatch):
    monkeypatch.setattr(orchestrator, "LOCK_FILE", Path("data/loop.lock"))
    monkeypatch.setattr(orchestrator, "WAKE_FLAG", Path("data/WAKE"))
    monkeypatch.setattr(orchestrator, "THINK_FLAG", Path("data/THINK"))
    yield
    trail.stop()

def brief(thesis, claim, quote, url="https://a.example.com/memo"):
    return ResearchBrief(thesis=thesis, counterpoint="The bank denies it. No source found makes this case.",
                         sources=[Source(id="s1", url=url, title="Memo", quote=quote, stance="supports"),
                                  Source(id="s2", url="https://b.example.com/denial", title="Denial",
                                         quote="We never paid anyone.", stance="contradicts")],
                         claims=[Claim(text=claim, source_ids=["s1"])])

def desk():
    """Three missions in two epics: one published with a brief and an article, one with a brief, one bare."""
    fed, water = store.create_epic("Fed Capture"), store.create_epic("Water Rights")
    store.save_mission(Mission(slug="fed-a", topic="Who wrote the 2019 rate memo?", epic=fed.slug,
                               objective="Find the memo's author.", state=State.DONE, published_at=T0))
    store.save_artifact("fed-a", "brief", brief("A lobbyist drafted the 2019 rate memo.",
                                                "The lobbyist's firm received donations from three regional banks.",
                                                "The firm received donations from three regional banks."))
    store.save_artifact("fed-a", "article", Article(title="The memo's ghostwriter", dek="A lobbyist's hand.", sections=[
        Section(heading="The memo", paragraphs=["The 2019 memo was drafted outside the Fed [s1]."]),
        Section(heading="The denial", paragraphs=["The bank denies paying anyone [s2]."])]))
    store.save_mission(Mission(slug="dam", topic="Who profits from the new dam permits?", epic=water.slug))
    store.save_artifact("dam", "brief", brief("A water district fast-tracked the dam permits.",
                                              "The district's board approved the permits in one meeting.",
                                              "The board approved the permits in a single meeting.",
                                              "https://c.example.com/board"))
    store.save_mission(Mission(slug="grid", topic="Why did the grid fail in July?"))

# ---------- what goes in ----------

def test_passages_cover_the_desks_own_work():
    desk()
    kinds = {(p.slug, p.kind) for p in library.documents()}
    assert {("fed-a", k) for k in ("mission", "thesis", "claim", "counterpoint", "source", "article", "section")} <= kinds
    assert ("grid", "mission") in kinds and ("grid", "claim") not in kinds
    claim = next(p for p in library.documents() if p.slug == "fed-a" and p.kind == "claim")
    assert '[s1] "The firm received donations from three regional banks."' in claim.text   # the quote it cites
    assert claim.url == "https://a.example.com/memo" and claim.label == "brief claim 1 (fact)"
    uncited = next(p for p in library.documents() if p.slug == "fed-a" and p.kind == "source")
    assert "We never paid anyone." in uncited.text and "contradicts" in uncited.label

def test_long_sections_split_into_passages():
    store.save_mission(Mission(slug="long", topic="A long story"))
    paras = [f"Paragraph {i} " + "word " * 60 for i in range(8)]
    store.save_artifact("long", "article", Article(title="T", dek="D", sections=[
        Section(heading="Big", paragraphs=paras), Section(heading="Small", paragraphs=["Short."])]))
    parts = [p for p in library.documents() if p.kind == "section" and "Big" in p.label]
    assert len(parts) > 1 and all(len(p.text) < library.CHUNK_CHARS + 400 for p in parts)
    assert parts[1].label == "article section: Big (part 2)"

def test_the_index_follows_the_desk():
    desk()
    found, vectors, note = library.sync()
    assert vectors == {} and note == "" and library.status()["passages"] == len(found)
    store.save_mission(Mission(slug="new", topic="Who owns the toll roads?"))
    m = store.get_mission("grid")
    m.objective = "Find the operator's logs."
    store.save_mission(m)
    found2, _, _ = library.sync()
    assert any(p.slug == "new" for p in found2) and library.status()["passages"] == len(found2)
    grid = [p for p in found2 if p.slug == "grid"]
    assert len(grid) == 1 and "operator's logs" in grid[0].text          # the changed passage replaced the old one

# ---------- ranking by words ----------

def test_search_by_words_ranks_the_right_passage_first():
    desk()
    hits, note = library.search("regional banks donations")
    assert hits[0].passage.slug == "fed-a" and hits[0].by == "words" and note == ""
    assert len(hits) <= settings.library_top_n() == 5

def test_top_n_is_the_settings_pick_and_filters_work():
    desk()
    settings.set_library_top_n(3)
    assert settings.library_top_n() == 3 and len(library.search("permits memo board bank")[0]) == 3
    with pytest.raises(ValueError):
        settings.set_library_top_n(4)                                        # not one of the choices
    assert {h.passage.slug for h in library.search("permits memo", epic="Water Rights")[0]} == {"dam"}
    assert {h.passage.slug for h in library.search("the memo", mission="fed-a")[0]} == {"fed-a"}
    assert library.search("memo", epic="Nope")[1] == "no epic called Nope"

def test_no_mission_takes_more_than_half_while_others_match():
    desk()
    settings.set_library_top_n(8)
    hits, _ = library.search("memo lobbyist bank permits board grid fail")
    slugs = [h.passage.slug for h in hits]
    assert slugs.count("fed-a") <= 4 + max(0, 8 - len(library.documents()))
    assert {"dam", "grid"} <= set(slugs)

def test_render_names_mission_stage_epic_and_place():
    desk()
    text = library.ask("regional banks donations")
    assert text.startswith("Library: ") and "ranked by words (no embedding model is set)" in text
    assert "1. [fed-a] Who wrote the 2019 rate memo? · published · epic Fed Capture · brief claim 1 (fact)" in text
    assert "No passage matches" in library.ask("zebra xylophone")
    assert library.ask("   ") == "Give a few words to look for."

# ---------- ranking by meaning ----------

CONCEPTS = [{"money", "cash", "donation", "donations", "paid", "funds"}, {"water", "dam", "river", "permits"},
            {"fed", "federal", "reserve", "memo", "rate"}, {"grid", "power", "outage", "fail"}]

def vector(text):
    words = set(text.lower().replace(".", " ").replace("?", " ").split())
    return [len(words & c) + 0.01 for c in CONCEPTS]

class Embedder:
    """A fake embeddings API: Ollama's /api/embed or the OpenAI shape's /embeddings."""
    def __init__(self, fail=False):
        self.requests, self.fail = [], fail
    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append((str(request.url), request.headers.get("authorization"), body))
        if self.fail:
            return httpx.Response(404, json={"error": f"model \"{body['model']}\" not found"})
        vectors = [vector(t) for t in body["input"]]
        if request.url.path.endswith("/api/embed"):
            return httpx.Response(200, json={"embeddings": vectors})
        return httpx.Response(200, json={"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]})

def use(monkeypatch, embedder):
    monkeypatch.setattr(library, "TRANSPORT", httpx.MockTransport(embedder))

def test_meaning_finds_what_shares_no_word(monkeypatch):
    desk()
    use(monkeypatch, fake := Embedder())
    settings.set_embed_model("embed-test")
    hits, note = library.search("cash")                                  # no passage says cash
    assert note == "" and hits and hits[0].by == "meaning"
    assert "donations" in hits[0].passage.text
    url, _, body = fake.requests[0]
    assert url == "http://127.0.0.1:11434/api/embed" and body["model"] == "embed-test"
    assert library.status()["embedded"] == len(library.documents())

def test_each_passage_is_embedded_once_and_again_for_a_new_model(monkeypatch):
    desk()
    use(monkeypatch, fake := Embedder())
    settings.set_embed_model("embed-test")
    library.search("memo")
    first = sum(len(b["input"]) for _, _, b in fake.requests)
    library.search("dam")
    assert sum(len(b["input"]) for _, _, b in fake.requests) == first + 1   # only the query this time
    store.save_mission(Mission(slug="new", topic="Who owns the toll roads?"))
    library.search("roads")
    assert fake.requests[-2][2]["input"] == ["Who owns the toll roads?\nWho owns the toll roads?"]
    settings.set_embed_model("embed-other")
    library.sync()
    n = len(library.documents())
    assert library.status() == {"passages": n, "missions": 4, "embedded": n, "model": "embed-other"}

def test_a_failing_model_ranks_by_words_and_says_why(monkeypatch):
    desk()
    use(monkeypatch, Embedder(fail=True))
    settings.set_embed_model("missing-embed")
    hits, note = library.search("regional banks donations")
    assert hits[0].passage.slug == "fed-a" and hits[0].by == "words"
    assert note.startswith("ranked by words only: Ollama refused the embedding model missing-embed (HTTP 404")
    assert "Note: ranked by words only" in library.ask("regional banks")

def test_embedding_time_is_spread_over_searches(monkeypatch):
    desk()
    use(monkeypatch, Embedder())
    monkeypatch.setattr(library, "BATCH", 2)
    _, vectors, note = library.sync("embed-test", time_limit=-1)        # no time at all: nothing embedded yet
    assert vectors == {} and "not embedded yet; they rank by words" in note
    _, vectors, note = library.sync("embed-test")
    assert len(vectors) == len(library.documents()) and note == ""

def test_cloud_embeddings_use_the_providers_key(monkeypatch):
    desk()
    use(monkeypatch, fake := Embedder())
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    settings.set_embed_model("openai/text-embedding-test")
    assert library.search("cash")[0][0].by == "meaning"
    url, auth, body = fake.requests[0]
    assert url == "https://api.openai.com/v1/embeddings" and auth == "Bearer sk-test"
    assert body["model"] == "text-embedding-test"
    assert library.check("openai/text-embedding-test") == len(CONCEPTS)

def test_providers_without_embeddings_are_refused(monkeypatch):
    with pytest.raises(ValueError, match="Anthropic offers no embedding models"):
        settings.set_embed_model("anthropic/some-model")
    assert "anthropic" not in settings.embed_providers() and settings.embed_providers() == ["ollama"]
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert settings.embed_providers() == ["ollama", "openai"]
    with pytest.raises(library.LibraryError, match="xAI isn't connected"):
        library.embed(["x"], "xai/embed-x")

def test_embedding_model_is_config(monkeypatch):
    assert settings.embed_model() == "" and library.embed_model() == ""
    monkeypatch.setenv("MISSION_LOOP_EMBED_MODEL", "env-embed")
    assert settings.embed_model() == "env-embed"
    settings.set_embed_model("saved-embed")
    assert settings.embed_model() == "saved-embed"                         # the Settings page wins over .env
    monkeypatch.setenv("MISSION_LOOP_STUBS", "1")
    assert library.embed_model() == ""                                     # stub runs never reach the network

def test_words_only_and_env_model_are_kept_apart(monkeypatch):
    monkeypatch.setenv("MISSION_LOOP_EMBED_MODEL", "env-embed")
    settings.set_embed_model("env-embed")                                  # the same as .env: nothing stored
    assert "embed_model" not in settings.saved() and settings.embed_model() == "env-embed"
    settings.set_embed_model(settings.WORDS_ONLY)                          # words only, though .env names a model
    assert settings.embed_model() == "" and settings.saved()["embed_model"] == settings.WORDS_ONLY
    settings.set_embed_model("")
    assert settings.embed_model() == "env-embed"
    monkeypatch.delenv("MISSION_LOOP_EMBED_MODEL")
    settings.set_embed_model(settings.WORDS_ONLY)                          # no .env model: nothing to override
    assert "embed_model" not in settings.saved()

def test_a_sync_with_no_model_keeps_the_vectors(monkeypatch):
    desk()
    use(monkeypatch, Embedder())
    _, vectors, _ = library.sync("embed-test")
    library.sync("")                                                       # a stub run, or a server without .env
    _, again, _ = library.sync("embed-test", time_limit=-1)
    assert again.keys() == vectors.keys()

# ---------- the MCP server ----------

async def test_library_server_runs_over_mcp():
    desk()
    async with Toolbox(["library"], config_path=assistant.CONFIG) as tb:
        assert [t["function"]["name"] for t in tb.tools] == ["search_library"]
        text = await tb.call("search_library", {"query": "regional banks donations"})
        assert "[fed-a]" in text and "brief claim 1" in text
        assert "[dam]" in await tb.call("search_library", {"query": "board", "epic": "Water Rights"})

# ---------- the Theorist looks things up ----------

class FakeLibrary:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail
        self.tools = [{"type": "function", "function": {"name": "search_library", "parameters": {}}}]
    async def __aenter__(self):
        if self.fail:
            raise RuntimeError("library server did not start")
        return self
    async def __aexit__(self, *exc): pass
    async def call(self, name, args):
        self.calls.append((name, args))
        return f"Library: passages for {args['query']}"

def tool_call(name, **args):
    return Message(role="assistant", content="", tool_calls=[
        Message.ToolCall(function=Message.ToolCall.Function(name=name, arguments=args))])

def script(monkeypatch, replies):
    seen, it = [], iter(replies)
    async def fake_chat(messages, tools=None, format=None, model=None):
        seen.append({"messages": list(messages), "tools": tools, "format": format})
        r = next(it)
        return r if isinstance(r, Message) else Message(role="assistant", content=r.model_dump_json())
    monkeypatch.setattr(llm, "chat", fake_chat)
    return seen

def theory_reply():
    return TheoryReply(patterns=["fed-a: donations from regional banks"], inspiration=LINE, why="Kept.")

async def think(box):
    return await theorist.run_theorist(store.get_inspiration(), store.inspiration_history(), store.all_missions(),
                                       T0, toolbox_factory=lambda: box)

async def test_theorist_searches_the_library_before_it_answers(monkeypatch):
    desk()
    store.set_inspiration(LINE)
    box = FakeLibrary()
    seen = script(monkeypatch, [tool_call("search_library", query="regional bank donations", epic="Fed Capture"),
                                tool_call("fetch", url="https://x.example.com"),       # not a library tool
                                Message(role="assistant", content="done"), theory_reply()])
    theory = await think(box)
    assert box.calls == [("search_library", {"query": LINE}),                          # code's own first search
                         ("search_library", {"query": "regional bank donations", "epic": "Fed Capture"})]
    assert f"Library: passages for {LINE}" in seen[0]["messages"][1]["content"]      # shown before it searches
    assert seen[0]["tools"] == box.tools and seen[0]["format"] is None
    assert seen[2]["messages"][-1]["content"].startswith("Not run: only search_library")
    final = seen[3]
    assert final["format"] is not None and final["tools"] is None and len(final["messages"]) == 2
    asked = final["messages"][1]["content"]
    assert 'Search: "regional bank donations"' in asked and "Library: passages for regional bank donations" in asked
    assert asked.endswith("Give your theory as JSON matching the schema.")
    assert "The desk's library holds the work behind each one-line summary" in final["messages"][0]["content"]
    assert theory.lookups == [LINE, "regional bank donations"] and theory.library == ""

async def test_theorist_stops_looking_after_its_budget(monkeypatch):
    desk()
    store.set_inspiration(LINE)
    box = FakeLibrary()
    replies = [tool_call("search_library", query=f"q{i}") for i in range(theorist.LOOKUPS)] + [theory_reply()]
    seen = script(monkeypatch, replies)
    theory = await think(box)
    assert len(box.calls) == 1 + theorist.LOOKUPS and len(theory.lookups) == 1 + theorist.LOOKUPS
    assert seen[-1]["format"] is not None

async def test_a_broken_library_never_stops_the_theorist(monkeypatch):
    desk()
    store.set_inspiration(LINE)
    seen = script(monkeypatch, [theory_reply()])
    theory = await think(FakeLibrary(fail=True))
    assert theory.inspiration == LINE and theory.lookups == []
    assert theory.library == "library not searched: RuntimeError: library server did not start"
    assert seen[0]["messages"][1]["content"] == "Give your theory as JSON matching the schema."
    orchestrator.apply_theory(theory, T0)
    assert theory.library in store.last_theory()["notes"]

def test_findings_are_shared_out_within_their_room():
    found = [(f"q{i}", "Library: head\n" + "\n".join(f"{n}. passage " + "x" * 400 for n in range(40)))
             for i in range(4)]
    text = theorist.findings_text(found)
    assert len(text) < theorist.FINDINGS_CHARS + 400 and text.count("[more passages cut]") == 4
    assert theorist.findings_text([]) == ""

def test_lookups_show_in_the_notes():
    desk()
    store.set_inspiration(LINE)
    orchestrator.apply_theory(Theory(patterns=["p"], inspiration=LINE, based_on=LINE, lookups=[LINE, "bank donations"]), T0)
    assert store.last_theory()["lookups"] == [LINE, "bank donations"]
    assert "Searched the library for: bank donations" in status_tools.theorist_notes()
    html = TestClient(ui.app).get("/").text
    assert "It searched the library for:" in html and "<li>bank donations</li>" in html

def test_theorize_opens_the_library(monkeypatch):
    store.set_inspiration(LINE)
    got = {}
    async def fake_run(line, history, missions, now=None, toolbox_factory=None):
        got["factory"] = toolbox_factory
        return Theory(patterns=["p"], inspiration=line, based_on=line)
    monkeypatch.setattr(theorist, "run_theorist", fake_run)
    theorist.theorize(T0)
    box = got["factory"]()
    assert isinstance(box, Toolbox) and box.enabled == ["library"]

# ---------- the chat assistant can do whatever the Theorist does ----------

def test_chat_reads_the_desk_as_the_theorist_does():
    desk()
    store.set_inspiration(LINE)
    text = status_tools.desk_overview()
    assert text.startswith(f"Inspiration line: {LINE}") and "Epic: Fed Capture, 1 missions" in text
    assert "Links between missions:" in text and "Latest published articles" in text
    assert assistant._limit(object(), "desk_overview") == assistant.MAX_DESK_CHARS

def test_chat_changes_the_line_and_asks_the_theorist():
    assert "needs an inspiration line" in status_tools.ask_theorist()
    store.set_inspiration(LINE)
    evolved = "Who really decides what the Federal Reserve does, and for whom?"
    assert status_tools.set_inspiration(evolved, "the human asked") == f"Inspiration line is now: {evolved}"
    assert store.inspiration_history()[-1] == {**store.inspiration_history()[-1], "by": "chat", "why": "the human asked"}
    assert "the chat assistant, for the editor" in theorist.history_text(store.inspiration_history())
    assert "would not have written it" in status_tools.set_inspiration("Follow the money.")
    assert "at most 500" in status_tools.set_inspiration("x" * 600)
    assert "Asked." in status_tools.ask_theorist() and orchestrator.THINK_FLAG.exists()
    assert "the chat assistant:</span> Follow the money." in TestClient(ui.app).get("/").text

def test_chat_proposes_an_epic_through_the_theorists_checks():
    desk()
    assert "needs at least 2 missions" in status_tools.propose_epic("Public Money", "dam")
    assert "already exists" in status_tools.propose_epic("Fed Capture", "dam, grid")
    assert "1 to 4 word" in status_tools.propose_epic("a b c d e f", "dam, grid")
    said = status_tools.propose_epic("Public Money", "dam, grid, nope", "Both follow public money.")
    assert said.startswith("Proposed the epic Public Money with 2 mission(s)")
    epic = store.find_epic("Public Money")
    assert epic.status == "proposed" and store.get_mission("dam").epic == epic.slug
    assert store.get_mission("dam").epic_fallback == store.find_epic("Water Rights").slug
    assert store.events("dam")[-1]["note"] == ("the chat assistant proposes the new epic Public Money: "
                                               "Both follow public money.")
    status_tools.propose_epic("Grid Money", "dam, grid", "Both follow grid money.")
    assert store.find_epic("Public Money") is None                       # emptied by the second proposal: dropped

def test_chat_proposes_a_combine_and_links_missions():
    desk()
    store.save_mission(Mission(slug="dam-2", topic="Which firm won the dam contract?"))
    assert "only missions not yet being written" in status_tools.propose_combine(
        "fed-a, dam", "One question?", "Because.")
    said = status_tools.propose_combine("dam, dam-2", "Who gains from the dam, permits and contract alike?",
                                        "Both missions follow the dam's money.", "Water Rights", "Same dam.")
    assert said.startswith("Proposed combining dam, dam-2 into:")
    c = store.all_combines("proposed")[0]
    assert c.by == "chat" and c.epic == "Water Rights"
    assert "has seen this combine already" in status_tools.propose_combine("dam, dam-2", "Another?", "x")
    assert "already waiting in a proposed combine" in status_tools.propose_combine("dam, grid", "Another?", "x")
    combined = orchestrator.approve_combine(c.slug, T0)
    assert "proposed by the chat assistant" in combined.objective
    assert status_tools.link_missions("grid", "fed-a", "The same regional bank funds both.") == \
        "Linked grid <-> fed-a: The same regional bank funds both."
    link = store.get_link("grid", "fed-a")
    assert link.by == "chat" and store.events("grid")[-1]["note"].startswith("linked to fed-a by the chat assistant")
    assert "linked already" in status_tools.link_missions("fed-a", "grid", "again")
    assert "needs a reason" in status_tools.link_missions("grid", combined.slug, " ")
    assert "Linked to fed-a by the chat" in status_tools.mission_status("grid")

async def test_chat_reaches_the_library_and_the_theorists_tools():
    async with Toolbox(assistant.servers()[:2], config_path=assistant.CONFIG) as tb:
        names = {t["function"]["name"] for t in tb.tools}
    assert {"search_library", "desk_overview", "set_inspiration", "ask_theorist", "propose_epic",
            "propose_combine", "link_missions", "add_mission"} <= names
    for tool in ("search_library", "desk_overview", "propose_epic", "propose_combine", "link_missions",
                 "set_inspiration", "ask_theorist"):
        assert tool in assistant.SYSTEM

# ---------- the Settings page and the CLI ----------

@pytest.fixture
def client():
    return TestClient(ui.app)

def test_settings_page_has_the_library(client):
    desk()
    library.sync()
    html = client.get("/settings").text
    assert 'id="library"' in html and "passages from 3 missions; ranked by words only" in html
    assert '<option value="none" selected>' in html and '<option value="5" selected>5</option>' in html
    assert "Anthropic" not in html.split('id="embed_provider"')[1].split("</select>")[0]

def test_settings_saves_and_checks_the_embedding_model(client, monkeypatch):
    use(monkeypatch, fake := Embedder())
    r = client.post("/settings/library", data={"do": "save", "provider": "ollama", "model": "embed-test",
                                               "top_n": "10"}, follow_redirects=False)
    assert r.headers["location"] == "/settings?done=library_saved"
    assert settings.embed_model() == "embed-test" and settings.library_top_n() == 10 and len(fake.requests) == 1
    client.post("/settings/library", data={"do": "save", "provider": "ollama", "model": "embed-test", "top_n": "5"})
    assert len(fake.requests) == 1 and "library_top_n" not in settings.saved()    # unchanged model: no new check
    client.post("/settings/library", data={"do": "save", "provider": "none", "model": "", "top_n": "5"})
    assert settings.embed_model() == ""

def test_settings_leave_the_env_model_in_env(client, monkeypatch):
    use(monkeypatch, fake := Embedder())
    monkeypatch.setenv("MISSION_LOOP_EMBED_MODEL", "embed-test")
    client.post("/settings/library", data={"do": "save", "provider": "ollama", "model": "embed-test", "top_n": "10"})
    assert "embed_model" not in settings.saved() and not fake.requests     # only top N changed
    assert "(from MISSION_LOOP_EMBED_MODEL in .env)" in client.get("/settings").text
    client.post("/settings/library", data={"do": "save", "provider": "none", "model": "", "top_n": "10"})
    assert settings.embed_model() == ""                                    # words only wins over .env

def test_settings_refuses_a_model_that_fails_its_check(client, monkeypatch):
    use(monkeypatch, Embedder(fail=True))
    html = client.post("/settings/library", data={"do": "save", "provider": "ollama", "model": "nope",
                                                  "top_n": "5"}).text
    assert "The embedding model was not saved: Ollama refused the embedding model nope" in html
    assert settings.embed_model() == ""
    assert "pick a provider" in client.post("/settings/library", data={"do": "save", "provider": "zzz",
                                                                      "model": "m", "top_n": "5"}).text
    r = client.post("/settings/library", data={"do": "save", "provider": "none", "top_n": "7"}, follow_redirects=False)
    assert r.headers["location"] == "/settings?error=top_n"
    evil = client.post("/settings/library", data={"do": "index"}, headers={"origin": "https://evil.example"})
    assert evil.status_code == 403

def test_index_now_builds_the_index(client):
    desk()
    r = client.post("/settings/library", data={"do": "index"}, follow_redirects=False)
    assert r.headers["location"] == "/settings?done=library_indexing"
    ui.index_now().join(10)
    assert library.status()["passages"] == len(library.documents())

def test_cli_searches_and_indexes():
    desk()
    out = CliRunner().invoke(cli.app, ["library"]).output
    assert "passages from 3 missions; ranked by words only" in out
    out = CliRunner().invoke(cli.app, ["library", "regional banks donations", "--epic", "Fed Capture"]).output
    assert "1. [fed-a]" in out and "[dam]" not in out

# ---------- loose ends (#74) ----------

async def test_a_stop_before_the_lookups_skips_them():
    import stopping
    box = FakeLibrary()
    stopping.press()
    found, note = await theorist.look_up("system", LINE, lambda: box)
    assert found == [] and box.calls == [] and note == "library not searched: stop was pressed"

async def test_a_search_still_running_past_the_turn_grace_gives_up():
    import os, time, stopping
    class SlowLibrary(FakeLibrary):
        async def call(self, name, args):
            self.calls.append((name, args))
            stopping.press()
            past = time.time() - stopping.TURN_GRACE - 1          # the press was long enough ago
            os.utime(stopping.FLAG, (past, past))
            await asyncio.sleep(60)                                # a search that would run on and on
    box = SlowLibrary()
    started = time.monotonic()
    found, note = await theorist.look_up("system", LINE, lambda: box)
    assert time.monotonic() - started < 5
    assert found == [] and len(box.calls) == 1 and note == "library: stop was pressed after 0 search(es)"

def test_top_n_passages_get_room_in_chat_and_in_the_findings():
    class Box:
        server = {"search_library": assistant.LIBRARY_SERVER}
    settings.set_library_top_n(20)
    assert assistant._limit(Box(), "search_library") >= 20 * library.PASSAGE_ROOM
    assert theorist.findings_room() == min(theorist.FINDINGS_MAX, 2 * 20 * library.PASSAGE_ROOM)
    settings.set_library_top_n(3)
    assert assistant._limit(Box(), "search_library") == assistant.MAX_WEB_CHARS
    assert theorist.findings_room() == theorist.FINDINGS_CHARS

async def test_the_toolbox_keeps_the_top_n_passages_whole(monkeypatch):
    from agents import toolbox
    class Session:
        async def call_tool(self, name, args):
            return type("R", (), {"content": [type("C", (), {"type": "text", "text": "x" * 30_000})()]})()
    tb = Toolbox.__new__(Toolbox)
    tb._owner, tb.server = {"search_library": Session(), "fetch": Session()}, {"search_library": "library", "fetch": "fetch"}
    settings.set_library_top_n(20)
    assert len(await tb.call("search_library", {"query": "q"})) == 20 * library.PASSAGE_ROOM
    assert len(await tb.call("fetch", {"url": "u"})) == toolbox.MAX_TOOL_CHARS
    settings.set_library_top_n(5)
    assert len(await tb.call("search_library", {"query": "q"})) == toolbox.MAX_TOOL_CHARS
