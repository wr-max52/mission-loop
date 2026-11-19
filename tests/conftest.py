import pytest
import agents, orchestrator, store
from agents import stubs

@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    """Every test gets its own database, stop flag and working folder."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(store, "DB", tmp_path / "data" / "loop.db")
    monkeypatch.setattr(orchestrator, "STOP_FLAG", tmp_path / "data" / "STOP")
    store.init()

@pytest.fixture
def stub_agents(monkeypatch):
    """Route every model-backed agent to stubs so loop tests never touch Ollama or the network.
    publish stays real: it only writes files into the test's own folder."""
    for name in ("find_missions", "research", "validate", "interview", "write", "check_article"):
        monkeypatch.setattr(agents, name, getattr(stubs, name))
