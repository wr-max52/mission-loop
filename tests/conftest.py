import httpx, pytest
import agents, orchestrator, settings, store, wayback
from agents import bluesky, custom, f916, github, llm, stubs

FAKE_CONNECTION = {"host": "http://127.0.0.1:11434", "ok": True, "error": "",
                   "models": [{"name": "small-test:1b", "size": 1_600_000_000},
                              {"name": "big-test:30b", "size": 18_000_000_000},
                              {"name": "other-test:8b", "size": 5_200_000_000}],
                   "loaded": {"big-test:30b": 21_000_000_000}}

def _no_network(request):
    raise AssertionError(f"a test tried to reach the network: {request.method} {request.url}")

@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    """Every test gets its own database, stop flag and working folder."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(store, "DB", tmp_path / "data" / "loop.db")
    monkeypatch.setattr(orchestrator, "STOP_FLAG", tmp_path / "data" / "STOP")
    monkeypatch.setattr(f916, "TRANSPORT", httpx.MockTransport(_no_network))
    monkeypatch.setattr(bluesky, "TRANSPORT", httpx.MockTransport(_no_network))
    monkeypatch.setattr(custom, "TRANSPORT", httpx.MockTransport(_no_network))
    monkeypatch.setattr(wayback, "TRANSPORT", httpx.MockTransport(_no_network))
    monkeypatch.setattr(github, "TRANSPORT", httpx.MockTransport(_no_network))
    monkeypatch.setattr(settings, "SITE_URL", "")                 # read from .env when settings is imported
    monkeypatch.setattr(settings, "GITHUB_CLIENT_ID", "")         # tests sign in with their own fake app
    for var in ("BLUESKY_HANDLE", "BLUESKY_APP_PASSWORD", "BLUESKY_SERVICE"):
        monkeypatch.delenv(var, raising=False)           # a real .env must never switch Bluesky on in tests
    monkeypatch.setattr(llm, "connection", lambda timeout=2: FAKE_CONNECTION)   # never asks a real Ollama
    for var in ("OLLAMA_HOST", "MISSION_LOOP_1F916_URL", "MISSION_LOOP_CHAT_MODEL", "MISSION_LOOP_GITHUB_URL",
                "MISSION_LOOP_GITHUB_CLIENT_ID"):
        monkeypatch.delenv(var, raising=False)           # nor may a real .env point tests at another server
    monkeypatch.setenv("MISSION_LOOP_SEARCH_MODEL", "small-test:1b")   # this test machine's config: the code has
    monkeypatch.setenv("MISSION_LOOP_MODEL", "big-test:30b")           # no models of its own
    store.init()

@pytest.fixture
def stub_agents(monkeypatch):
    """Route every model-backed agent to stubs so loop tests never touch Ollama or the network.
    publish stays real: it only writes files into the test's own folder."""
    for name in ("theorize", "find_missions", "gather", "write_brief", "validate", "interview", "write", "check_article"):
        monkeypatch.setattr(agents, name, getattr(stubs, name))
