"""The step log in the terminal (terminal_log.py): each step that ends and each mission that comes to need a human,
one line, cut to fit, coloured by outcome and linking to the missions page."""
import io, json, time
import activity, store, terminal_log
from contracts import Mission, State

URL = "http://127.0.0.1:8770/"

class Tty(io.StringIO):
    def isatty(self): return True

def step(agent="sources", slug="grid", outcome=None, took=252):
    e = {"worker": "assistant", "slug": slug, "agent": agent, "since": time.time() - took, "ended": time.time()}
    if outcome:
        e["outcome"] = outcome
    activity.LOG.parent.mkdir(parents=True, exist_ok=True)
    with activity.LOG.open("a") as f:
        f.write(json.dumps(e) + "\n")

def test_new_steps_print_with_their_outcome_and_the_missions_link(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    store.save_mission(Mission(slug="grid", topic="Texas grid outage"))
    step(agent="brief")                                  # ended before the watcher began: not printed
    out = Tty()
    w = terminal_log.Watcher(URL, out=out, width=120)
    step()
    step(agent="writer", outcome="failed")
    w.look()
    first, second = out.getvalue().splitlines()
    assert "Source finder · Texas grid outage · 4m 12s · \033[32mdone\033[0m" in first      # done is green
    assert "\033[31mfailed\033[0m" in second                                                # failed is red
    assert first.endswith(f"\033]8;;{URL}\033\\{URL}\033]8;;\033\\")                         # a link, its text the address
    w.look()
    assert len(out.getvalue().splitlines()) == 2                                            # each step once

def test_a_mission_needing_review_prints_once_in_purple(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    store.save_mission(Mission(slug="grid", topic="Texas grid outage", state=State.BRIEF_READY))
    out = Tty()
    w = terminal_log.Watcher(URL, out=out, width=120)
    w.look()
    w.look()
    [said] = out.getvalue().splitlines()
    assert "Texas grid outage · brief · \033[35mneeds review\033[0m" in said
    m = store.get_mission("grid")
    m.state = State.ARTICLE_READY
    store.save_mission(m)
    w.look()
    assert "Texas grid outage · article · \033[35mneeds review\033[0m" in out.getvalue().splitlines()[-1]

def test_a_long_line_is_cut_to_the_terminal_and_piped_output_has_no_colour():
    store.save_mission(Mission(slug="grid", topic="A very long mission topic " * 6))
    out = io.StringIO()                                  # piped: plain text, plain address
    w = terminal_log.Watcher(URL, out=out, width=90)
    step()
    w.look()
    [said] = out.getvalue().splitlines()
    assert len(said) <= 90 and "…" in said and said.endswith(f"· done  {URL}") and "\033" not in said
