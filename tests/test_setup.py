"""First launch: a fresh folder, the setup check, the first-run card, an honest loop status and personalization."""
import html as htmllib, json, shutil, socket, stat, sys
from pathlib import Path
import httpx, pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner
import ollama
from types import SimpleNamespace as NS
import agents, doctor, orchestrator, run as cli, search_sources, settings, status_tools, store, ui
from agents import assistant
from agents import finder, llm, publisher, toolbox
from contracts import Mission

FIXTURE = Path(__file__).parent / "fixtures" / "echo_server.py"

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(orchestrator, "LOCK_FILE", Path("data/loop.lock"))
    monkeypatch.setattr(orchestrator, "WAKE_FLAG", Path("data/WAKE"))
    return TestClient(ui.app)

@pytest.fixture
def no_models(monkeypatch):
    """A fresh clone: no model chosen anywhere."""
    monkeypatch.delenv("MISSION_LOOP_SEARCH_MODEL")
    monkeypatch.delenv("MISSION_LOOP_MODEL")

def brave(status: int):
    return httpx.MockTransport(lambda request: httpx.Response(status, json={"web": {"results": []}}))

def ready_to_go(monkeypatch):
    settings.save_search_key("brave-key")
    store.save_mission(Mission(slug="m", topic="A question"))

# ---------- a fresh folder ----------

def test_every_command_finds_the_database_ready(no_models):
    store.DB.unlink()
    result = CliRunner().invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.output
    assert CliRunner().invoke(cli.app, ["epics"]).exit_code == 0

def test_run_with_no_model_says_why_instead_of_crashing(no_models, monkeypatch):
    monkeypatch.setattr(agents, "ready", llm.ready)
    store.DB.unlink()
    result = CliRunner().invoke(cli.app, ["run"])
    assert result.exit_code == 0 and "no model chosen" in result.output
    assert "no model chosen" in doctor.waiting()["reason"]

def test_the_ui_port_check(monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        taken = s.getsockname()[1]
        assert not cli._port_free(taken)
        result = CliRunner().invoke(cli.app, ["ui", "--port", str(taken)])
    assert result.exit_code == 1 and "already in use" in result.output and f"--port {taken + 1}" in result.output

# ---------- the setup check ----------

def test_a_fresh_clone_needs_a_model_search_and_work(no_models):
    found = {c.key: c for c in doctor.checks()}
    assert not found["models"].ok and found["models"].blocks_loop
    assert "Connect Ollama" in found["models"].fix                  # Ollama answers with models installed
    assert "connection" not in found                                 # nothing to connect until a model is chosen
    assert found["search"].ok and "DuckDuckGo" in found["search"].detail   # free search needs no key
    assert "npx" not in found and found["uvx"].ok and not found["work"].ok

def test_no_search_source_on_is_a_problem_with_the_fix():
    settings.set_search_on("ddgs", False)
    found = {c.key: c for c in doctor.checks()}
    assert not found["search"].ok and not found["search"].blocks_loop and found["search"].link == "/settings#research"
    assert "Switch DuckDuckGo on" in found["search"].fix
    settings.save_search_key("tavily-key", "tavily")
    found = {c.key: c for c in doctor.checks()}
    assert found["search"].ok and found["search"].detail == "Searches with Tavily."

def test_all_set(monkeypatch):
    ready_to_go(monkeypatch)
    found = doctor.checks()
    assert doctor.done(found), doctor.report(found)
    assert "All set" in doctor.report(found)

def test_a_model_that_is_not_pulled_says_how_to_get_it(monkeypatch):
    monkeypatch.setenv("MISSION_LOOP_MODEL", "missing:7b")
    found = {c.key: c for c in doctor.checks()}
    assert not found["connection"].ok and found["connection"].fix == "Run: ollama pull missing:7b"

def test_ollama_closed_is_a_blocking_problem(monkeypatch):
    closed = dict(llm.connection(), ok=False, models=[], error="ConnectError")
    found = {c.key: c for c in doctor.checks(conn=closed)}
    assert not found["connection"].ok and found["connection"].blocks_loop and "Ollama not answering" in found["connection"].detail

def test_missing_uvx_says_what_to_install(monkeypatch):
    monkeypatch.setattr(toolbox, "WHICH", lambda command: None)
    found = {c.key: c for c in doctor.checks()}
    assert not found["uvx"].ok and "brew install uv" in found["uvx"].fix
    assert found["search"].ok                                        # search no longer needs npx

def test_doctor_command(no_models):
    result = CliRunner().invoke(cli.app, ["doctor"])
    assert result.exit_code == 1 and "!! A model for each worker" in result.output and "ok Web search" in result.output

# ---------- tools this machine can't run are skipped ----------

async def test_toolbox_skips_a_missing_command_and_an_empty_key(tmp_path, monkeypatch):
    monkeypatch.setattr(toolbox, "WHICH", shutil.which)
    monkeypatch.delenv("ECHO_KEY", raising=False)
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"gone": {"command": "no-such-command-here"},
                               "echo": {"command": sys.executable, "args": [str(FIXTURE)], "requires": ["ECHO_KEY"]}}))
    async with toolbox.Toolbox(["gone", "echo"], config_path=cfg) as tb:
        assert tb.tools == []
        assert tb.skipped == {"gone": "no-such-command-here is not installed", "echo": "ECHO_KEY is not set"}
    monkeypatch.setenv("ECHO_KEY", "x")
    async with toolbox.Toolbox(["echo"], config_path=cfg) as tb:
        assert "echo: hi" in await tb.call("echo", {"text": "hi"}) and tb.skipped == {}

def test_search_keys_are_saved_per_source_and_privately(monkeypatch):
    assert toolbox.server_status(["search"])["search"] == ""          # the search server runs without any key
    settings.save_search_key("abc123")
    settings.save_search_key("tv-1", "tavily")
    assert settings.search_key() == "abc123" and settings.search_key("tavily") == "tv-1" and not settings.search_key("exa")
    assert stat.S_IMODE(settings.SEARCH_FILE.stat().st_mode) == 0o600
    settings.forget_search_key("brave")
    assert not settings.search_key() and settings.search_key("tavily") == "tv-1"
    monkeypatch.setenv("EXA_API_KEY", "from-env")
    assert settings.search_key("exa") == "from-env" and not settings.search_key_saved("exa")
    with pytest.raises(ValueError):
        settings.save_search_key("has spaces in it")
    with pytest.raises(ValueError):
        settings.save_search_key("k", "ddgs")                       # DuckDuckGo takes no key

def test_a_brave_key_saved_before_sources_still_works():
    settings.SEARCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    settings.SEARCH_FILE.write_text('{"key": "old-brave"}')
    assert settings.search_key("brave") == "old-brave" and settings.search_on("brave")

async def test_the_finder_says_web_search_is_off():
    class NoSearch:
        skipped = {"search": "BRAVE_API_KEY is not set"}
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): pass
    with pytest.raises(finder.FinderFailed, match="web search is off"):
        await finder.run_finder("a line", [], toolbox_factory=lambda names: NoSearch())

def test_the_key_check(monkeypatch):
    monkeypatch.setattr(search_sources, "TRANSPORT", brave(401))
    assert doctor.check_search_key("bad") == (False, "Brave Search refused that key")
    assert doctor.check_search_key("bad", "serper") == (False, "Serper refused that key")
    monkeypatch.setattr(search_sources, "TRANSPORT", brave(200))
    assert doctor.check_search_key("good") == (True, "")
    def offline(request): raise httpx.ConnectError("no network")
    monkeypatch.setattr(search_sources, "TRANSPORT", httpx.MockTransport(offline))
    ok, note = doctor.check_search_key("maybe")
    assert ok and "saved anyway" in note                             # offline: warn, don't block

# ---------- the first-run card ----------

def test_fresh_home_page_shows_get_started(client, no_models):
    html = client.get("/").text
    card = html.split('id="setup"')[1].split("</section>")[0]
    assert "Get started" in card and "2 steps left" in card          # web search works with no key
    assert 'action="/settings/ollama-connect"' in card and 'name="back" value="/"' in card
    assert 'action="/settings/search"' not in card and 'action="/inspiration"' in card
    assert 'href="/about"' in card and 'data-ask=' in card             # the About page and Researcher Chat
    assert all(htmllib.escape(line) in card for line in ui.INSPIRATIONS)
    assert html.index('id="setup"') < html.index('id="flow"')         # first thing on the page

def test_connect_ollama_from_the_card_comes_back_home(client, no_models):
    r = client.post("/settings/ollama-connect", data={"back": "/"}, follow_redirects=False)
    assert r.headers["location"] == "/?done=ollama_connected"
    assert settings.worker_model("pi") == "big-test:30b" and settings.worker_model("assistant") == "small-test:1b"

def test_the_card_hides_returns_and_goes_once_everything_is_done(client, monkeypatch):
    assert 'id="setup"' in client.get("/").text
    assert client.post("/setup/dismiss", follow_redirects=False).headers["location"] == "/?done=setup_hidden"
    assert 'id="setup"' not in client.get("/").text
    settings_page = client.get("/settings").text
    assert 'action="/setup/show"' in settings_page                    # Settings → Setup brings it back
    client.post("/setup/show")
    assert 'id="setup"' in client.get("/").text
    ready_to_go(monkeypatch)
    assert 'id="setup"' not in client.get("/").text                   # nothing left to set up

def test_saving_a_search_key_checks_it_first(client, monkeypatch):
    monkeypatch.setattr(search_sources, "TRANSPORT", brave(401))
    r = client.post("/settings/search", data={"key": "nope", "source": "brave", "back": "/"}, follow_redirects=False)
    assert r.headers["location"] == "/?error=search_key" and not settings.search_key()
    r = client.post("/settings/search", data={"key": "two words", "source": "brave"}, follow_redirects=False)
    assert r.headers["location"] == "/settings?error=search_format#search"
    r = client.post("/settings/search", data={"key": "k", "source": "ddgs"}, follow_redirects=False)
    assert "error=search_format" in r.headers["location"]           # no key for DuckDuckGo
    monkeypatch.setattr(search_sources, "TRANSPORT", brave(200))
    r = client.post("/settings/search", data={"key": "good-key", "source": "tavily", "back": "/"}, follow_redirects=False)
    assert r.headers["location"] == "/?done=search_saved" and settings.search_key("tavily") == "good-key"
    assert "Remove key" in client.get("/settings").text
    client.post("/settings/search", data={"remove": "1", "source": "tavily"})
    assert not settings.search_key("tavily")

def test_settings_lists_every_source_and_saves_order_and_switches(client):
    page = client.get("/settings").text
    order = [page.index(f"<strong>{name}</strong>") for name in
             ("Brave Search", "Tavily", "Exa", "Serper", "DuckDuckGo", "GDELT")]
    assert order == sorted(order) and "no key" in page and "news only" in page
    r = client.post("/settings/search/move", data={"source": "ddgs", "step": "-1"}, follow_redirects=False)
    assert "done=search_order" in r.headers["location"]
    assert settings.search_order()[3:5] == ["ddgs", "serper"]
    client.post("/settings/search/toggle", data={"source": "ddgs", "on": ""})
    assert not settings.search_on("ddgs") and settings.search_sources_on("web") == []
    assert settings.search_sources_on("news") == ["gdelt"]
    client.post("/settings/search/toggle", data={"source": "ddgs", "on": "1"})
    assert settings.search_sources_on("web") == ["ddgs"]

# ---------- an honest loop ----------

def test_play_refuses_with_the_reason_when_the_loop_cant_work(client, no_models, monkeypatch):
    started = []
    monkeypatch.setattr(ui, "start_loop", lambda every: started.append(every))
    monkeypatch.setattr(agents, "ready", llm.ready)
    r = client.post("/loop/start", data={"back": "/"}, follow_redirects=False)
    assert r.headers["location"] == "/?error=notready" and started == []
    toast = client.get("/?error=notready").text.split('class="flash err">')[1].split("</div>")[0]
    assert htmllib.unescape(toast).startswith("The loop can't start yet: no model chosen for the Research Assistant")

def test_add_and_start_adds_the_mission_but_only_starts_a_loop_that_can_work(client, monkeypatch):
    started = []
    monkeypatch.setattr(ui, "start_loop", lambda every: started.append(every))
    monkeypatch.setattr(agents, "ready", lambda: (False, "Ollama is not reachable"))
    r = client.post("/missions", data={"topic": "Who paid for it?", "slug": "paid", "go": "start"}, follow_redirects=False)
    assert r.headers["location"] == "/m/paid?error=notready" and store.get_mission("paid") and started == []

def test_a_running_loop_that_cant_work_shows_waiting(client, monkeypatch):
    monkeypatch.setattr(ui, "loop_running", lambda: True)
    doctor.set_waiting("Ollama is not reachable at http://127.0.0.1:11434 (ConnectError); start the Ollama app")
    html = client.get("/").text
    nav = html.split('<nav class="top">')[1].split("</nav>")[0]
    assert ">Waiting</span>" in nav and "Running" not in nav and "start the Ollama app" in nav
    assert "Loop waiting: Ollama is not reachable" in html
    doctor.clear_waiting()
    assert ">Running</span>" in client.get("/").text

def test_the_loop_records_why_it_waits_once_and_clears_it(monkeypatch, capsys):
    monkeypatch.setattr(agents, "ready", lambda: (False, "no model chosen"))
    assert not cli._ready(quiet_repeat=True) and not cli._ready(quiet_repeat=True)
    assert capsys.readouterr().out.count("waiting: no model chosen") == 1   # said once, not every 30 seconds
    monkeypatch.setattr(agents, "ready", lambda: (True, "ok"))
    assert cli._ready() and doctor.waiting() is None

def test_researcher_chat_can_check_the_setup(no_models):
    text = status_tools.setup_status()
    assert "TO DO: A model for each worker" in text and "Get started" in text

# ---------- About and personalization ----------

def test_about_works_before_anything_is_published(client):
    r = client.get("/about", follow_redirects=False)
    assert r.headers["location"] == "/site/about/"
    assert client.get("/site/about/").status_code == 200
    assert 'href="/about"' in client.get("/settings").text             # in the top bar on every page

def test_personalizing_the_publication(client):
    statement = ("The Mission Loop Agent Group is an independent multi-agent team.\n\n"
                 "Half-truths break trust.")
    r = client.post("/settings/publication", data={"name": "Mission Loop Agent Group", "tagline": "Reading between the lines",
                                                   "about": statement}, follow_redirects=False)
    assert r.headers["location"] == "/settings?done=publication_saved#publication"
    index = (publisher.SITE / "index.html").read_text()
    assert "<title>Mission Loop Agent Group</title>" in index and "Reading between the lines" in index
    about = (publisher.SITE / "about" / "index.html").read_text()
    assert "<p>The Mission Loop Agent Group is an independent multi-agent team.</p><p>Half-truths break trust.</p>" in about
    assert json.loads((publisher.SITE / "feed.json").read_text())["title"] == "Mission Loop Agent Group"
    assert 'value="Mission Loop Agent Group"' in client.get("/settings").text
    r = client.post("/settings/publication", data={"name": "x" * 61}, follow_redirects=False)
    assert r.headers["location"] == "/settings?error=publication#publication"
    assert settings.publication_name() == "Mission Loop Agent Group"
    client.post("/settings/publication", data={"name": "", "tagline": "", "about": ""})
    assert settings.publication_name() == settings.PUBLICATION_DEFAULT and settings.saved().get("publication_name") is None

# ---------- Researcher Chat on a fresh clone ----------

def test_the_chat_answers_before_any_worker_has_a_model(no_models, monkeypatch):
    class Fake:                                                     # stands in for ollama.Client
        def __init__(self, host=None, timeout=None): pass
        def list(self): return NS(models=[NS(model=n) for n in ("embed:latest", "chatty:4b")])
        def show(self, n): return NS(capabilities=["tools"] if n == "chatty:4b" else ["embedding"])
    monkeypatch.setattr(ollama, "Client", Fake)
    assert assistant.default_model() == "chatty:4b"                 # was "": every question failed validation
    class Down(Fake):
        def list(self): raise ConnectionError("no ollama")
    monkeypatch.setattr(ollama, "Client", Down)
    assert assistant.default_model() == "" and assistant.chat("How do I set up?") == assistant.NO_MODEL

def test_a_new_loop_logs_a_reason_an_earlier_one_left(monkeypatch, capsys):
    doctor.set_waiting("no model chosen")                          # a refused play or a killed loop left it
    monkeypatch.setattr(agents, "ready", lambda: (False, "no model chosen"))
    real = cli._ready
    def once(**k):
        r = real(**k)
        orchestrator.STOP_FLAG.parent.mkdir(parents=True, exist_ok=True)
        orchestrator.STOP_FLAG.touch()                              # one pass is enough
        return r
    monkeypatch.setattr(cli, "_ready", once)
    cli.loop(every=15)
    assert "waiting: no model chosen" in capsys.readouterr().out

def test_stubs_need_no_model(monkeypatch, no_models):
    monkeypatch.setenv("MISSION_LOOP_STUBS", "1")
    found = doctor.checks()
    assert found[0].key == "models" and found[0].ok and not any(c.key == "connection" for c in found)
