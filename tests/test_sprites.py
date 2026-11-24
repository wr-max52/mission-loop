"""The sprite rulebook (SPRITES.md) in code: specs are data, checked before they join the cast."""
import json, re, shutil, subprocess
from pathlib import Path
import pytest
import avatars

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
window.RESEARCHER_BACKS = %(backs)s;
eval(fs.readFileSync(%(script)s, "utf8"));
console.log(JSON.stringify((%(body)s)(window.Researchers)));
"""

def js(body: str, config: dict | None = None):
    src = HARNESS % {"config": json.dumps(config or {}), "backs": json.dumps(avatars.backs()), "script": json.dumps(str(SCRIPT)), "body": body}
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

def test_a_custom_sprite_keeps_the_skin_it_was_given_on_every_visit():
    # The strip and the cards give each visit a new seed; a tone picked in the builder must survive all of them.
    r = js("""R => { R.registerSprite("custom-sprite-1", Object.assign({}, EX, { skin: [6] }));
                     R.registerSprite("custom-sprite-2", { base: "ben", name: "Ben 2" });
                     const seeds = Array.from({ length: 200 }, (_, i) => i + 1);
                     return { picked: [...new Set(seeds.map(s => R.makeLook("custom-sprite-1", s).skin))],
                              want: R.makeLook("custom-sprite-1", 1).skin,
                              likeStart: [...new Set(seeds.map(s => R.makeLook("custom-sprite-2", s).skin))].length,
                              builtIn: [...new Set(seeds.map(s => R.makeLook("ben", s).skin))].length }; }"""
           .replace("EX", json.dumps(EXAMPLE)))
    assert r["picked"] == [r["want"]]          # one tone, every visit
    assert r["likeStart"] <= 3                 # "Like the original": only its start's own tones (ben has 3)
    assert r["builtIn"] > 3                    # built-ins still turn up in other tones now and then

def test_register_keeps_built_in_names_and_tidy_keys():
    r = js("""R => [R.registerSprite("pip", EX).errors, R.registerSprite("Custom Sprite!", EX).errors]""".replace("EX", json.dumps(EXAMPLE)))
    assert "built-in" in r[0][0] and "lowercase" in r[1][0]

# ---------- the cyborg crew (assets/cyborgs.json) ----------

CYBORGS = json.loads((ROOT / "assets" / "cyborgs.json").read_text())

def test_every_built_in_has_a_cyborg_that_follows_the_rulebook():
    assert sorted(CYBORGS) == sorted(f"{k}-borg" for k in avatars.BUILT_IN)
    assert all(spec["base"] == key[:-5] and spec["metal"] and spec["glow"] and spec["maps"] for key, spec in CYBORGS.items())
    errors = js("R => Object.fromEntries(Object.entries(BORGS).map(([k, s]) => [k, R.validateSprite(s).errors]))".replace("BORGS", json.dumps(CYBORGS)))
    assert all(e == [] for e in errors.values()), errors

def test_a_cyborg_keeps_its_original_s_clothes_crew_and_quirk():
    r = js("""R => { R.registerSprite("ben-borg", BORG); const a = R.makeLook("ben", 5), b = R.makeLook("ben-borg", 5);
               return { same: ["top", "pants", "shoes", "hair", "outfit", "worker", "legs", "torso"].every(k => a[k] === b[k]),
                        skin: b.skin, metal: b.metal, eye: b.eye, base: b.base, rosy: b.rosy, human: [a.metal, a.glow] }; }"""
           .replace("BORG", json.dumps(CYBORGS["ben-borg"])))
    assert r == {"same": True, "skin": CYBORGS["ben-borg"]["metal"], "metal": CYBORGS["ben-borg"]["metal"],
                 "eye": CYBORGS["ben-borg"]["glow"], "base": "ben", "rosy": False, "human": [None, None]}

def test_kenji_borg_is_black_with_grey_armor_and_red_lights():
    k = CYBORGS["kenji-borg"]
    dark = lambda c: max(int(c[i:i + 2], 16) for i in (1, 3, 5)) < 48
    assert all(dark(k[f]) for f in ("metal", "hair", "top", "pants", "shirt")) and k["glow"] == "#ff2a2a"
    pals = [m["pal"] for m in k["maps"]]
    assert all(p["r"] == "#ff2a2a" for p in pals)                                    # red buttons on every plate
    assert all(len({p[c][1:3], p[c][3:5]}) == 1 or abs(int(p[c][1:3], 16) - int(p[c][5:7], 16)) < 24 for p in pals for c in "ieE")   # grey armor

def test_cyborgs_join_their_crews_beside_everyone_else():
    r = js("R => ({ borgs: R.CYBORGS.length, on: R.enabled(), pi: R.crewOf('pi'), assistant: R.crewOf('assistant') })",
           {"cyborgs": CYBORGS, "add": {"custom-sprite-1": EXAMPLE}, "off": ["pip"]})
    assert r["borgs"] == 12
    assert sorted(r["on"]) == sorted([k for k in avatars.BUILT_IN if k != "pip"] + [f"{k}-borg" for k in avatars.BUILT_IN]
                                     + ["custom-sprite-1"])                     # pip stays home; pip-borg still comes
    assert {"kenji", "kenji-borg"} <= set(r["pi"]) and {"mei", "mei-borg", "custom-sprite-1"} <= set(r["assistant"])

def test_without_the_switch_no_cyborg_is_in_the_cast():
    assert js("R => [R.CYBORGS, R.CAST.length]") == [[], 12]

# ---------- back pieces (assets/backs.json, #152) ----------

BACKS = json.loads((ROOT / "assets" / "backs.json").read_text())

def drawn(body: str):
    """Runs body(R, pixels) on the real renderer (the contact-sheet tool's canvas); pixels(spec, seed) is a still's bytes."""
    src = """
const { load, Canvas } = require(%s);
const R = load(%s, {});
const pixels = (spec, seed = 11, face = -1) => { const c = new Canvas(); const v = R.renderSprite(c, spec, { action: "rest", t: 0.5, seed, face, width: 60, height: 62 });
  if (!v.ok) throw new Error(v.errors.join("; ")); return Buffer.from(c.getContext("2d").d).toString("base64"); };
console.log(JSON.stringify((%s)(R, pixels)));
""" % (json.dumps(str(ROOT / "tools" / "sprite-sheet.js")), json.dumps(str(SCRIPT)), body)
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])

def test_every_back_piece_follows_the_map_rules():
    r = js("R => ({ backs: Object.keys(R.BACKS), parts: R.PARTS.back, errors: Object.fromEntries(Object.entries(PIECES).map(([k, p]) => [k, R.checkBack(p, k)])) })"
           .replace("PIECES", json.dumps(BACKS)))
    assert r["backs"] == list(BACKS) and r["parts"] == [None, *BACKS] and all(e == [] for e in r["errors"].values()), r["errors"]
    assert js("R => R.checkBack({ name: 'X', at: [-12, 0], rows: ['ss'], tint: { s: 1 } }, 'x')") != []   # a tint letter can't be a slot
    assert js("R => R.checkBack({ name: 'X', at: [10, 0], rows: ['aa'], tint: { a: 1 } }, 'x')") != []     # nor reach outside the back's box

def test_a_back_piece_is_worn_by_name_or_taken_off():
    r = js("""R => ({ ben: R.makeLook("ben", 3).back, mei: R.makeLook("mei", 3).back,
      off: R.validateSprite({ base: "ben", name: "B", back: null }).ok, jet: R.validateSprite({ base: "mei", name: "M", back: "jetpack" }).ok,
      bad: R.validateSprite({ base: "mei", name: "M", back: "nope" }).errors })""")
    assert r["ben"] == "backpack" and r["mei"] is None and r["off"] and r["jet"]      # a pack colour and no `back`: a backpack
    assert len(r["bad"]) == 1 and r["bad"][0].startswith("back: one of null, backpack, radio-pack")

def test_taking_the_backpack_off_takes_the_strap_with_it():
    r = drawn("""(R, px) => ({ worn: px({ base: "ben", name: "B" }) === px({ base: "ben", name: "B", back: null }),
      bare: px({ base: "ben", name: "B", back: null }) === px({ base: "ben", name: "B", back: null, pack: ["#ff00ff", "#00ff00"] }),
      strap: px({ base: "ben", name: "B", back: null }, 11, 1) === px({ base: "ben", name: "B", back: null, pack: ["#ff00ff", "#00ff00"] }, 11, 1) })""")
    assert r == {"worn": False, "bare": True, "strap": True}      # off, the pack colour shows nowhere: no bag, no strap

def test_every_cyborg_looks_the_same_with_its_back_piece_as_with_its_old_map():
    old = {k: v for k, v in CYBORGS.items() if "back" in v}
    r = drawn("""(R, px) => {
  const out = {};
  for (const [k, spec] of Object.entries(OLD)) {
    const piece = R.BACKS[spec.back], map = { layer: "back", at: piece.at, pal: Object.assign({}, piece.pal) };
    if (piece.frames) { map.frames = piece.frames; map.fps = piece.fps; } else map.rows = piece.rows;
    const tinted = !!piece.tint, before = Object.assign({}, spec, { back: tinted ? "backpack" : null, maps: [...spec.maps, map] });
    if (tinted) {                                   // the radio pack was the backpack with an antenna map over it
      const bag = R.BACKS.backpack.rows.length, h = map.frames ? map.frames[0].length : map.rows.length;
      map.frames = map.frames.map(f => f.slice(0, h - bag + 2).map(r => r.replace(/[^nNE]/g, ".")));   // just the antenna
      map.at = [piece.at[0], piece.at[1] + bag - 2];
    }
    const seeds = [1, 2, 3, 5, 8, 11, 13, 21].filter(s => !tinted || R.makeLook(k.replace("-borg", ""), s).torso === 12);
    out[k] = seeds.length > 0 && seeds.every(s => px(Object.assign({}, spec), s) === px(before, s));
  }
  return out;
}""".replace("OLD", json.dumps(old)))
    assert r == {k: True for k in old}, r

def about_strip():
    import sys
    sys.path.insert(0, str(ROOT))
    import flowchart
    return flowchart.FLOW

def route_js():
    return js("R => ({ stations: R.STATIONS, routes: R.ROUTES, beats: Object.keys(R.BEATS), bits: Object.keys(R.TOUR_ACTS), vars: Object.keys(R.TOUR_VARS) })")

def check_route(stops, r, flow):
    kinds = {k: kind for k, kind, _ in flow}
    for step, *beats in stops:
        assert step in kinds, step
        assert not beats or step in r["stations"], (step, beats)       # a check or a review takes no beats
        for b in beats:
            assert b[0] in r["beats"], (step, b)
            if b[0] == "do":
                assert b[1] in r["bits"], (step, b)
            if b[0] == "say":
                assert set(re.findall(r"\{(\w+)\}", b[1])) <= set(r["vars"]), (step, b)
    return [step for step, *_ in stops]

def test_the_about_page_has_a_researcher_on_every_agents_box():
    """#155: one researcher per agent's box on the About strip, from the crew that runs it (anyone at Publish)."""
    r, flow = route_js(), about_strip()
    assert sorted(r["stations"]) == sorted(k for k, kind, _ in flow if kind == "agent")
    assert r["stations"] == {"theorist": "pi", "finder": "assistant", "sources": "assistant", "brief": "pi",
                             "outreach": "pi", "writer": "pi", "article_rating": "assistant", "publisher": "desk"}

def test_the_long_trip_passes_its_paper_through_every_box_once_in_order():
    """The About page's long trip (#139): its paper visits every box on the strip, outreach included, once and in
    strip order, and every agent's box has something to say or do with it."""
    r, flow = route_js(), about_strip()
    stops = r["routes"]["tour"]["stops"]
    assert check_route(stops, r, flow) == [k for k, _, _ in flow]
    assert all(len(stop) > 1 for stop in stops if stop[0] in r["stations"])

def test_the_loops_pass_their_paper_back_between_two_boxes_then_on():
    """The loops (#155): each paper goes back to the box before once (another angle, one more source, a draft turned
    down) and then on along the strip to a review or Publish."""
    r, flow = route_js(), about_strip()
    order = [k for k, _, _ in flow]
    steps = {name: check_route(route["stops"], r, flow) for name, route in r["routes"].items() if name != "tour"}
    assert steps == {
        "theorist": ["theorist", "theorist_check", "finder", "theorist", "finder", "finder_check", "epic_review"],
        "sources": ["sources", "brief", "sources", "brief", "source_check", "brief_review"],
        "outreach": ["outreach", "outreach_check", "replies", "writer", "article_check", "article_rating", "writer",
                     "article_check", "article_rating", "rating_check", "article_review", "publisher"],
    }
    for name, s in steps.items():
        back = [i for i in range(1, len(s)) if order.index(s[i]) < order.index(s[i - 1])]
        assert len(back) == 1, name                                    # passed back once, everything else forward
    assert len({route["ink"] for route in r["routes"].values()}) == len(r["routes"])   # each trip's paper has its own colour

def test_researchers_on_the_strip_take_turns_to_speak():
    """Two bubbles never land on each other (#155): a line waits while someone near on the same row is talking, and
    is said once their bubble is gone; someone far along the row, or on another row, talks at once."""
    r = js("""R => {
      const crowded = R.Tour.prototype.crowded, me = { x: 100, alpha: 1, stage: { ground: 50 } };
      const them = (x, ground, t) => ({ x, alpha: 1, stage: { ground }, bubble: { text: "Pages for you.", t, dur: 2 } });
      const tour = a => ({ actors: [me, a], bubbleW: t => t.length * 6 + 12 });
      return { near: crowded.call(tour(them(130, 50, 0.5)), me, "One more source!"),
               far: crowded.call(tour(them(400, 50, 0.5)), me, "One more source!"),
               below: crowded.call(tour(them(130, 120, 0.5)), me, "One more source!"),
               done: crowded.call(tour(them(130, 50, 1.95)), me, "One more source!") };
    }""")
    assert r == {"near": True, "far": False, "below": False, "done": False}

def test_an_arm_keeps_its_length_wherever_the_hand_goes():
    # #146: a raised, forward or lowered arm is one length; a hand out of reach is pulled in, never stretched to.
    worst = js("""R => { let worst = 0; const [U, F] = R.ARM;
        for (let x = -18; x <= 18; x++) for (let y = -18; y <= 18; y++) for (const fwd of [false, true]) {
          const [e, h] = R.reach(0, 0, x, y, fwd);
          worst = Math.max(worst, Math.abs(Math.hypot(e[0], e[1]) - U), Math.abs(Math.hypot(h[0] - e[0], h[1] - e[1]) - F));
          if (Math.hypot(x, y) <= U + F - 1 && Math.hypot(x, y) >= 3) worst = Math.max(worst, Math.hypot(h[0] - x, h[1] - y));   // in reach: the hand gets there
        }
        return worst; }""")
    assert worst <= 1.5, worst                                     # whole pixels: half a pixel of rounding at each end

def test_every_bit_draws_for_every_researcher():
    # The real renderer on the contact-sheet tool's canvas: every bit, every built-in and cyborg, a few moments each.
    src = """
const { load, Canvas } = require(%s);
const R = load(%s, { cyborgs: JSON.parse(require("fs").readFileSync(%s, "utf8")) });
const cast = [...Object.keys(R.ROSTER)], bits = Object.keys(R.ACTIONS).filter(a => !["spawn", "despawn", "farewell", "chase"].includes(a));
let n = 0; const bad = [];
for (const k of cast) for (const a of bits) for (const t of [0.3, 1.6]) {
  try { const v = R.renderSprite(new Canvas(), k, { action: a, t, effects: true }); if (!v.ok) bad.push([k, a, v.errors]); n++; }
  catch (e) { bad.push([k, a, String(e)]); }
}
console.log(JSON.stringify({ n, cast: cast.length, bits: bits.length, bad: bad.slice(0, 5) }));
""" % tuple(json.dumps(str(p)) for p in (ROOT / "tools" / "sprite-sheet.js", SCRIPT, ROOT / "assets" / "cyborgs.json"))
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r["bad"] == [] and r["cast"] == 24 and r["bits"] >= 43 and r["n"] == r["cast"] * r["bits"] * 2, r
