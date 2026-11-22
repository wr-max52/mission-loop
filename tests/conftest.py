import httpx, pytest
import agents, orchestrator, store
from agents import bluesky, f916, stubs

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
    for var in ("BLUESKY_HANDLE", "BLUESKY_APP_PASSWORD", "BLUESKY_SERVICE"):
        monkeypatch.delenv(var, raising=False)           # a real .env must never switch Bluesky on in tests
    store.init()

@pytest.fixture
def stub_agents(monkeypatch):
    """Route every model-backed agent to stubs so loop tests never touch Ollama or the network.
    publish stays real: it only writes files into the test's own folder."""
    for name in ("find_missions", "research", "validate", "interview", "write", "check_article"):
        monkeypatch.setattr(agents, name, getattr(stubs, name))
