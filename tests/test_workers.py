"""The two workers: the Research Assistant (small model) and the Principal Investigator (big model) run side by side,
each taking the next task for its role; turning one off hands its steps to the other."""
import json, threading
from datetime import datetime, timezone
from pathlib import Path
import ollama, pytest
from starlette.testclient import TestClient
import activity, agents, orchestrator, settings, stopping, store, ui
from agents import llm, stubs
from agents.researcher import _plain
from contracts import Evidence, Mission, State

pytestmark = pytest.mark.usefixtures("stub_agents")
T0 = datetime(2026, 11, 22, 9, 0, tzinfo=timezone.utc)

def writing(slug="w"):
    """A mission whose brief is approved, so its next task is the article (the PI's)."""
    store.save_mission(Mission(slug=slug, topic="Writing topic", state=State.WRITING, approved=True))
    store.save_artifact(slug, "brief", stubs.research(Mission(slug=slug, topic="Writing topic")))

def researching(slug="r"):
    """A mission whose next task is searching (the Research Assistant's)."""
    store.save_mission(Mission(slug=slug, topic="Research topic", state=State.RESEARCHING))

def test_roles_and_who_does_them():
    assert orchestrator.role(Mission(slug="a", topic="t", state=State.RESEARCHING)) == "assistant"
    assert orchestrator.role(Mission(slug="a", topic="t", state=State.RESEARCHING, gathered=True)) == "pi"
    assert orchestrator.role(Mission(slug="a", topic="t", state=State.WRITING)) == "pi"
    assert orchestrator.role(Mission(slug="a", topic="t", state=State.BRIEF_READY)) is None
    assert settings.worker_for("assistant") == "assistant" and settings.model_for("assistant") == "small-test:1b"
    assert settings.model_for("pi") == llm.model_for("pi") == "big-test:30b" and settings.parallel()

def test_both_workers_run_at_the_same_time(monkeypatch):
    """The search for one mission and the article for another wait for each other: only parallel runs pass."""
    researching(); writing()
    meet, seen = threading.Barrier(2, timeout=5), {}
    def gather(m, now=None):
        seen["gather"] = activity._worker()
        meet.wait()                        # raises BrokenBarrierError if the article isn't being written meanwhile
        return stubs.gather(m, now)
    def write(m, brief, records):
        seen["write"] = activity._worker()
        meet.wait()
        return stubs.write(m, brief, records)
    monkeypatch.setattr(agents, "gather", gather)
    monkeypatch.setattr(agents, "write", write)
    orchestrator.run_until_idle(lambda: T0)
    assert seen["gather"] == "assistant" and seen["write"] == "pi"
    assert store.get_mission("r").state == State.BRIEF_READY
    assert store.get_mission("w").state == State.ARTICLE_READY
    assert activity.working() == []

def test_a_worker_that_is_off_hands_its_steps_to_the_other(monkeypatch):
    settings.set_worker("assistant", "small-test:1b", on=False)
    assert settings.worker_for("assistant") == "pi" and settings.model_for("assistant") == "big-test:30b"
    researching(); writing()
    workers, at_once = [], []
    def gather(m, now=None):
        workers.append(activity._worker()); at_once.append(len(activity.working()))
        return stubs.gather(m, now)
    def write(m, brief, records):
        workers.append(activity._worker()); at_once.append(len(activity.working()))
        return stubs.write(m, brief, records)
    monkeypatch.setattr(agents, "gather", gather)
    monkeypatch.setattr(agents, "write", write)
    orchestrator.run_until_idle(lambda: T0)
    assert set(workers) == {"pi"} and max(at_once) == 1          # one task at a time
    assert store.get_mission("r").state == State.BRIEF_READY and store.get_mission("w").state == State.ARTICLE_READY

def test_with_the_investigator_off_the_assistant_writes_with_the_small_model(monkeypatch):
    settings.set_worker("pi", "big-test:30b", on=False)
    assert settings.worker_for("pi") == "assistant" and settings.model_for("pi") == "small-test:1b"
    researching(); writing()
    steps = []                             # (step, worker, the model its LLM calls would use)
    def gather(m, now=None):
        steps.append(("gather", activity._worker(), llm.model_for("assistant"))); return stubs.gather(m, now)
    def write_brief(m, evidence, now=None):
        steps.append(("brief", activity._worker(), llm.model_for("pi"))); return stubs.write_brief(m, evidence, now)
    def write(m, brief, records):
        steps.append(("article", activity._worker(), llm.model_for("pi"))); return stubs.write(m, brief, records)
    monkeypatch.setattr(agents, "gather", gather)
    monkeypatch.setattr(agents, "write_brief", write_brief)
    monkeypatch.setattr(agents, "write", write)
    orchestrator.run_until_idle(lambda: T0)
    assert sorted(s for s, _, _ in steps) == ["article", "brief", "gather"]
    assert {(w, model) for _, w, model in steps} == {("assistant", "small-test:1b")}
    assert store.get_mission("r").state == State.BRIEF_READY and store.get_mission("w").state == State.ARTICLE_READY

def test_sources_survive_a_stop_and_the_brief_is_written_from_them(monkeypatch):
    researching()
    def gather(m, now=None):
        stopping.press(orchestrator.STOP_FLAG)
        return Evidence(messages=[{"role": "tool", "content": "page text"}])
    monkeypatch.setattr(agents, "gather", gather)
    orchestrator.run_until_idle(lambda: T0)
    m = store.get_mission("r")
    assert m.state == State.RESEARCHING and m.gathered and m.attempts == 1
    assert store.load_artifact("r", "evidence")["messages"][0]["content"] == "page text"

    orchestrator.STOP_FLAG.unlink()
    got = []
    monkeypatch.setattr(agents, "gather", lambda m, now=None: pytest.fail("search should not rerun"))
    monkeypatch.setattr(agents, "write_brief", lambda m, ev, now=None: got.append(ev) or stubs.research(m, now))
    orchestrator.run_until_idle(lambda: T0)
    m = store.get_mission("r")
    assert got[0].messages[0]["content"] == "page text"
    assert m.state == State.BRIEF_READY and m.attempts == 1 and not m.gathered
    assert store.load_artifact("r", "evidence") is None

def test_transcript_messages_save_as_plain_json():
    msg = ollama.Message(role="assistant", content="", tool_calls=[
        ollama.Message.ToolCall(function=ollama.Message.ToolCall.Function(name="search", arguments={"q": "x"}))])
    evidence = Evidence(messages=[_plain({"role": "system", "content": "s"}), _plain(msg)])
    back = Evidence(**json.loads(evidence.model_dump_json()))
    assert back.messages[1]["tool_calls"][0]["function"] == {"name": "search", "arguments": {"q": "x"}}

def test_worker_settings():
    assert settings.saved() == {}                                  # nothing saved: models come from .env (here, the test env)
    settings.set_worker("pi", "other-test:8b", on=True)
    assert settings.worker_model("pi") == "other-test:8b" and settings.saved() == {"pi_model": "other-test:8b"}
    settings.set_worker("pi", "big-test:30b", on=False)
    assert settings.saved() == {"pi_model": "big-test:30b", "pi_worker": "off"} and settings.worker_for("pi") == "assistant"
    with pytest.raises(ValueError, match="stay on"):
        settings.set_worker("assistant", "small-test:1b", on=False)
    with pytest.raises(ValueError):
        settings.set_worker("dean", "x", on=True)
    with pytest.raises(ValueError, match="pick a model"):
        settings.set_worker("assistant", "", on=True)

def test_the_code_has_no_models_of_its_own(monkeypatch):
    """With nothing in settings.json or .env, no worker has a model and the loop refuses to start."""
    monkeypatch.delenv("MISSION_LOOP_MODEL"); monkeypatch.delenv("MISSION_LOOP_SEARCH_MODEL")
    assert settings.worker_model("assistant") == settings.worker_model("pi") == ""
    assert settings.missing_models() == ["assistant", "pi"]
    ok, why = llm.ready()
    assert not ok and "no model chosen for the Research Assistant and the Principal Investigator" in why
    settings.set_worker("pi", "big-test:30b", on=True)             # saved config wins over .env and fills the gap
    assert settings.missing_models() == ["assistant"]

class FakeOllama:
    """Stands in for ollama.Client and AsyncClient, remembering the address each was made for."""
    hosts: list[str] = []
    names = ("small-test:1b", "big-test:30b")
    def __init__(self, host=None, timeout=None):
        FakeOllama.hosts.append(host)
    def list(self):
        return type("L", (), {"models": [type("M", (), {"model": n}) for n in self.names]})
    async def chat(self, **kw):
        return type("R", (), {"message": ollama.Message(role="assistant", content="hi " + kw["model"]),
                             "done_reason": "stop"})

def test_ready_names_both_workers(monkeypatch):
    monkeypatch.setattr(ollama, "Client", FakeOllama)
    ok, why = llm.ready()
    assert ok and "Research Assistant" in why and "in parallel" in why
    settings.set_worker("pi", "missing:70b", on=True)
    ok, why = llm.ready()
    assert not ok and "missing:70b" in why and "Principal Investigator" in why

def test_every_ollama_call_goes_to_the_configured_address(monkeypatch):
    import asyncio
    FakeOllama.hosts = []
    monkeypatch.setattr(ollama, "Client", FakeOllama)
    monkeypatch.setattr(ollama, "AsyncClient", FakeOllama)
    assert settings.ollama_host() == settings.OLLAMA_DEFAULT
    monkeypatch.setenv("OLLAMA_HOST", "http://gpu-box:11434/")
    assert settings.ollama_host() == "http://gpu-box:11434"         # .env beats the standard address
    settings.set_ollama_host("http://10.0.0.5:11434")
    assert settings.ollama_host() == "http://10.0.0.5:11434"        # the Settings page beats .env
    llm.ready()
    msg = asyncio.run(llm.chat([{"role": "user", "content": "x"}], model=llm.model_for("assistant")))
    assert msg.content == "hi small-test:1b"
    assert FakeOllama.hosts == ["http://10.0.0.5:11434", "http://10.0.0.5:11434"]
    with pytest.raises(ValueError):
        settings.set_ollama_host("gpu-box")
    settings.set_ollama_host("")
    assert settings.ollama_host() == "http://gpu-box:11434"

def test_service_addresses_are_config(monkeypatch):
    from agents import bluesky, f916
    assert f916.api() == settings.F916_DEFAULT and bluesky._service() == settings.BLUESKY_DEFAULT
    monkeypatch.setenv("MISSION_LOOP_1F916_URL", "https://staging.1f916.example")
    monkeypatch.setenv("BLUESKY_SERVICE", "https://pds.example")
    assert f916.api() == "https://staging.1f916.example" and bluesky._service() == "https://pds.example"
    settings.set_f916_url("https://other.1f916.example/")
    assert f916.api() == "https://other.1f916.example"
    # a key belongs to the server that issued it, whatever the config says later
    f916.KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    f916.KEY_FILE.write_text(json.dumps({"handle": "h", "secret": "s", "api": "https://issued.example"}))
    assert f916.api() == "https://issued.example" and f916.post_url(7) == "https://issued.example/api/post/7"
    f916.KEY_FILE.write_text(json.dumps({"handle": "h", "secret": "s"}))   # saved before keys recorded it
    assert f916.api() == "https://other.1f916.example"
    # a Bluesky login saved on the Settings page keeps the server it was checked against
    bluesky.save_credentials("me.example", "app-pass", "https://my-pds.example/")
    assert bluesky._service() == "https://my-pds.example"
    assert json.loads(bluesky.CRED_FILE.read_text())["service"] == "https://my-pds.example"

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(orchestrator, "LOCK_FILE", Path("data/loop.lock"))
    monkeypatch.setattr(orchestrator, "WAKE_FLAG", Path("data/WAKE"))
    return TestClient(ui.app)

def test_settings_page_shows_both_workers_and_the_connection(client):
    html = client.get("/settings").text
    assert "Research Assistant" in html and "Principal Investigator" in html and "in parallel" not in html
    assert '<option value="small-test:1b" selected>' in html and '<option value="big-test:30b" selected>' in html
    assert "other-test:8b · 5.2 GB" in html and "big-test:30b · 18.0 GB · loaded" in html     # every pulled model
    assert "<strong>Source finder</strong>" in html and "<strong>Article writer</strong>" in html   # where each is used
    assert 'data-crew="assistant"' in html and 'data-crew="pi"' in html and "researchers.js" in html
    assert "LLM connection" in html and "in memory, 21.0 GB" in html
    assert 'name="ollama_host"' in html and 'name="f916_url"' in html and 'name="service"' in html

def test_settings_page_asks_for_models_when_none_are_configured(client, monkeypatch):
    monkeypatch.delenv("MISSION_LOOP_MODEL"); monkeypatch.delenv("MISSION_LOOP_SEARCH_MODEL")
    html = client.get("/settings").text
    assert html.count('<option value="" selected disabled>Choose a model</option>') == 2
    assert "No model chosen yet." in html and "the loop won't start until you do" in html
    r = client.post("/settings/worker", data={"worker": "pi", "model": "big-test:30b", "on": "on"}, follow_redirects=False)
    assert "worker_saved" in r.headers["location"] and settings.saved() == {"pi_model": "big-test:30b"}

def test_settings_page_saves_the_ollama_address(client):
    r = client.post("/settings/ollama", data={"ollama_host": "http://gpu-box:11434"}, follow_redirects=False)
    assert "ollama_saved" in r.headers["location"] and settings.ollama_host() == "http://gpu-box:11434"
    r = client.post("/settings/ollama", data={"ollama_host": "not a url"}, follow_redirects=False)
    assert "error=ollama_host" in r.headers["location"] and settings.ollama_host() == "http://gpu-box:11434"
    assert client.post("/settings/ollama", data={"ollama_host": ""}, headers={"origin": "https://evil.example"}).status_code == 403

def test_settings_page_saves_a_worker(client):
    r = client.post("/settings/worker", data={"worker": "pi", "model": "other-test:8b", "on": "on"}, follow_redirects=False)
    assert "worker_saved" in r.headers["location"] and settings.worker_model("pi") == "other-test:8b"
    r = client.post("/settings/worker", data={"worker": "assistant", "model": "small-test:1b"}, follow_redirects=False)
    assert "worker_off" in r.headers["location"] and not settings.worker_on("assistant")
    html = client.get("/settings").text
    assert "Only the Principal Investigator is on" in html and 'class="worker off"' in html
    r = client.post("/settings/worker", data={"worker": "pi", "model": "other-test:8b"}, follow_redirects=False)
    assert "worker_last" in r.headers["location"] and settings.worker_on("pi")      # one has to stay on
    r = client.post("/settings/worker", data={"worker": "pi", "model": "nope:1b", "on": "on"}, follow_redirects=False)
    assert "worker_model" in r.headers["location"] and settings.worker_model("pi") == "other-test:8b"
    evil = {"origin": "https://evil.example"}
    assert client.post("/settings/worker", data={"worker": "pi", "model": "big-test:30b", "on": "on"},
                       headers=evil).status_code == 403

def test_unchanged_model_saves_while_ollama_is_closed(client, monkeypatch):
    closed = {"host": "http://127.0.0.1:11434", "ok": False, "error": "ConnectError", "models": [], "loaded": {}}
    monkeypatch.setattr(llm, "connection", lambda timeout=2: closed)
    html = client.get("/settings").text
    assert "small-test:1b (Ollama not reachable)" in html and "is not reachable (ConnectError)" in html
    r = client.post("/settings/worker", data={"worker": "assistant", "model": "small-test:1b"}, follow_redirects=False)
    assert "worker_off" in r.headers["location"]
    r = client.post("/settings/worker", data={"worker": "assistant", "model": "other-test:8b", "on": "on"}, follow_redirects=False)
    assert "error=ollama" in r.headers["location"]

def test_dashboard_shows_both_workers_at_once(client, monkeypatch):
    monkeypatch.setattr(ui, "loop_running", lambda: True)
    researching(); writing()
    activity.work_as("assistant"); activity.begin("r", "sources")
    activity.work_as("pi"); activity.begin("w", "writer")
    activity.work_as(activity.DESK)
    html = client.get("/").text
    assert html.count('class="agent on"') == 2 and html.count('class="card active"') == 2
    assert '<span class="agent on" aria-current="step" data-worker="assistant">Source finder</span>' in html
    assert '<span class="who">Principal Investigator</span> <strong>Article writer</strong>' in html
    assert '<a class="card active" href="/m/w" data-lab="busy" data-worker="pi">' in html
    assert "Now: <strong>Source finder</strong>" in client.get("/settings").text

def test_each_workers_trail_stays_with_its_own_mission():
    """The chat assistant reads what each agent did; two workers at once must not mix up their steps."""
    import trail
    meet = threading.Barrier(2, timeout=5)
    def work(worker, slug, agent):
        activity.work_as(worker)
        activity.begin(slug, agent)
        meet.wait()                        # both agents are current at the same moment
        trail.record("said", f"{agent} on {slug}")
        activity.clear()
    threads = [threading.Thread(target=work, args=("assistant", "r", "sources")),
               threading.Thread(target=work, args=("pi", "w", "writer"))]
    for t in threads: t.start()
    for t in threads: t.join()
    assert [s["text"] for s in trail.recent("r") if s["kind"] == "said"] == ["sources on r"]
    assert [s["text"] for s in trail.recent("w") if s["kind"] == "said"] == ["writer on w"]
    assert activity.working() == []
