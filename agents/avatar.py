"""The avatar designer: one structured model call that changes a sprite spec from what a person types (and an image,
if they attach one and the model can see).

The spec is data only (SPRITES.md), so the model never writes code that runs. Its reply is held to a JSON schema
built from the parts that exist (`catalog`, which the studio sends from assets/researchers.js), so even a small
local model can only pick a hairstyle, hat or outfit that is really there, and a color that is a color. It answers
with just the fields it changes; pixel maps (ears, a scarf) are the one place it draws. Its rules are SPRITES.md
itself, read fresh each time, so the rulebook and what the model is told can't drift apart. The browser checks
every result with validateSprite and sends the problems back for a repair round (`errors`), so this module keeps no
copy of the rules. Nudity and the like are refused before any model is asked; the model is told to refuse them
too. Not loop work: the stop button doesn't apply, thinking is off so it answers in seconds, and nothing is saved."""
import asyncio, base64, binascii, json, re
from pathlib import Path
import ollama
import settings
from agents import llm, providers

RULEBOOK = Path(__file__).resolve().parent.parent / "SPRITES.md"
TIME_LIMIT = 180                         # seconds one edit may take; a small local model answers in seconds
MAX_MESSAGE = 1000                       # characters of the request
MAX_IMAGE = 2_000_000                    # bytes of an attached image, after decoding
REFUSE = re.compile(r"\b(nude|nudity|naked|nsfw|topless|bottomless|lingerie|porn\w*|sexy|sexual|erotic\w*|"
                    r"undress\w*|hentai|explicit)\b", re.I)
REFUSAL = ("I can't draw that: everyone here stays clothed and work-friendly. Want a new outfit, a hat or a "
           "different colour scheme instead?")
NOTHING = "I couldn't work out a change from that. Try naming a piece or a colour"
PICTURE_TIP = ", or pick a bigger model: small ones read pictures poorly"
# What a catalog may name, and the lists it must hold to describe a spec (assets/researchers.js PARTS, FLAG_KEYS,
# COLOR_KEYS). Anything missing falls back to a schema that only checks the reply's shape.
ENUMS = ("worker", "style", "head", "outfit", "face", "facial", "legs", "brow", "mouth", "prop")
POSE = ("eyes", "arm", "layer", "icon")
# The order fields may appear in an answer. Ollama writes a schema's properties in order and can't go back, so each
# part sits next to its own colors (the hat, then the hat's color), the way a person describes a look.
ORDER = ("name", "worker", "skin", "metal", "glow", "style", "hair", "ahoge", "head", "cap", "beanie", "pom", "hijab", "face", "frame",
         "facial", "brow", "mouth", "lash", "lids", "bushy", "freckles", "grin", "outfit", "top", "shirt", "tie", "bow",
         "legs", "pants", "skirt", "shoes", "pack", "rolled", "pencil", "prop", "maps", "propMap", "quirk")

PROMPT = """You are the avatar designer in a research app. Every avatar is a small pixel-art researcher drawn
from a JSON spec that starts from a built-in researcher. Change it to give the person what they ask for.

How to work:
- Pick existing parts and colors first: they always look right and animate. The answer's schema lists them.
- Draw pixel maps only for something no part can do (ears, horns, a scarf, a badge). Keep maps small, 2 to 4
  colors, and never on the face: the face is drawn by code. `maps` replaces all maps, so include any to keep.
- In `change`, put ONLY the fields you change; leave out everything else. null clears a part (no hat).
- Add a quirk only when they ask for a habit, a mood or a vibe; never on its own.
- Use the person's image, if there is one, for colors and style: a tiny cartoon, not a portrait.
- Refuse nudity, sexual content, gore, hate symbols, real people's likenesses and logos: refused true, change {},
  and suggest something that fits.
- reply: one or two friendly sentences saying what you changed (or why not), matching `change` exactly.
- Colors: `top` is the outfit's main color, `shirt` what shows under it, plus `hair`, `pants`, `shoes`, and the hat's
  own (`cap`, `beanie`, `pom`, `hijab`) or the glasses' `frame`. A robot or cyborg: `metal` (the metal in place of
  skin) and `glow` (the light its eyes glow with).

Examples (the avatar is a researcher in a lab coat unless its spec says otherwise):
- "a red beanie and a green hoodie" -> {"refused": false, "change": {"head": "beanie", "beanie": "#c0392b",
  "outfit": "hoodie", "top": "#2f9e44"}, "reply": "Red beanie on, and a green hoodie to match."}
- "make them grumpy" -> {"refused": false, "change": {"brow": "stern", "mouth": "flat"}, "reply": "Now they frown at
  every footnote."}
- "cat ears" -> {"refused": false, "change": {"maps": [{"layer": "head", "at": [-4, 14], "rows": [".h.....h.",
  "hrh...hrh", "hhh...hhh"], "pal": {"r": "#ffb3c7"}}]}, "reply": "Two cat ears in their hair colour, pink inside."}
- "a yellow scarf" -> {"refused": false, "change": {"maps": [{"layer": "body", "at": [-5, 9], "rows": ["yyyyyyyyyy",
  "yYyYyYyYyy", ".....yy...", ".....Yy..."], "pal": {"y": "#f1c40f", "Y": "#d4a017"}}]}, "reply": "A cozy yellow
  scarf, with a tail."}
- anything with nudity -> {"refused": true, "change": {}, "reply": "I keep everyone clothed. How about a new outfit?"}

The rulebook (SPRITES.md):
"""

def rulebook() -> str:
    """The parts of SPRITES.md a model needs: the rules, the fields, maps, props, quirks and the advice for a model."""
    keep = []
    for section in re.split(r"(?m)^## ", RULEBOOK.read_text())[1:]:
        if not section.startswith(("How animation works", "Using sprites from a page")):
            keep.append("## " + section.strip())
    return "\n\n".join(keep)

def refuses(message: str) -> bool:
    return bool(REFUSE.search(message or ""))

# ---------- the answer's schema ----------

HEX = {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"}
INT = {"type": "integer"}

def _values(catalog: dict, name: str) -> list | None:
    v = catalog.get(name)
    if not isinstance(v, list) or not v or len(v) > 40:
        return None
    v = [x for x in v if x is None or (isinstance(x, str) and len(x) <= 40)]
    return v or None

def schema(catalog: dict | None) -> dict:
    """The JSON schema the model answers in: {"reply", "refused", "change"}, where change may only name parts the
    catalog lists. Without a usable catalog, change is any object (the browser still checks it)."""
    top = {"refused": {"type": "boolean"}}             # decided first, then the change, then the reply that describes it
    reply = {"reply": {"type": "string", "maxLength": 400}}
    lists = {k: _values(catalog or {}, k) for k in ENUMS + POSE}
    flags = [f for f in (catalog or {}).get("flags", []) if isinstance(f, str)][:20]
    colors = [c for c in (catalog or {}).get("colors", []) if isinstance(c, str)][:20]
    if not all(lists[k] for k in ENUMS) or not flags or not colors:
        return {"type": "object", "properties": {**top, "change": {"type": "object"}, **reply}, "required": ["refused", "change", "reply"]}
    arm = {"type": "array", "prefixItems": [{"enum": lists["arm"] or ["swing"]}, {"type": "integer", "minimum": -4, "maximum": 4}],
           "minItems": 2, "maxItems": 2}
    rows = {"type": "array", "items": {"type": "string", "maxLength": 24}, "minItems": 1, "maxItems": 24}
    picture = {"at": {"type": "array", "prefixItems": [INT, INT], "minItems": 2, "maxItems": 2}, "rows": rows,
               "pal": {"type": "object", "additionalProperties": HEX}}
    key = {"type": "object", "properties": {
        "t": {"type": "number", "minimum": 0, "maximum": 6}, "armF": arm, "armB": arm,
        "eyes": {"enum": lists["eyes"] or ["open"]}, "mouth": {"enum": lists["mouth"]}, "brow": {"enum": lists["brow"]},
        "hop": {"type": "number", "minimum": 0, "maximum": 10}, "blush": {"type": "boolean"}, "glint": {"type": "boolean"},
        "icon": {"enum": lists["icon"] or ["heart"]}, "sparkle": {"type": "boolean"}}, "required": ["t"], "additionalProperties": False}
    change = {
        "name": {"type": "string", "maxLength": 24},
        **{k: {"enum": lists[k]} for k in ENUMS},
        "skin": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 7}, "minItems": 1, "maxItems": 3},
        **{c: HEX for c in colors},
        **{f: {"type": "boolean"} for f in flags},
        "maps": {"type": "array", "maxItems": 4, "items": {"type": "object", "properties": {
            "layer": {"enum": lists["layer"] or ["head"]}, **picture}, "required": ["layer", "at", "rows"], "additionalProperties": False}},
        "propMap": {"type": "object", "properties": picture, "required": ["at", "rows"], "additionalProperties": False},
        "quirk": {"type": "object", "properties": {
            "dur": {"type": "number", "minimum": 0.5, "maximum": 6}, "keys": {"type": "array", "items": key, "minItems": 1, "maxItems": 8},
            "say": {"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 3}},
            "required": ["dur", "keys"], "additionalProperties": False},
    }
    change = {k: change[k] for k in sorted(change, key=lambda k: ORDER.index(k) if k in ORDER else len(ORDER))}
    return {"type": "object", "properties": {**top, "change": {"type": "object", "properties": change, "additionalProperties": False}, **reply},
            "required": ["refused", "change", "reply"]}

SLOTS = set("sShHltTcpPokw")             # letters a map uses for this visit's colors (assets/researchers.js SLOTS)

def tidy_picture(m) -> None:
    """Fixes, in place, the two slips small models make drawing a map that code can fix without guessing: a palette
    letter that is also a color slot gets a free letter instead, and short rows are padded with empty pixels."""
    if not isinstance(m, dict):
        return
    pal = m.get("pal") if isinstance(m.get("pal"), dict) else {}
    free = [c for c in "abdefgijmnqruvxyzABCDEFGIJKLMNOQRUVWXYZ0123456789" if c not in pal]
    swap = {ch: free.pop(0) for ch in list(pal) if ch in SLOTS and free}
    if swap:
        m["pal"] = {swap.get(k, k): v for k, v in pal.items()}
    if isinstance(m.get("rows"), list):
        m["rows"] = _pad([_swap(r, swap) for r in m["rows"] if isinstance(r, str)])
    if isinstance(m.get("frames"), list):
        m["frames"] = [_pad([_swap(r, swap) for r in f if isinstance(r, str)]) for f in m["frames"] if isinstance(f, list)]

def _swap(row: str, swap: dict) -> str:
    return "".join(swap.get(ch, ch) for ch in row)

def _pad(rows: list[str]) -> list[str]:
    w = max((len(r) for r in rows), default=0)
    return [r.ljust(w, ".") for r in rows]

def tidy(change: dict) -> dict:
    for m in change.get("maps", []) if isinstance(change.get("maps"), list) else []:
        tidy_picture(m)
    tidy_picture(change.get("propMap"))
    return change

def merge(spec: dict, change: dict) -> dict:
    """The spec with a model's change laid over it: base never changes, an empty maps list removes the maps."""
    out = dict(spec)
    for k, v in (change or {}).items():
        if k == "base":
            continue
        if k == "maps" and v == []:
            out.pop("maps", None)
        else:
            out[k] = v
    return out

# ---------- models ----------

def can_see(model: str) -> bool | None:
    """True if Ollama says the model takes images, False if it says it doesn't, None if nobody can tell (a cloud
    model, or Ollama closed)."""
    provider, name = settings.split_model(model)
    if provider != settings.LOCAL:
        return None
    try:
        caps = ollama.Client(host=settings.ollama_host(), timeout=5).show(name).capabilities or []
    except Exception:
        return None
    return "vision" in caps

def models() -> list[dict]:
    """Models for the studio's picker, the chat's default first: [{"name", "vision"}] with vision True, False or
    None (can't tell). Ollama's installed models, then each connected cloud provider's. Never raises."""
    default = settings.chat_model()
    found = []
    try:
        for m in sorted(ollama.Client(host=settings.ollama_host(), timeout=3).list().models, key=lambda m: m.model):
            found.append({"name": m.model, "vision": can_see(m.model)})
    except Exception:
        pass
    for pid in settings.cloud_providers():
        found += [{"name": settings.model_ref(pid, n), "vision": None} for n in providers.models(pid)[0]]
    if default and not any(f["name"] == default for f in found):
        found.insert(0, {"name": default, "vision": can_see(default)})
    return sorted(found, key=lambda f: f["name"] != default)

# ---------- the call ----------

def _image(b64: str | None) -> bytes | None:
    if not b64:
        return None
    try:
        raw = base64.b64decode(b64.split(",", 1)[-1], validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("That image didn't arrive in one piece; attach it again.")
    if len(raw) > MAX_IMAGE:
        raise ValueError("That image is too big; attach a smaller one.")
    return raw

def messages(spec: dict, look: dict, message: str, errors: list[str] | None = None, image: bytes | None = None,
             see: bool | None = None, local: bool = True) -> list:
    """The system prompt (with the rulebook) and the request. An image goes along only to a local model that may
    see it; Ollama takes images on the message, the cloud providers' adapters don't carry them."""
    ask = (f"How the avatar looks now (the spec laid over its starting researcher):\n{json.dumps(look)}\n\n"
           f"Its spec:\n{json.dumps(spec)}\n\nThe person asks: {message.strip()}")
    if errors:
        ask += ("\n\nThat spec breaks these rules. Fix every one in `change`:\n- "
                + "\n- ".join(str(e)[:300] for e in errors[:20]))
    if image is not None:
        ask += ("\n\nThey attached an image: take its colors and style." if see is not False else
                "\n\n(They attached an image, but this model can't see images: go by their words.)")
    user: dict = {"role": "user", "content": ask}
    if image is not None and see is not False and local:
        user["images"] = [image]
    return [{"role": "system", "content": PROMPT + rulebook()}, user]

async def _ask(model: str, msgs: list, fmt: dict) -> dict:
    error = None
    for _ in range(2):                    # one repair round for JSON that doesn't parse
        msg = await asyncio.wait_for(llm.chat(msgs, format=fmt, model=model, stoppable=False, think=False), TIME_LIMIT)
        try:
            out = json.loads(msg.content or "")
            if isinstance(out, dict) and isinstance(out.get("reply"), str):
                return out
            error = "the answer needs reply, refused and change"
        except ValueError as e:
            error = str(e)
        msgs = msgs + [{"role": "assistant", "content": msg.content or ""},
                       {"role": "user", "content": f"That wasn't valid ({error}). Answer again with JSON only."}]
    raise RuntimeError(f"the model couldn't write a valid reply: {error}")

def edit(spec: dict, message: str, look: dict | None = None, model: str | None = None, image: str | None = None,
         errors: list[str] | None = None, catalog: dict | None = None) -> dict:
    """{"reply", "refused", "spec", "change", "model", "saw_image"}: the spec with the model's change laid over it,
    which the browser still checks."""
    message = (message or "").strip()[:MAX_MESSAGE]
    done = lambda reply, refused=True: {"reply": reply, "refused": refused, "spec": spec, "change": {}, "model": "", "saw_image": False}
    if refuses(message):
        return done(REFUSAL)
    if not message and not image:
        return done("Tell me what to change, or attach a picture to go by.")
    model = model or settings.chat_model()
    if not model:
        raise RuntimeError("no model chosen yet; pick one on the Settings page")
    raw = _image(image)
    local = settings.split_model(model)[0] == settings.LOCAL
    see = can_see(model) if raw is not None else None
    msgs = messages(spec, look or spec, message or "Make it match the picture.", errors, raw, see, local)
    out = asyncio.run(_ask(model, msgs, schema(catalog)))
    refused = bool(out.get("refused"))
    change = tidy(out.get("change")) if isinstance(out.get("change"), dict) and not refused else {}
    now = look or spec                     # a "change" that only repeats what is already there changes nothing
    change = {k: v for k, v in change.items() if k != "base" and now.get(k, spec.get(k)) != v}
    saw = raw is not None and see is not False and local
    reply = out["reply"].strip()[:600] or "Done."
    if not change and not refused:         # don't repeat a reply that claims what didn't happen
        reply = NOTHING + (PICTURE_TIP if raw is not None else "") + "."
    if raw is not None and not saw:
        reply += " (This model couldn't see your picture, so I went by your words.)"
    return {"reply": reply, "refused": refused, "spec": merge(spec, change), "change": change, "model": model, "saw_image": saw}
