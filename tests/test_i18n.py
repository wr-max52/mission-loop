"""The UI's languages: every string the UI shows has a translation in every locale, placeholders survive, the
Settings page switches the language, and the published site stays English."""
import ast, json, re
from pathlib import Path
import pytest
from starlette.testclient import TestClient
import activity, desk_profiles, flowchart, i18n, search_status, settings, ui
from agents import assistant
from contracts import RATING_FACTORS

ROOT = Path(__file__).parent.parent
CODES = [c for c in settings.LANGUAGES if c != "en"]

def called(path: Path) -> set[str]:
    """Every literal passed to _() or _n() in a module."""
    out = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("_", "_n"):
            out |= {a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)}
    return out

def keys() -> set[str]:
    """Every English string the UI looks up: the literals, and the tables it translates at display time."""
    out = called(ROOT / "ui.py") | called(ROOT / "flowchart.py") | called(ROOT / "doctor.py")
    out |= set(ui.MESSAGES.values()) | set(ui.ERRORS.values()) | set(ui.LABELS.values())
    out |= set(activity.AGENTS.values()) | set(settings.WORKERS.values()) | set(ui.WORKER_BLURBS.values())
    out |= {s for steps in ui.ROLE_STEPS.values() for s in steps} | set(ui.ASK_ACTION.values())
    out |= {title for _k, _i, title in ui.STAGES} | set(ui.SUGGESTIONS) | set(ui.JS_STRINGS)
    out |= set(settings.CUSTOM_FORMATS.values()) | set(settings.CUSTOM_KINDS.values())
    out |= {p["blurb"] for p in settings.PROVIDERS.values()} | set(ui.BY.values()) | {settings.PROVIDERS["custom"]["name"]}
    out |= set(settings.BOARD_SURFACES.values()) | set(settings.BOARD_CARDS.values()) | set(settings.BOARD_STRING_NAMES.values())
    out |= {name for colors in settings.BOARD_STRINGS.values() for name in colors}
    out |= set(ui.script_strings("board.js")) | set(ui.script_strings("avatars.js"))
    out |= set(settings.CHAT_COLORS) | {"Library red"} | {label for _v, _i, label in ui.THEME_CHOICES}
    out |= {text for _k, _kind, text in flowchart.FLOW} | {sub for _i, sub in flowchart.TILES.values()}
    out |= set(flowchart.REVIEW_SUB.values()) | {t for t, _tone in flowchart.HUMAN_STEPS.values()}
    out |= {s for pair in flowchart.AUTO_STEPS.values() for s in pair} | set(flowchart.CHECKS.values())
    out |= set(ui.PAST.values()) | set(ui.OUTCOME.values()) | set(ui.LOG_COLUMNS) | {"done"}
    out |= set(ui.CLAIM_CHIP.values()) | set(ui.VERDICT_NAMES.values()) | set(ui.VERDICT_HINTS.values())
    out |= set(ui.SET_BY.values())
    out |= {title for _a, _i, title in ui.SETTINGS_SECTIONS}
    out |= {flowchart.RETRY, ui.STUB_RUN, "Local site", "Why it stopped", "Your feedback", "Check notes",
            "Brief ready", "Article ready", "Parked", "Why it went back"}
    out |= set(RATING_FACTORS.values()) | set(ui.RATING_SHORT.values())
    out |= {s for pair in assistant.TOOL_LABELS.values() for s in pair}      # Researcher Chat's live line
    out |= set(search_status.FIX.values()) | {src["blurb"] for src in settings.SEARCH_SOURCES.values()}
    out |= set(desk_profiles.BLOCKS.values())                               # Settings → Research → Profile
    return out

PLACEHOLDER = re.compile(r"\{[a-z_]*\}")

@pytest.mark.parametrize("code", CODES)
def test_every_ui_string_is_translated(code):
    table = json.loads((ROOT / "locales" / f"{code}.json").read_text())
    missing = sorted(keys() - set(table))
    assert not missing, f"{code} is missing {len(missing)}: {missing[:10]}"
    for english, translated in table.items():
        assert translated.strip(), f"{code}: empty translation for {english!r}"
        assert sorted(PLACEHOLDER.findall(english)) == sorted(PLACEHOLDER.findall(translated)), \
            f"{code}: placeholders differ in {english!r} -> {translated!r}"

@pytest.mark.parametrize("code", CODES)
def test_every_language_has_researcher_lines(code):
    js = (ROOT / "assets" / "lines" / f"{code}.js").read_text()
    assert js.startswith("window.RESEARCHER_LINES = ")
    data = json.loads(js.removeprefix("window.RESEARCHER_LINES = ").rstrip().rstrip(";"))
    assert len(data["quips"]) >= 120 and len(set(data["quips"])) == len(data["quips"])
    for part in ("quotes", "bits", "steps", "closers"):
        assert data[part] and all(isinstance(v, list) and v for v in data[part].values()), part
    assert "{name}" in data["reporting"] and "{name}" in data["guide"] and "{model}" in data["guide"]

def test_settings_page_switches_the_language_and_the_published_site_stays_english():
    client = TestClient(ui.app)
    html = client.get("/settings").text
    assert 'name="language" value="fr"' in html and '<html lang="en"' in html and "window.I18N = " not in html
    client.post("/settings/language", data={"language": "fr"})
    assert settings.language() == "fr"
    html = client.get("/").text
    assert '<html lang="fr"' in html and "Paramètres" in html and "window.I18N = " in html
    assert '<script src="/assets/lines/fr.js?v=' in html
    from render import about_html
    assert "Review brief" in about_html() and "Relire" not in about_html()
    client.post("/settings/language", data={"language": "xx"})
    assert settings.language() == "fr"                    # an unknown code changes nothing
    client.post("/settings/language", data={"language": "en"})
    assert settings.language() == "en" and "language" not in settings.saved()

def test_chat_answers_in_the_ui_language():
    from agents import assistant
    assert assistant._language() == ""
    settings.set_language("zh")
    assert "Chinese" in assistant._language()
    settings.set_language("en")
