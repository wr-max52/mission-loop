"""Desk profiles (#122): the agents' persona words come from a profile file, the checks don't. With investigative (the
default) every prompt is byte for byte what it was before profiles; another profile changes only its blocks; a
custom file in data/profiles/ falls back to investigative's words for any block it leaves out."""
import json
from datetime import datetime, timezone
from pathlib import Path
import pytest
from starlette.testclient import TestClient
import desk_profiles, orchestrator, settings, store, ui
from agents import finder, rater, researcher, theorist, writer
from agents.stubs import research as stub_brief, write as stub_article
from contracts import Mission

# Each prompt template exactly as it was on main before profiles (captured from the code, not retyped).
BEFORE = json.loads((Path(__file__).parent / "fixtures" / "investigative_prompts.json").read_text())
NOW = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
M = Mission(slug="heat", topic="Heat pumps in the cold", objective="Do heat pumps hold up below freezing?",
            validator_notes="Find a third site.")
BRIEF = stub_brief(M)
ARTICLE = stub_article(M, BRIEF, [])

def prompts() -> dict[str, str]:
    """Every agent's prompt that a profile touches, built now, as each agent builds it."""
    store.save_mission(M)
    return {
        "researcher.SYSTEM": researcher.prompt(M, NOW),
        "finder.SEARCH": finder.prompt("Who pays for heat pumps?", "- none yet", finder.ANGLES[0], finder.ANGLES[1], NOW),
        "theorist.SYSTEM": theorist.prompt("Who pays for heat pumps?", [], store.all_missions(), NOW),
        "writer.SYSTEM": writer.prompt(M, BRIEF, []),
        "rater.SYSTEM": rater.messages(BRIEF, ARTICLE, "confirmed")[0]["content"],
    }

def before() -> dict[str, str]:
    """The same prompts built from the templates as they were before profiles (format ignores the new keys)."""
    modules = {"researcher": researcher, "finder": finder, "theorist": theorist, "writer": writer, "rater": rater}
    with pytest.MonkeyPatch.context() as mp:
        for key, text in BEFORE.items():
            module, name = key.split(".")
            mp.setattr(modules[module], name, text)
        return prompts()

def first_line(text: str) -> str:
    return text.splitlines()[0]

def custom(name: str, text: str) -> Path:
    path = Path("data/profiles") / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(orchestrator, "LOCK_FILE", Path("data/loop.lock"))
    monkeypatch.setattr(orchestrator, "WAKE_FLAG", Path("data/WAKE"))
    return TestClient(ui.app)

def test_with_investigative_every_prompt_is_unchanged():
    assert desk_profiles.current() == "investigative" and "profile" not in settings.saved()
    old, new = before(), prompts()
    for key in BEFORE:
        assert new[key] == old[key], key
    assert first_line(new["researcher.SYSTEM"]) == "You are a skeptical, nonpartisan political investigator. Your job is to find the edge of what"
    assert "for an independent investigative desk." in new["finder.SEARCH"]

def test_explainer_changes_the_researchers_first_line_and_the_finders_desk_line():
    old = before()
    settings.set_profile("explainer")
    new = prompts()                                    # built again: no restart, no reimport
    assert settings.saved()["profile"] == "explainer"
    blocks = desk_profiles.blocks("explainer")
    assert first_line(new["researcher.SYSTEM"]) == first_line(blocks["researcher"]) != first_line(old["researcher.SYSTEM"])
    assert first_line(new["finder.SEARCH"]) == "You are the mission finder for an independent explainer desk. You do not research stories."
    assert first_line(new["theorist.SYSTEM"]).startswith("You are the Theorist of an independent explainer desk: its mastermind.")
    assert first_line(new["writer.SYSTEM"]) == blocks["writer"]
    for key in BEFORE:                                 # the words changed, the rest of each prompt didn't
        assert new[key] != old[key], key
    assert "political" not in new["researcher.SYSTEM"] and "partisan" not in new["rater.SYSTEM"]

def test_the_checks_are_the_same_in_every_profile():
    rules = {"researcher.SYSTEM": ['Each must begin with the words "It is possible that"',
                                   "Find at least 3 supporting sources on 3 different websites.",
                                   "Code checks\n  every quote against the page.", "A dead end is a finding."],
             "rater.SYSTEM": ["How to score: 1-3 weak", "A source's quote that doesn't say what the article uses it for"],
             "writer.SYSTEM": ["The brief is the only\nthing you know"]}
    for name in desk_profiles.BUILT_INS:
        settings.set_profile(name)
        built = prompts()
        for key, needles in rules.items():
            for needle in needles:
                assert needle in built[key], (name, key, needle)
        for key, text in built.items():
            assert "{" + "researcher}" not in text and "{the_desk}" not in text and "{rater}" not in text, (name, key)

def test_a_custom_file_with_only_a_researcher_block_uses_investigative_for_the_rest(client):
    old = before()
    custom("local", "# Local history\n\nA desk for the town's past.\n\n## Researcher\nYou are a local historian.\n"
                    "Your job is to find what the town's records show.\n")
    assert "local" in desk_profiles.names()
    settings.set_profile("local")
    new = prompts()
    assert new["researcher.SYSTEM"].startswith("You are a local historian.\nYour job is to find what the town's "
                                               "records show.\nTopic: Heat pumps in the cold\n")
    assert new["researcher.SYSTEM"].split("Topic:", 1)[1] == old["researcher.SYSTEM"].split("Topic:", 1)[1]
    for key in ("finder.SEARCH", "theorist.SYSTEM", "writer.SYSTEM", "rater.SYSTEM"):
        assert new[key] == old[key], key
    assert desk_profiles.missing("local") == ["desk", "primary_sources", "writer", "rater"]
    html = client.get("/settings").text                # the page says which blocks fall back, and where the file is
    assert '<option value="local" selected>Local history (local)</option>' in html
    assert "Uses the investigative profile&#x27;s words for: Desk, Primary sources, Writer, Rater." in html
    assert "<code>data/profiles/local.md</code>" in html and "A desk for the town&#x27;s past." in html

def test_an_empty_block_or_a_misspelt_heading_falls_back_and_the_page_says_so(client):
    custom("typo", "## Desk\n\n## Writr\nYou are a poet.\n")
    settings.set_profile("typo")
    assert desk_profiles.blocks()["desk"] == "an independent investigative desk"
    assert desk_profiles.blocks()["writer"] == desk_profiles.blocks("investigative")["writer"]
    assert desk_profiles.unknown("typo") == ["writr"]
    html = client.get("/settings").text
    assert "Headings not used: writr." in html and "for: Desk, Researcher, Primary sources, Writer, Rater." in html

def test_a_custom_profile_may_be_written_without_the_prompts_indentation():
    custom("flat", "## Primary sources\nRead the minutes\nof every council meeting.\n\n## Desk\nthe town\n  history desk\n")
    settings.set_profile("flat")
    text = researcher.prompt(M, NOW)
    assert "\n- Read the minutes\n  of every council meeting.\n- Set each source's stance" in text
    assert desk_profiles.block("desk") == "the town history desk"   # read inside a sentence: one line

def test_every_built_in_profile_has_every_block_and_only_investigative_is_political():
    assert desk_profiles.names()[:4] == list(desk_profiles.BUILT_INS)
    for name in desk_profiles.BUILT_INS:
        assert desk_profiles.missing(name) == [] and desk_profiles.unknown(name) == [], name
        assert desk_profiles.title(name) and desk_profiles.description(name), name
        if name != "investigative":
            words = " ".join(desk_profiles.blocks(name).values()).lower()
            for political in ("political", "partisan", "campaign", "legislature", "election", "roll-call"):
                assert political not in words, (name, political)

def test_picking_a_profile_on_the_settings_page_saves_it_only_when_not_investigative(client):
    html = client.get("/settings").text
    assert 'action="/settings/profile"' in html and '<option value="investigative" selected>Investigative</option>' in html
    assert '<option value="market-scan">Market scan</option>' in html
    r = client.post("/settings/profile", data={"do": "pick", "profile": "literature-review"}, follow_redirects=False)
    assert r.status_code == 303 and "done=profile_saved" in r.headers["location"]
    assert settings.saved()["profile"] == "literature-review" and desk_profiles.current() == "literature-review"
    client.post("/settings/profile", data={"do": "pick", "profile": "investigative"})
    assert "profile" not in settings.saved()
    for bad in ("../secrets", "nope", "", "/etc/passwd", "INVESTIGATIVE"):
        r = client.post("/settings/profile", data={"do": "pick", "profile": bad}, follow_redirects=False)
        assert "error=profile" in r.headers["location"], bad
    assert "profile" not in settings.saved()
    r = client.post("/settings/profile", data={"profile": "explainer"}, headers={"origin": "http://evil.example"})
    assert r.status_code == 403 and "profile" not in settings.saved()

def test_copy_to_edit_copies_the_profile_in_use_and_switches_to_the_copy(client):
    settings.set_profile("explainer")
    r = client.post("/settings/profile", data={"do": "copy"}, follow_redirects=False)
    assert "done=profile_copied" in r.headers["location"]
    copy = Path("data/profiles/my-explainer.md")
    assert copy.read_text() == (desk_profiles.BUILT_IN / "explainer.md").read_text()
    assert settings.profile() == "my-explainer" and desk_profiles.blocks() == desk_profiles.blocks("explainer")
    assert "<code>data/profiles/my-explainer.md</code>" in client.get("/settings").text
    client.post("/settings/profile", data={"do": "copy"})          # a copy of the copy never overwrites it
    assert Path("data/profiles/my-explainer-2.md").exists() and settings.profile() == "my-explainer-2"

def test_names_that_could_leave_the_folder_are_never_read():
    custom("investigative", "## Researcher\nYou are someone else.\n")   # a built-in's name: the built-in wins
    Path("data/profiles/Bad Name.md").write_text("## Researcher\nNo.\n")
    Path("data/secret.md").write_text("## Researcher\nLeaked.\n")
    assert desk_profiles.names() == list(desk_profiles.BUILT_INS)
    for name in ("../secret", "..", "Bad Name", "a/b", ".hidden"):
        assert desk_profiles.path(name) is None, name
    settings.save(profile="../secret")                             # a hand-edited settings.json
    assert desk_profiles.current() == "investigative"
    assert first_line(researcher.prompt(M, NOW)).startswith("You are a skeptical, nonpartisan political investigator.")
    settings.save(profile="gone")                                   # a custom profile whose file was deleted
    assert desk_profiles.current() == "investigative"
