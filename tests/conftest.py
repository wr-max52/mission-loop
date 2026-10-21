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
    """Route research and validate to stubs so loop tests never touch Ollama or the network."""
    monkeypatch.setattr(agents, "research", stubs.research)
    monkeypatch.setattr(agents, "validate", stubs.validate)
