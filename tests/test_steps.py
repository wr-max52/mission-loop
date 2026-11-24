"""The step log: every agent step the loop finishes, with how long it took, under the strip and on /log."""
import json
from datetime import datetime, timezone
from pathlib import Path
import pytest
from starlette.testclient import TestClient
import time
import activity, agents, orchestrator, settings, stopping, store, ui
from contracts import Mission, State

pytestmark = pytest.mark.usefixtures("stub_agents")
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(orchestrator, "LOCK_FILE", Path("data/loop.lock"))
    monkeypatch.setattr(orchestrator, "WAKE_FLAG", Path("data/WAKE"))
    return TestClient(ui.app)

def logged():
    return [json.loads(line) for line in activity.LOG.read_text().splitlines()]

def test_each_step_ends_at_the_next_begin_or_clear():
    activity.begin("b", "sources")
    activity.begin("b", "brief")
    activity.clear()
    steps = logged()
    assert [(e["slug"], e["agent"], e["outcome"], e["worker"]) for e in steps] == \
        [("b", "sources", "done", "desk"), ("b", "brief", "done", "desk")]
    assert all(e["ended"] >= e["since"] for e in steps)
    assert [e["agent"] for e in activity.recent()] == ["brief", "sources"]   # newest first
    activity.clear()                                   # nothing at work: nothing more is logged
    assert len(logged()) == 2

def test_a_failure_or_stop_is_recorded_and_the_first_outcome_stands():
    activity.begin("b", "brief")
    activity.fail(RuntimeError("model down"))
    activity.outcome("failed", "the check that followed")
    activity.begin("b", "writer")
    activity.fail(stopping.Interrupted())
    activity.clear()
    brief, writer = logged()
    assert (brief["outcome"], brief["why"]) == ("failed", "model down")
    assert writer["outcome"] == "stopped"

def test_the_loop_logs_its_steps_and_a_failed_check(monkeypatch):
    monkeypatch.setattr(agents, "validate", lambda m, b: (False, "only 2 supporting websites"))
    store.save_mission(Mission(slug="b", topic="Outages"))
    orchestrator.run_until_idle(lambda: T0)
    steps = [e for e in logged() if e["slug"] == "b"]
    assert [e["agent"] for e in steps[:3]] == ["sources", "brief", "source_check"]
    assert steps[2]["outcome"] == "failed" and "2 supporting websites" in steps[2]["why"]
    assert steps[0]["outcome"] == steps[1]["outcome"] == "done"

def test_a_crashed_worker_task_is_logged_failed():
    def boom(): activity.begin("b", "writer"); raise RuntimeError("out of memory")
    with pytest.raises(RuntimeError):
        orchestrator._as_worker("pi", boom)
    activity.work_as(activity.DESK)
    (e,) = logged()
    assert (e["worker"], e["agent"], e["outcome"]) == ("pi", "writer", "failed")

def test_recent_skips_a_half_written_line():
    activity.begin("b", "sources"); activity.clear()
    with activity.LOG.open("a") as f:
        f.write('{"worker": "pi", "slug": "b", "ag')
    assert [e["agent"] for e in activity.recent()] == ["sources"]

def test_strip_shows_work_in_progress_then_the_last_three_steps(client, monkeypatch):
    store.save_mission(Mission(slug="b", topic="Outages", state=State.RESEARCHING))
    for agent in ("sources", "brief", "source_check", "writer"):
        activity.begin("b", agent)
    activity.clear()
    monkeypatch.setattr(ui, "loop_running", lambda: True)
    activity.begin("b", "article_check")
    now = client.get("/api/flow").json()["now"]
    assert now.index("<strong>Article checker</strong> · ") < now.index("<strong>Article writer</strong> · ")
    assert now.count('class="past"') == 3 and "Source finder" not in now   # only the last three
    assert "took " in now and 'href="/m/b">Outages</a>' in now and 'href="/log"' in now
    assert '<span class="ago"' in client.get("/").text

def test_log_page_lists_every_step_and_filters_by_mission(client, monkeypatch):
    store.save_mission(Mission(slug="b", topic="Outages"))
    store.save_mission(Mission(slug="c", topic="Grid"))
    activity.begin("b", "sources"); activity.begin("c", "brief"); activity.fail(ValueError("bad JSON")); activity.clear()
    activity.begin("", "theorist"); activity.clear()
    html = client.get("/log").text
    assert "<h1>Step log</h1>" in html and "3 steps · " in html and "data/steps.jsonl" in html
    assert "Outages" in html and "Grid" in html and "all missions" in html
    assert '<span class="out failed">failed</span> <span class="muted">ValueError: bad JSON</span>' in html
    one = client.get("/log?m=b").text
    assert "1 step on" in one and "Grid" not in one.split("<h1>")[1] and "Back to the mission" in one
    store.log_event("c", T0, "QUEUED", "QUEUED", "added")
    assert 'href="/log?m=c"' in client.get("/m/c").text          # its History links to its steps
    monkeypatch.setattr(ui, "LOG_PAGE", 2)
    assert "Page 1 of 2" in client.get("/log").text and "Page 2 of 2" in client.get("/log?page=9").text

def test_log_page_with_no_steps(client):
    assert "No steps yet" in client.get("/log").text

# ---------- /stats (#121) ----------

def write_log(lines):
    activity.LOG.parent.mkdir(parents=True, exist_ok=True)
    activity.LOG.write_text("".join(json.dumps(e) + "\n" for e in lines))

def step(agent, outcome="done", model=None, took=10.0, ended=None):
    ended = time.time() - 60 if ended is None else ended
    return {"worker": "pi", "slug": "b", "agent": agent, "since": ended - took, "ended": ended, "outcome": outcome} \
        | ({"model": model} if model else {})

def test_each_step_records_the_model_its_seat_used():
    settings.save(assistant_model="small-test:1b", pi_model="anthropic/claude-test")
    activity.work_as("assistant-2"); activity.begin("b", "sources")
    activity.work_as("pi"); activity.begin("b", "brief")
    activity.work_as(activity.DESK); activity.begin("b", "publisher")
    for seat in ("assistant-2", "pi", activity.DESK):
        activity.work_as(seat); activity.clear()
    activity.work_as(activity.DESK)
    models = {e["agent"]: e.get("model") for e in logged()}
    assert models == {"sources": "small-test:1b", "brief": "anthropic/claude-test", "publisher": None}

def test_stats_count_brief_checks_per_model():
    lines = [step("source_check", "done", "model-a")] * 3 + [step("source_check", "failed", "model-a")] \
        + [step("source_check", "done", "model-b")] * 2
    rows = {r["model"]: r for r in activity.stats(lines, by_model=True)}
    assert rows["model-a"]["rate"] == 0.75 and rows["model-b"]["rate"] == 1.0
    assert (rows["model-a"]["runs"], rows["model-a"]["passed"], rows["model-a"]["failed"]) == (4, 3, 1)

def test_stats_page_shows_the_pass_rate_on_each_models_row(client):
    write_log([step("source_check", "done", "model-a")] * 3 + [step("source_check", "failed", "model-a")]
              + [step("source_check", "done", "model-b")] * 2)
    html = client.get("/stats").text
    assert "<h1>Step stats</h1>" in html
    by_model = html.split("<h2>By model</h2>")[1]
    row_a = by_model.split("<code>model-a</code>")[1].split("</tr>")[0]
    row_b = by_model.split("<code>model-b</code>")[1].split("</tr>")[0]
    assert ">4<" in row_a and ">75%<" in row_a and ">2<" in row_b and ">100%<" in row_b
    by_step = html.split("<h2>By step</h2>")[1].split("<h2>")[0]
    assert ">83%<" in by_step                                        # 5 of 6 for the source check as a whole
    assert by_step.count("<tr>") == 1 + len(activity.STATS_AGENTS)  # every model step, even with no runs
    assert 'href="/stats"' in client.get("/log").text                # linked from the Full log page

def test_stats_leave_stopped_runs_out_of_the_rate_and_the_times():
    lines = [step("writer", "done", took=t) for t in (10, 20, 30, 40, 50, 60, 70, 80, 90, 100)] \
        + [step("writer", "failed", took=200), step("writer", "stopped", took=9999)]
    (row,) = [r for r in activity.stats(lines) if r["agent"] == "writer"]
    assert (row["runs"], row["passed"], row["failed"], row["stopped"]) == (12, 10, 1, 1)
    assert row["rate"] == 10 / 11
    assert row["median"] == 60 and row["p90"] == 100                # 11 times: the 10th of them, sorted
    empty = next(r for r in activity.stats(lines) if r["agent"] == "brief")
    assert empty["runs"] == 0 and empty["rate"] is None and empty["median"] is None

def test_stats_week_switch_and_old_lines_with_no_model(client):
    old = time.time() - 30 * 86400
    write_log([step("brief", "failed", ended=old), step("brief", "done"), step("publisher")])   # no models
    week = client.get("/stats?window=week").text
    all_time = client.get("/stats").text
    assert "unknown model" in all_time and "Publisher" not in all_time   # only the steps a model runs
    assert ">50%<" in all_time and ">50%<" not in week and ">100%<" in week
    assert '<strong>Last 7 days</strong>' in week and 'href="/stats?window=week"' in all_time

def test_stats_page_with_no_steps(client):
    html = client.get("/stats").text
    assert "No steps yet" in html and ">0<" in html
