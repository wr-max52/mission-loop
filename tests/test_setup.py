"""First launch: a fresh folder, the setup check, the first-run card, an honest loop status and personalization."""
import html as htmllib, json, shutil, socket, stat, sys
from pathlib import Path
import httpx, pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner
import agents, doctor, orchestrator, run as cli, settings, status_tools, store, ui
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
    return httpx.MockTransport(lambda request: httpx.Response(status, json={}))

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
    assert not found["search"].ok and not found["search"].blocks_loop and found["search"].link == doctor.BRAVE_KEYS
    assert found["npx"].ok and found["uvx"].ok and not found["work"].ok

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
    assert not found["connection"].ok and found["connection"].blocks_loop and "Ollama isn't answering" in found["connection"].detail

def test_missing_npx_and_uvx_say_what_to_install(monkeypatch):
    monkeypatch.setattr(toolbox, "WHICH", lambda command: None)
    found = {c.key: c for c in doctor.checks()}
    assert not found["npx"].ok and "brew install node" in found["npx"].fix
    assert not found["uvx"].ok and "brew install uv" in found["uvx"].fix

def test_doctor_command(no_models):
    result = CliRunner().invoke(cli.app, ["doctor"])
    assert result.exit_code == 1 and "!! A model for each worker" in result.output and "-- Web search key" in result.output

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

def test_the_saved_search_key_reaches_the_search_server():
    assert toolbox.server_status(["search"])["search"] == "BRAVE_API_KEY is not set"
    settings.save_search_key("abc123")
    assert toolbox._expand("${BRAVE_API_KEY}") == "abc123" and toolbox.server_status(["search"])["search"] == ""
    assert stat.S_IMODE(settings.SEARCH_FILE.stat().st_mode) == 0o600
    with pytest.raises(ValueError):
        settings.save_search_key("has spaces in it")

async def test_the_finder_says_web_search_is_off():
    class NoSearch:
        skipped = {"search": "BRAVE_API_KEY is not set"}
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): pass
    with pytest.raises(finder.FinderFailed, match="web search is off"):
        await finder.run_finder("a line", [], toolbox_factory=lambda names: NoSearch())

def test_the_brave_key_check(monkeypatch):
    monkeypatch.setattr(doctor, "TRANSPORT", brave(401))
    assert doctor.check_search_key("bad") == (False, "Brave refused that key")
    monkeypatch.setattr(doctor, "TRANSPORT", brave(200))
    assert doctor.check_search_key("good") == (True, "")
    def offline(request): raise httpx.ConnectError("no network")
    monkeypatch.setattr(doctor, "TRANSPORT", httpx.MockTransport(offline))
    ok, note = doctor.check_search_key("maybe")
    assert ok and "saved anyway" in note                             # offline: warn, don't block

# ---------- the first-run card ----------

def test_fresh_home_page_shows_get_started(client, no_models):
    html = client.get("/").text
    card = html.split('id="setup"')[1].split("</section>")[0]
    assert "Get started" in card and "3 steps left" in card
    assert 'action="/settings/ollama-connect"' in card and 'name="back" value="/"' in card
    assert 'action="/settings/search"' in card and 'action="/inspiration"' in card
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
    assert 'action="/setup/show"' in settings_page and 'id="setup"' in settings_page   # Settings → Setup
    client.post("/setup/show")
    assert 'id="setup"' in client.get("/").text
    ready_to_go(monkeypatch)
    assert 'id="setup"' not in client.get("/").text                   # nothing left to set up

def test_saving_a_search_key_checks_it_with_brave(client, monkeypatch):
    monkeypatch.setattr(doctor, "TRANSPORT", brave(401))
    r = client.post("/settings/search", data={"key": "nope", "back": "/"}, follow_redirects=False)
    assert r.headers["location"] == "/?error=search_key" and not settings.search_key()
    r = client.post("/settings/search", data={"key": "two words"}, follow_redirects=False)
    assert r.headers["location"] == "/settings?error=search_format"
    monkeypatch.setattr(doctor, "TRANSPORT", brave(200))
    r = client.post("/settings/search", data={"key": "good-key", "back": "/"}, follow_redirects=False)
    assert r.headers["location"] == "/?done=search_saved" and settings.search_key() == "good-key"
    assert "Remove key" in client.get("/settings").text
    client.post("/settings/search", data={"remove": "1"})
    assert not settings.search_key()

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
    assert "Loop is waiting: Ollama is not reachable" in html
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
