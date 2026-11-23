"""Custom avatars (code, no model): the researchers people make in the avatar studio, and which sprites visit.

Everything lives in data/sprites.json, on this machine only:
    {"sprites": {"custom-sprite-1": spec, ...}, "off": ["pip", ...]}
A spec is the data-only sprite format in SPRITES.md; assets/researchers.js checks it fully (validateSprite) before
it is saved from the studio and again when any page loads it, so this module only keeps the file tidy: a sane key,
a JSON object that starts from a built-in sprite, a size limit. Every UI page hands the file to the sprites as
window.RESEARCHER_SPRITES (ui.page), so a saved avatar can turn up anywhere a researcher can."""
import json, re
from pathlib import Path
import settings

SPRITES_FILE = Path("data/sprites.json")
CYBORGS_FILE = Path(__file__).parent / "assets" / "cyborgs.json"   # the built-ins as cyborgs, one spec each (SPRITES.md)
BACKS_FILE = Path(__file__).parent / "assets" / "backs.json"       # what a sprite can wear on its back (SPRITES.md)
BUILT_IN = ("aris", "aisha", "ben", "mei", "sam", "evelyn", "david", "javier", "hanna", "kenji", "quill", "pip")
KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")    # the same rule as registerSprite
MAX_SPEC = 24_000                                  # bytes of JSON one spec may take
MAX_AVATARS = 40                                   # custom avatars kept at most
PREFIX = "custom-sprite-"

class AvatarError(ValueError):
    """Something a person can fix: the message says what."""

def _read() -> dict:
    try:
        data = json.loads(SPRITES_FILE.read_text())
    except (OSError, ValueError):
        return {"sprites": {}, "off": []}
    sprites = data.get("sprites") if isinstance(data.get("sprites"), dict) else {}
    off = [k for k in data.get("off", []) if isinstance(k, str)] if isinstance(data.get("off"), list) else []
    return {"sprites": {k: v for k, v in sprites.items() if KEY.match(k) and isinstance(v, dict)}, "off": off}

def _write(data: dict) -> None:
    SPRITES_FILE.parent.mkdir(parents=True, exist_ok=True)
    SPRITES_FILE.write_text(json.dumps(data, indent=2) + "\n")

def custom() -> dict[str, dict]:
    """Every saved avatar, by key, oldest first."""
    return _read()["sprites"]

def off() -> list[str]:
    """Sprites switched off: they never visit (a crew switched off entirely is kept on by the page)."""
    return _read()["off"]

def cyborgs() -> dict[str, dict]:
    """The cyborg crew: every built-in researcher drawn again in metal, by key ("kenji-borg"). {} if the file is gone."""
    try:
        data = json.loads(CYBORGS_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if KEY.match(k) and isinstance(v, dict) and v.get("base") in BUILT_IN}

def config() -> dict:
    """What a page hands to assets/researchers.js: {} when nothing is customised. With Cyborg sprites on
    (settings.cyborgs), `cyborgs` holds the cyborg crew, and the page sends each in place of its original."""
    data = _read()
    out = {}
    if data["sprites"]:
        out["add"] = data["sprites"]
    if data["off"]:
        out["off"] = data["off"]
    if settings.cyborgs():
        borgs = {k: v for k, v in cyborgs().items() if k not in data["sprites"]}
        if borgs:
            out["cyborgs"] = borgs
    return out

def backs() -> dict[str, dict]:
    """Every back piece by name (assets/backs.json); assets/researchers.js checks each one. {} if the file is gone."""
    try:
        data = json.loads(BACKS_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if KEY.match(k) and isinstance(v, dict)} if isinstance(data, dict) else {}

def backs_tag() -> str:
    """window.RESEARCHER_BACKS for any page that draws researchers, set before assets/researchers.js loads."""
    raw = json.dumps(backs(), separators=(",", ":")).replace("<", "\\u003c")
    return f"<script>window.RESEARCHER_BACKS={raw};</script>"

def script_tag() -> str:
    """window.RESEARCHER_SPRITES for the page head, or "" with nothing customised. The JSON can't close the tag."""
    cfg = config()
    if not cfg:
        return ""
    raw = json.dumps(cfg, separators=(",", ":")).replace("<", "\\u003c")
    return f"<script>window.RESEARCHER_SPRITES={raw};</script>"

def slug(text: str) -> str:
    """A key from a name: "Dr. Nova" -> "dr-nova"; "" if nothing usable is left."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:32].strip("-")
    return s if s and KEY.match(s) else ""

def next_key() -> str:
    """The first free custom-sprite-N."""
    taken = set(custom())
    n = 1
    while f"{PREFIX}{n}" in taken:
        n += 1
    return f"{PREFIX}{n}"

def check(key: str, spec) -> None:
    if not KEY.match(key or ""):
        raise AvatarError("A key is lowercase letters, digits and dashes, up to 32, like custom-sprite-1.")
    if key in BUILT_IN:
        raise AvatarError(f"{key} is a built-in researcher; save your avatar under another name.")
    if not isinstance(spec, dict) or spec.get("base") not in BUILT_IN:
        raise AvatarError("An avatar starts from one of the built-in researchers.")
    if len(json.dumps(spec)) > MAX_SPEC:
        raise AvatarError("That avatar is too big to save; draw smaller pixel maps.")

def save(key: str, spec: dict, replace: str = "") -> str:
    """Saves an avatar under key and switches it on. `replace` is the key it was opened under, so editing one keeps
    its place in the order (and renames it, if the key changed). Returns the key."""
    check(key, spec)
    data = _read()
    if key in data["sprites"] and key != replace:
        raise AvatarError(f"There's already an avatar called {key}; pick another name.")
    if replace not in data["sprites"] and len(data["sprites"]) >= MAX_AVATARS:
        raise AvatarError(f"You have {MAX_AVATARS} avatars already; delete one first.")
    sprites = {}
    for k, v in data["sprites"].items():
        sprites[key if k == replace else k] = spec if k == replace else v
    sprites.setdefault(key, spec)
    data["sprites"] = sprites
    data["off"] = [k for k in data["off"] if k not in (key, replace)]
    _write(data)
    return key

def delete(key: str) -> bool:
    data = _read()
    if key not in data["sprites"]:
        return False
    del data["sprites"][key]
    data["off"] = [k for k in data["off"] if k != key]
    _write(data)
    return True

def set_on(key: str, on: bool) -> None:
    """Switches a built-in or saved avatar on or off."""
    data = _read()
    if key not in BUILT_IN and key not in data["sprites"]:
        raise AvatarError(f"There is no researcher called {key}.")
    data["off"] = [k for k in data["off"] if k != key] + ([] if on else [key])
    _write(data)
