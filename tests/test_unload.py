"""#107: the loop's Ollama models unload once nothing wants them, instead of staying loaded for 24 hours."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import ollama
import settings, ui
from agents import llm

NOW = datetime(2026, 11, 22, 18, 0, tzinfo=timezone.utc)

class FakeOllama:
    """ps() lists what is loaded, with when each unloads; generate() records the keep_alive it was sent."""
    def __init__(self, held: dict[str, timedelta]):
        self.held = {name: NOW + left for name, left in held.items()}
        self.sent: list[tuple[str, object]] = []

    def ps(self):
        return SimpleNamespace(models=[SimpleNamespace(model=n, expires_at=t) for n, t in self.held.items()])

    def generate(self, model, keep_alive=None):
        self.sent.append((model, keep_alive))
        if keep_alive == 0:
            self.held.pop(model, None)

def test_release_sets_only_loaded_models_to_unload_in_five_minutes(monkeypatch):
    fake = FakeOllama({"big-test:30b": timedelta(hours=24), "someone-elses:7b": timedelta(hours=24)})
    monkeypatch.setattr(llm, "CLIENT", fake)
    assert llm.release() == ["big-test:30b"]
    assert fake.sent == [("big-test:30b", "300s")]          # small-test:1b isn't loaded: never loaded by a release

def test_unload_pinned_leaves_other_apps_and_short_timers_alone(monkeypatch):
    fake = FakeOllama({"big-test:30b": timedelta(hours=23),       # a worker's, held by an old 24-hour call
                       "small-test:1b": timedelta(minutes=4),     # a worker's, already going by itself
                       "someone-elses:7b": timedelta(hours=23)})  # another app's
    monkeypatch.setattr(llm, "CLIENT", fake)
    assert llm.unload_pinned(NOW) == ["big-test:30b"]
    assert fake.sent == [("big-test:30b", 0)]
    assert set(fake.held) == {"small-test:1b", "someone-elses:7b"}

def test_a_model_picked_in_chat_counts_as_ours(monkeypatch):
    fake = FakeOllama({"picked-in-chat:8b": timedelta(hours=20)})
    monkeypatch.setattr(llm, "CLIENT", fake)
    assert llm.unload_pinned(NOW) == []
    llm.USED.add("picked-in-chat:8b")
    assert llm.unload_pinned(NOW) == ["picked-in-chat:8b"]

def test_cloud_models_and_stub_runs_are_never_ours(monkeypatch):
    settings.save(pi_model="anthropic/claude-test")
    assert "claude-test" not in llm.ours() and "anthropic/claude-test" not in llm.ours()
    monkeypatch.setenv("MISSION_LOOP_STUBS", "1")
    assert llm.ours() == set()

def test_ollama_closed_never_raises():
    assert llm.loaded() == {} and llm.unload(["big-test:30b"]) == [] and llm.release() == []

def test_the_ui_unloads_once_the_loop_has_been_off_five_minutes(monkeypatch):
    fake = FakeOllama({"big-test:30b": timedelta(hours=24)})
    monkeypatch.setattr(llm, "CLIENT", fake)
    monkeypatch.setattr(ui, "loop_running", lambda: False)
    off = ui.idle_check(None, 1000.0)
    assert off == 1000.0 and fake.sent == []
    assert ui.idle_check(off, 1000.0 + llm.IDLE_UNLOAD - 1) == off and fake.sent == []
    ui.idle_check(off, 1000.0 + llm.IDLE_UNLOAD)
    assert fake.sent == [("big-test:30b", 0)]
    monkeypatch.setattr(ui, "loop_running", lambda: True)
    assert ui.idle_check(off, 5000.0) is None                 # running again: the clock starts over

def test_chat_asks_for_five_minutes_and_the_loop_for_its_rest(monkeypatch):
    seen = []

    class FakeAsync:
        def __init__(self, host=None):
            pass

        async def chat(self, **kw):
            seen.append(kw["keep_alive"])
            return SimpleNamespace(message=ollama.Message(role="assistant", content="hi"), done_reason="stop")

    monkeypatch.setattr(ollama, "AsyncClient", FakeAsync)
    asyncio.run(llm.chat([{"role": "user", "content": "hi"}], model="chat-test:4b", stoppable=False))
    monkeypatch.setattr(llm, "KEEP_ALIVE", 15 * 60 + 60)       # run.py loop --every 15
    asyncio.run(llm.chat([{"role": "user", "content": "hi"}], model="big-test:30b"))
    assert seen == ["300s", "960s"]
    assert {"chat-test:4b", "big-test:30b"} <= llm.USED and llm.LAST_CALL > 0

def test_a_full_rest_with_nothing_for_a_model_unloads(monkeypatch):
    import run
    fake = FakeOllama({"big-test:30b": timedelta(minutes=6)})
    monkeypatch.setattr(llm, "CLIENT", fake)
    monkeypatch.setattr(llm, "LAST_CALL", 50.0)
    assert run._unload_after_rest(False, 100.0) == []          # the rest was cut short by a UI action
    assert run._unload_after_rest(True, 40.0) == []            # the sweep after it called a model
    assert fake.sent == []
    assert run._unload_after_rest(True, 100.0) == ["big-test:30b"]
    assert fake.sent == [("big-test:30b", 0)]
