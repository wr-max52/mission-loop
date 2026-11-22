import httpx
import pytest
from agents.researcher import _failed_source_ids
import wayback
from checks import validator
from contracts import Mission, ResearchBrief, Source, POC, Claim

REAL_FETCH = validator.fetch               # the fake_web fixture replaces validator.fetch
M = Mission(slug="v", topic="t")
Q = "Heat pumps kept homes above 20C at minus 25C during the trial."

PAGES = {
    "https://a.com/1": ("<p>x</p>", f"Intro. {Q} More text."),
    "https://b.org/2": ("<p>x</p>", f"Other words. {Q}"),
    "https://c.net/3": ("<p>x</p>", f"{Q} Closing."),
    "https://d.io/4":  ("<p>x</p>", "Critics say efficiency collapses below minus 20C in older homes."),
    "https://a.com/team": ("<a href='mailto:ana@a.com'>Ana</a> bo@b.org cy@c.net", "team"),
}

def brief(**changes) -> ResearchBrief:
    data = dict(
        thesis="Cold climate heat pumps work",
        claims=[Claim(text="They hold temperature", source_ids=["s1", "s2"])],
        counterpoint="Older homes need insulation first",
        sources=[
            Source(id="s1", url="https://a.com/1", title="A", quote=Q, stance="supports"),
            Source(id="s2", url="https://b.org/2", title="B", quote=Q, stance="supports"),
            Source(id="s3", url="https://c.net/3", title="C", quote=Q, stance="supports"),
            Source(id="s4", url="https://d.io/4", title="D",
                   quote="Critics say efficiency collapses below minus 20C in older homes.", stance="contradicts"),
        ],
        pocs=[POC(name=n, role="Engineer", email=e, contact_source_url="https://a.com/team", why_them="x")
              for n, e in [("Ana", "ana@a.com"), ("Bo", "bo@b.org"), ("Cy", "cy@c.net")]],
        question_for_pocs="Does this match what you see?",
    )
    data.update(changes)
    return ResearchBrief(**data)

@pytest.fixture(autouse=True)
def fake_web(monkeypatch):
    monkeypatch.setattr(validator, "fetch", lambda url: PAGES.get(url))
    monkeypatch.setattr(wayback, "snapshot", lambda page, when="": None)

def test_good_brief_passes():
    ok, notes = validator.validate(M, brief())
    assert ok, notes

def test_invented_quote_fails():
    b = brief()
    b.sources[0].quote = "Heat pumps are the best technology ever invented for every home."
    ok, notes = validator.validate(M, b)
    assert not ok and "s1: quote not found" in notes

def test_unfetchable_source_fails():
    b = brief()
    b.sources[1].url = "https://dead.example/404"
    ok, notes = validator.validate(M, b)
    assert not ok and "could not fetch" in notes

def test_too_few_domains_fails():
    b = brief()
    b.sources[2].url = "https://a.com/1"          # now only a.com and b.org support
    ok, notes = validator.validate(M, b)
    assert not ok and "supporting domain" in notes

def test_counterpoint_without_a_contradicting_source_passes():
    b = brief()
    b.sources[3].stance = "supports"      # a broad thesis may have no source that answers it
    ok, notes = validator.validate(M, b)
    assert ok, notes

def test_empty_counterpoint_fails():
    ok, notes = validator.validate(M, brief(counterpoint="  "))
    assert not ok and "Counterpoint is empty" in notes

def test_unverified_contradicting_source_still_fails():
    b = brief()
    b.sources[3].quote = "Critics say heat pumps never work in any cold climate at all."
    ok, notes = validator.validate(M, b)
    assert not ok and "s4: quote not found" in notes

def test_unknown_source_id_fails():
    ok, notes = validator.validate(M, brief(claims=[Claim(text="x", source_ids=["s9"])]))
    assert not ok and "unknown source id 's9'" in notes

FACT = Claim(text="They hold temperature", source_ids=["s1"])

def test_labeled_possibility_passes_alongside_a_fact():
    maybe = Claim(text="It is possible that newer homes benefit most", source_ids=["s1"], kind="possibility")
    ok, notes = validator.validate(M, brief(claims=[FACT, maybe]))
    assert ok, notes

def test_possibility_without_the_wording_fails():
    maybe = Claim(text="Newer homes benefit most", source_ids=["s1"], kind="possibility")
    ok, notes = validator.validate(M, brief(claims=[FACT, maybe]))
    assert not ok and "must be worded" in notes

def test_possibility_marked_as_fact_fails():
    wrong = Claim(text="It is possible that newer homes benefit most", source_ids=["s1"])
    ok, notes = validator.validate(M, brief(claims=[FACT, wrong]))
    assert not ok and "its kind is 'fact'" in notes

def test_brief_of_only_possibilities_fails():
    maybe = Claim(text="It is possible that newer homes benefit most", source_ids=["s1"], kind="possibility")
    ok, notes = validator.validate(M, brief(claims=[maybe]))
    assert not ok and "No claim of kind 'fact'" in notes

def test_poc_email_not_on_page_fails(monkeypatch):
    monkeypatch.setattr(validator.settings, "OUTREACH", True)
    b = brief()
    b.pocs[2].email = "nobody@nowhere.com"
    ok, notes = validator.validate(M, b)
    assert not ok and "nobody@nowhere.com not found" in notes

def test_blocked_domain_fails(monkeypatch, tmp_path):
    blocked = tmp_path / "blocked.txt"
    blocked.write_text("# comment\nwww.b.org\n")
    monkeypatch.setattr(validator, "BLOCKED_FILE", blocked)
    ok, notes = validator.validate(M, brief())
    assert not ok and "s2: b.org is on the blocked list" in notes

def test_failed_quote_note_shows_the_real_sentence():
    b = brief()
    b.sources[3].quote = "Critics say heat pump efficiency collapses below minus 30C in all homes."
    ok, notes = validator.validate(M, b)
    assert not ok and 'Closest sentence actually on the page: "Critics say efficiency collapses' in notes

def test_pocs_not_checked_while_outreach_is_off(monkeypatch):
    monkeypatch.setattr(validator.settings, "OUTREACH", False)
    b = brief()
    b.pocs[2].email = "nobody@nowhere.com"
    assert validator.validate(M, b)[0]
    assert validator.validate(M, brief(pocs=[], question_for_pocs=""))[0]

def test_outreach_needs_three_pocs(monkeypatch):
    monkeypatch.setattr(validator.settings, "OUTREACH", True)
    ok, notes = validator.validate(M, brief(pocs=[]))
    assert not ok and "need 3" in notes

def test_researcher_can_tell_which_sources_failed():
    b = brief()
    b.sources[0].quote = "Heat pumps are the best technology ever invented for every home."
    b.sources[1].url = "https://dead.example/404"
    ok, notes = validator.validate(M, b)
    assert not ok
    failed = _failed_source_ids(notes)
    assert {"s1", "s2"} <= failed and not {"s3", "s4"} & failed

def scripted_get(monkeypatch, outcomes):
    """Make httpx.get play back responses or raise exceptions; return the list of calls."""
    calls = []
    it = iter(outcomes)
    def fake_get(url, **kwargs):
        calls.append(url)
        outcome = next(it)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(validator.httpx, "get", fake_get)
    monkeypatch.setattr(validator, "_cache", {})
    return calls

@pytest.mark.parametrize("failure", [httpx.Response(403), httpx.ReadTimeout("slow")], ids=["403", "timeout"])
def test_failed_fetch_is_tried_again(monkeypatch, failure):
    calls = scripted_get(monkeypatch, [failure, httpx.Response(200, text="<p>back</p>")])
    assert REAL_FETCH("https://a.com/1") is None
    assert REAL_FETCH("https://a.com/1")[0] == "<p>back</p>"
    assert len(calls) == 2

def test_successful_fetch_is_cached(monkeypatch):
    calls = scripted_get(monkeypatch, [httpx.Response(200, text="<p>once</p>")])
    assert REAL_FETCH("https://a.com/1") == REAL_FETCH("https://a.com/1")
    assert len(calls) == 1

def test_poc_name_not_on_page_fails(monkeypatch):
    monkeypatch.setattr(validator.settings, "OUTREACH", True)
    b = brief()
    b.pocs[0].name = "Zed Quux"
    ok, notes = validator.validate(M, b)
    assert not ok and "POC Zed Quux: name not found" in notes

def test_validator_page_cache_stays_small(monkeypatch):
    class R:
        status_code, text = 200, "<p>x</p>"
    monkeypatch.setattr(validator.httpx, "get", lambda *a, **k: R())
    monkeypatch.setattr(validator, "_cache", {})
    for n in range(validator.CACHE_PAGES + 5):
        REAL_FETCH(f"https://example.com/{n}")
    assert len(validator._cache) == validator.CACHE_PAGES and "https://example.com/0" not in validator._cache
