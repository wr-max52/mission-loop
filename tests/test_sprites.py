"""The sprite rulebook (SPRITES.md) in code: specs are data, checked before they join the cast."""
import json, shutil, subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "assets" / "researchers.js"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node to run the page script")

# A bare page for the script: no canvas, nothing on it, so only the cast and the checks run.
HARNESS = """
const fs = require("fs");
globalThis.window = globalThis;
globalThis.matchMedia = () => ({ matches: false });
const kept = {};
globalThis.sessionStorage = { getItem: k => kept[k] ?? null, setItem: (k, v) => { kept[k] = String(v); }, removeItem: k => { delete kept[k]; } };
globalThis.document = { readyState: "complete", addEventListener() {}, querySelectorAll: () => [], querySelector: () => null, documentElement: {} };
globalThis.addEventListener = () => {};
globalThis.requestAnimationFrame = () => {};
globalThis.CSS = { escape: s => s };
window.RESEARCHER_SPRITES = %(config)s;
eval(fs.readFileSync(%(script)s, "utf8"));
console.log(JSON.stringify((%(body)s)(window.Researchers)));
"""

def js(body: str, config: dict | None = None):
    src = HARNESS % {"config": json.dumps(config or {}), "script": json.dumps(str(SCRIPT)), "body": body}
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])

EXAMPLE = json.loads((ROOT / "tools" / "example-sprite.json").read_text())

def test_every_built_in_follows_the_rulebook():
    errors = js("""R => Object.fromEntries(Object.keys(R.ROSTER).map(k => [k, R.validateSprite(Object.assign({ base: k }, R.ROSTER[k])).errors]))""")
    assert len(errors) == 12 and all(e == [] for e in errors.values()), errors

def test_the_example_sprite_checks_out_and_joins_its_crew():
    r = js("""R => { const v = R.registerSprite("custom-sprite-1", EX); return { ok: v.ok, errors: v.errors, crew: R.crewOf("assistant"),
               look: (({ maps, propMap, quirk, base, outfit }) => ({ maps: !!maps, propMap: !!propMap, quirk: !!quirk, base, outfit }))(R.makeLook("custom-sprite-1", 3)) }; }"""
           .replace("EX", json.dumps(EXAMPLE)))
    assert r["ok"], r["errors"]
    assert "custom-sprite-1" in r["crew"]
    assert r["look"] == {"maps": True, "propMap": True, "quirk": True, "base": "mei", "outfit": "labcoat"}   # the rest comes from the start

def test_a_page_adds_sprites_and_switches_some_off():
    r = js("R => ({ cast: R.CAST, on: R.enabled(), crew: R.crewOf('assistant') })",
           {"add": {"custom-sprite-1": EXAMPLE}, "off": ["pip", "sam"]})
    assert r["cast"][-1] == "custom-sprite-1"
    assert "pip" not in r["on"] and "sam" not in r["on"] and "custom-sprite-1" in r["on"]
    assert "pip" not in r["crew"] and "custom-sprite-1" in r["crew"]

def test_switching_a_whole_crew_off_keeps_it():
    r = js("R => R.crewOf('assistant')", {"off": ["ben", "mei", "sam", "javier", "hanna", "pip"]})
    assert sorted(r) == ["ben", "hanna", "javier", "mei", "pip", "sam"]

@pytest.mark.parametrize("patch, says", [
    ({"base": "nobody"}, "base: start from one of"),
    ({"onload": "alert(1)"}, "onload: not a sprite field"),                       # data only: nothing that could run
    ({"hair": "red"}, "hair: a #rrggbb color"),
    ({"style": "mohawk"}, "style: one of"),
    ({"name": "<b>x</b>"}, "name: 1 to 24"),
    ({"maps": [{"layer": "head", "at": [1, 3], "rows": ["kk"]}]}, "covers the face"),   # faces stay drawn by code
    ({"maps": [{"layer": "body", "at": [30, 0], "rows": ["k"]}]}, "is outside"),        # one size only
    ({"maps": [{"layer": "head", "at": [0, 12], "rows": ["zz"]}]}, '"z" isn\'t in pal'),
    ({"maps": [{"layer": "head", "at": [0, 12], "rows": ["s"], "pal": {"s": "#ff0000"}}]}, "can't be defined"),
    ({"quirk": {"dur": 2, "keys": [{"t": 1}, {"t": 0.5}]}}, "keys go in time order"),
    ({"quirk": {"dur": 2, "keys": [{"t": 0, "armF": ["wave", 0]}]}}, "armF: [mode, nudge]"),
    ({"quirk": {"dur": 2, "keys": [{"t": 0, "dance": True}]}}, "dance: unknown"),
    ({"fav": {"teleport": 2}}, "fav.teleport: not an action"),
])
def test_a_bad_spec_says_what_to_fix(patch, says):
    spec = dict(EXAMPLE, **patch)
    errors = js("R => R.validateSprite(SPEC).errors".replace("SPEC", json.dumps(spec)))
    assert any(says in e for e in errors), errors

def test_register_keeps_built_in_names_and_tidy_keys():
    r = js("""R => [R.registerSprite("pip", EX).errors, R.registerSprite("Custom Sprite!", EX).errors]""".replace("EX", json.dumps(EXAMPLE)))
    assert "built-in" in r[0][0] and "lowercase" in r[1][0]

def test_the_about_page_storyboard_runs_the_whole_strip_in_order():
    """The About page's scene (#139): every beat is one the director knows, on a step the strip shows; played with
    no clock (each actor until it waits on a signal), the work lights every step once, in strip order, and ends."""
    import sys
    sys.path.insert(0, str(ROOT))
    import flowchart
    r = js("R => ({ tour: R.TOUR, beats: Object.keys(R.BEATS), bits: Object.keys(R.TOUR_ACTS) })")
    tour, strip = r["tour"], [k for k, kind, _ in flowchart.FLOW if k not in flowchart.OUTREACH_STEPS]
    reviews = [k for k, kind, _ in flowchart.FLOW if kind == "human"]
    for who, beats in tour.items():
        for b in beats:
            assert b[0] in r["beats"], (who, b)
            if b[0] in ("enter", "go", "light", "approve"):
                assert b[1] in strip, (who, b)
            if b[0] == "do":
                assert b[1] in r["bits"], (who, b)
            if b[0] in ("cue", "face"):
                assert b[1] in tour, (who, b)
    lit, signals, queue, at = [], set(), [], {}
    playing = {"P": list(tour["P"]), "R": list(tour["R"])}
    while playing or queue:
        if not playing:
            who = queue.pop(0)
            playing[who] = list(tour[who])
        moved = False
        for who, beats in list(playing.items()):
            while beats:
                op, *args = beats[0]
                if op == "until" and args[0] not in signals:
                    break
                beats.pop(0)
                moved = True
                if op == "light":
                    lit.append(args[0])
                elif op == "signal":
                    signals.add(args[0])
                elif op == "cue":
                    queue.append(args[0])
            if not beats:
                del playing[who]
        assert moved or not playing, f"stuck: {playing}"
    assert lit == strip                                                # every box once, in the strip's order
    for k in reviews:                                                  # every review is approved after it lights
        assert any(b[:2] == ["approve", k] for beats in tour.values() for b in beats)
