# SPRITES.md: how to draw a researcher sprite

The pixel researchers in `assets/researchers.js` are drawn by code, part by part, every frame. A new sprite is
**not code**: it is a small JSON spec that starts from one of the 12 built-in sprites and changes what it wants.
The renderer does everything else, including every animation. This file is the rulebook for writing a spec, for
people and for a model asked to draw one.

Try a spec in the playground: serve the repo (`python -m http.server 8765`) and open
`http://127.0.0.1:8765/tools/sprites.html`. `tools/example-sprite.json` is a complete example (Dr. Nova: Mei with
cat ears, a tail that wags, a flask and her own quirk).

## How animation works (why a spec never animates anything)

Each frame an action (walk, read, dig, eureka, ...) returns a **pose**: where the arms are (`["up", 0]`), the leg
swing, a crouch, a nod, the eyes, the mouth, the brows. The body is then drawn from that pose: legs, torso, head
and arms are placed from anchors (hips, shoulders, the head's base row, each hand), and every part is drawn
relative to its anchor. So anything a spec adds is pinned to an anchor and moves with every pose for free: a hat on
the head nods and jumps with it, a tail on the hips bobs with the walk, a prop in the hand goes where the hand
goes. Facing left, falling over, squashing and fading happen to the whole picture afterwards.

What a spec can't get for free, it gets from a short list of shortcuts instead of code:

| You want | Use |
|---|---|
| A different hairstyle, hat, outfit, glasses | the parts lists below |
| Something the parts don't have (ears, horns, a scarf, a badge) | a pixel map pinned to a layer (`maps`) |
| A part that moves by itself (a tail that wags, a blinking light) | a map with 2 to 4 `frames` and an `fps` |
| A signature prop held in the hand | `propMap` |
| A personal habit between bits | `quirk`: keyframes of pose words |
| What they like doing most | `fav`: weights on existing actions |

Anything else (a new kind of motion, a new action) is a code change in `researchers.js`, not a spec.

## The rules

1. **Start from a built-in sprite.** `base` is one of `aris aisha ben mei sam evelyn david javier hanna kenji
   quill pip`. Everything the spec doesn't say comes from it, its quirk included unless the spec has its own.
2. **One size.** Height and build are set by the engine (and vary a little per visit); a spec can't change them.
   Maps must stay inside their layer's box (below), so every sprite fits the body the animations were made for.
3. **Faces are drawn by code.** Eyes, brows and mouth change with every pose (happy `^ ^`, wide, half-lidded,
   a smirk). A map may not put a pixel on the face box (head layer, x -1 to 6, y 1 to 8). Use `face`, `facial`,
   `brow`, `mouth`, `lash`, `lids`, `freckles` for the face.
4. **Facing right, y up.** Every map is drawn facing right; facing left mirrors it. In a map, rows run top to
   bottom as written, but `at` and the boxes count y **up** from the anchor.
5. **Data only.** Only the fields below; nothing in a spec runs. Unknown fields fail the check.
6. **Content.** Sprites are cheerful researchers for a work tool. Refuse nudity or sexual content, gore,
   hateful symbols, real people's likenesses and brand logos; draw everyone clothed.
7. **Keep it small and readable.** About 1 CSS pixel per sprite pixel: two to four colors per added part, no
   single stray pixels (every colored pixel gets a dark outline, so a lone pixel becomes a 3x3 blob; draw thin
   lines with `k`, which is never outlined again).
8. **Check, then fix.** `validateSprite(spec)` returns `{ ok, errors }`; each error says what to change.

## The spec

```json
{
  "base": "mei",
  "name": "Dr. Nova",
  "worker": "assistant",
  "hair": ["#f2f0f7", "#c9b8e8"],
  "maps": [{ "layer": "head", "at": [-4, 14], "rows": [".h.....h.", "hrh...hrh", "hhh...hhh"], "pal": { "r": "#ffb3c7" } }],
  "quirk": { "dur": 2.4, "keys": [{ "t": 0, "armF": ["up", 0], "eyes": "wide" }, { "t": 0.8, "hop": 3, "icon": "heart" }] }
}
```

| Field | What | Values |
|---|---|---|
| `base` | required: the starting sprite | a built-in key |
| `name` | required: shown when they speak as themselves | 1 to 24 letters, digits, spaces, `. ' -` |
| `worker` | whose crew they join | `assistant` (young), `pi` (tenured) |
| `style` | hair | `short tousled receding bob bigcurly long crop neat spiky` |
| `head` | headwear (`null` for none) | `hijab cap beanie hardhat deerstalker propeller` |
| `outfit` | clothes | `labcoat suit hoodie sweater cardigan vest overalls jacket abaya trench tshirt` |
| `legs` | | `pants skirt` |
| `face` | glasses (`null` for none) | `round browline halfmoon` |
| `facial` | (`null`, or a list: one per visit) | `walrus mustache` |
| `brow`, `mouth` | resting expression | brow: `flat raise stern worry skeptic`; mouth: `none smile grin o flat smirk` |
| flags | `true`/`false` | `lash lids bushy ahoge grin freckles pencil rolled` |
| colors | `#rrggbb`, or a list of up to 8 (one per visit) | `hair top shirt pants shoes pack tie bow hijab cap beanie pom skirt frame` |
| `skin` | tones to pick from | list of 0 (lightest) to 7 (darkest) |
| `prop` | built-in signature prop (`null`, or a list) | `tablet laptop phone book briefcase camera magnifier mug` |
| `fav` | how much they like an action | action name to weight 0 to 5, e.g. `{ "dig": 3 }` |
| `maps` | up to 8 pinned pixel maps | see Maps |
| `propMap` | their own prop instead of `prop` | see Props |
| `quirk` | their own habit | see Quirks |

Flags: `lash` a lash flick on each eye, `lids` half-lidded eyes, `bushy` thick brows, `ahoge` one strand of hair
standing up, `grin` an open smile with a fang, `freckles`, `pencil` behind the ear, `rolled` rolled-up sleeves.

## Maps

A map is a picture in letters, pinned to a layer:

```json
{ "layer": "back", "at": [-10, 1], "fps": 3, "frames": [["hh..", ".hh.", "..hh"], ["..hh", ".hh.", "hh.."]], "pal": {} }
```

- `layer`, and what it hangs from:

  | Layer | Drawn | Anchor (0, 0) | Box `[left, bottom, right, top]` |
  |---|---|---|---|
  | `back` | behind the body (capes, tails, wings) | the middle of the hips | `[-12, -9, 10, 16]` |
  | `body` | over the clothes (badges, scarves, belts) | the middle of the hips | `[-12, -9, 10, 16]` |
  | `behindHead` | behind the face (long hair, a hood) | the head's bottom row, middle | `[-9, -3, 10, 22]` |
  | `head` | on top of hair and hat (ears, horns, bows) | the head's bottom row, middle | `[-9, -3, 10, 22]`, not on the face box |

- `at`: `[x, y]` of the map's bottom-left pixel from the anchor. x grows toward the face, y grows up.
- `rows` (one picture) or `frames` (2 to 4 pictures, cycled at `fps`, 1 to 12): up to 24 rows of up to 24
  characters, all rows the same width. `.` is empty.
- Letters: anything in `pal` (`{"r": "#ffb3c7"}`, one letter or digit each), or a color slot that follows this
  visit's colors: `s` skin, `S` skin shade, `h` hair, `H` hair shade, `l` hair light, `t` top, `T` top shade,
  `c` shirt, `p` pants, `P` pants shade, `o` shoes, `k` outline ink, `w` white. `pal` can't redefine a slot.

Head geometry, for placing things: the head is 14 x 13 with its outline, x -6 to 7, y 0 to 12 from its anchor:
big for the body, so the face has room. The skin is x -5 to 6, y 1 to 11. Eyes are 2 pixels wide at x 0 and x 4,
rows y 4 to 6, with brows at y 8 or 9; the mouth is around x 2 to 5, y 1 to 3; the ear is at x -4 to -3, y 4 to 6.
Hair reaches y 14 or 15; most hats reach y 14 to 18.
The torso is about 12 wide (x -6 to 5) and 11 to 13 tall above the hips; the front of the body is +x.

## Props

```json
{ "arm": "forward", "at": [-1, 1], "rows": ["..k..", ".kwk.", ".kgk.", "kgwgk", "kgggk", ".kkk."], "pal": { "g": "#69db7c" } }
```

Held in the front hand whenever a bit lets them carry something (walking, standing about). `at` is from the
middle of the hand, box `[-6, -10, 12, 12]`; `arm` is how the arm holds it (`forward high low ...`, default
`forward`). The fingers are drawn over the 3 x 3 pixels around the hand's middle, so put what should show outside
that (above it, `at` y 1, for something held upright). Bits with their own props (a book, a shovel) still use
theirs. `rows`/`frames`/`fps`/`pal` work as in maps.

## Quirks

A quirk is a habit done now and then between bits, a couple of seconds long, made of keyframes. Each key holds its
pose until the next key; anything a key doesn't say is the usual standing pose (still carrying the prop unless
the key moves the front arm).

```json
{ "dur": 2.4, "say": ["Nya-hypothesis!"], "sayAt": 0.9,
  "keys": [ { "t": 0, "armF": ["up", 0], "eyes": "wide", "mouth": "o" },
            { "t": 0.8, "hop": 3, "icon": "heart", "eyes": "happy", "mouth": "smile", "blush": true },
            { "t": 1.2, "hop": 0 } ] }
```

- `dur` 0.5 to 6 seconds; up to 12 `keys` in time order, each with `t` (seconds from the start).
- Pose words in a key: `armF` / `armB` `[mode, nudge]` with mode `swing forward high low up head mouth face` and
  nudge -4 to 4; `eyes` `open closed happy down wide up`; `mouth`; `brow`; `blush`, `glint` (glasses flash),
  `nod`, `liftF` / `liftB` (lift a foot): `true`/`false`; `crouch` 0 to 3; `hop` 0 to 10 pixels off the floor.
- Once, as the key is reached: `icon` (`bulb q bang heart note check`), `sparkle: true`.
- `say`: up to 5 lines of up to 40 characters; one may be said (half the time) at `sayAt` seconds.

## Using sprites from a page

```js
window.RESEARCHER_SPRITES = { add: { "custom-sprite-1": spec }, off: ["pip"] };   // before researchers.js loads
```

- `add`: sprites to register, by key (lowercase letters, digits, dashes, up to 32; built-in names are taken).
  A spec that fails the check is skipped with a console warning.
- `off`: sprites that never visit (built-in or custom). A crew is never left empty: if every member of one worker's
  crew is off, the setting is ignored for that crew.
- `Researchers.validateSprite(spec)` → `{ ok, errors, sprite }`; `registerSprite(key, spec)`;
  `setSpriteEnabled(key, on)`; `renderSprite(canvas, keyOrSpec, { scale, action, t, seed, face })` draws one still,
  feet at the bottom middle, for previews and pickers (default 44 x 60 sprite pixels).
- Nobody is on a page twice at once, custom sprites included.

## For a model drawing a sprite

1. Pick the built-in closest to the request as `base`; the closer it is, the less you draw.
2. Change parts and colors first; draw maps only for what the parts can't do.
3. Sketch each map on paper-sized grids in your head against the head geometry above; keep off the face box.
4. Give them an attitude in a line or two: a resting `brow`/`mouth`, a flag or two, a quirk of 2 to 4 keys.
5. Run `validateSprite`, fix every error it lists, and look at `renderSprite` in a few actions (`rest`, `walk`,
   `read`, `sleep`, `quirk`) before handing it over.
6. If the request asks for nudity, sexual content, gore, hate symbols, a real person or a logo, say no and offer
   a version that fits rule 6.
