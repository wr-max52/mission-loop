import pytest
from checks import validator
from contracts import Mission, ResearchBrief, Source, POC, Claim

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

def test_missing_contradiction_fails():
    b = brief()
    b.sources[3].stance = "supports"
    ok, notes = validator.validate(M, b)
    assert not ok and "contradicts" in notes

def test_unknown_source_id_fails():
    ok, notes = validator.validate(M, brief(claims=[Claim(text="x", source_ids=["s9"])]))
    assert not ok and "unknown source id 's9'" in notes

def test_poc_email_not_on_page_fails():
    b = brief()
    b.pocs[2].email = "nobody@nowhere.com"
    ok, notes = validator.validate(M, b)
    assert not ok and "nobody@nowhere.com not found" in notes
