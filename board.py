"""The cork board (code, no model): every mission and epic as a sticky note in 2D, joined by string.

A note gets a position the first time the board is read and keeps it: a new epic takes the first free spot on
rings around the board's centre, a new mission the first free spot spiralling out from its epic's note (missions
in no epic gather round an Unsorted label, a combined mission's members round the mission that took them over).
The same desk always gives the same placement. Positions live in the `board` table; a human drag or the
Theorist's board turn (#69) moves them later.

Strings are never stored: they are read from what the desk already holds each time. Epic to each of its
missions, the links table, and mentions, which code finds when a brief or article is saved (find_mentions) and
when research is shown related published articles (record_background), kept in their own table."""
import math, re
from datetime import datetime, timezone
from urllib.parse import urlsplit
import store, wayback
from contracts import Article, BoardNote, Mention, Mission, ResearchBrief, State

# ---------- tunable numbers ----------
SLOT = 215                                 # px between a mission and the next ring of its cluster
EPIC_RING = 640                            # px between rings of epics around the board's centre
WIDE = 1.5                                 # epic rings are this much wider than tall, like a screen
MIN_GAP = 200                              # a new note keeps at least this far from every other note
EPIC_GAP = 560                             # a new epic keeps at least this far from every other epic
TALL = 1.45                                # notes are wider than tall: vertical gaps count this much more
SIZE_MIN, SIZE_MAX = 0.7, 1.6              # a note's size, 1.0 = normal; also the clamp for human and Theorist sizes
EPIC_SIZE = 1.3
STAGE_SIZE = {                             # further along is bigger
    State.QUEUED: 0.8, State.RESEARCHING: 0.9, State.BRIEF_READY: 1.0, State.AWAITING_COMMENTS: 1.05,
    State.WRITING: 1.05, State.ARTICLE_READY: 1.15, State.DONE: 1.25, State.PARKED: 0.85, State.COMBINED: 0.75}
PER_STRING, MAX_STRING_BONUS = 0.04, 0.2   # a little bigger for every string, up to this much
UNSORTED = "_unsorted"                     # the label missions in no epic gather round
MIN_NAME = 3                               # words a title or slug needs before a mention of it counts
POLL_SECONDS = 4                           # how often an open board asks for changes
PREVIEW_CHARS = 420                        # the preview's thesis is cut to this

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# ---------- placement ----------

def _rings(cx: float, cy: float, step: float, wide: float = 1.0, start: float = -math.pi / 2):
    """Spots on rings around (cx, cy), nearest first, each ring starting at `start` (straight up unless told):
    the same order every time."""
    for r in range(1, 60):
        n = 6 * r
        for i in range(n):
            a = start + 2 * math.pi * i / n
            yield round(cx + r * step * wide * math.cos(a), 1), round(cy + r * step * math.sin(a), 1)

def _far(x: float, y: float, others, gap: float) -> bool:
    return all(math.hypot(x - ox, (y - oy) * TALL) >= gap for ox, oy in others)

def _spot(cx: float, cy: float, step: float, taken: list, gap: float, epics: list | None = None) -> tuple:
    spots = _rings(cx, cy, step, WIDE, 0.0) if epics is not None else _rings(cx, cy, step)   # epics spread sideways
    for x, y in ([(cx, cy)] if epics is not None else []) + list(spots):
        if _far(x, y, taken, gap) and (epics is None or _far(x, y, epics, EPIC_GAP)):
            return x, y
    return cx, cy

def place(missions: list[Mission] | None = None) -> dict[tuple[str, str], BoardNote]:
    """Every note's position, placing (and saving) any note that has none yet."""
    missions = store.all_missions() if missions is None else missions
    epics = store.all_epics()
    notes = store.board_notes()
    epic_slugs = {e.slug for e in epics}
    taken = [(n.x, n.y) for n in notes.values()]
    epic_spots = [(n.x, n.y) for (k, i), n in notes.items() if k == "epic"]

    def put(kind: str, id_: str, xy: tuple) -> BoardNote:
        n = store.save_board_note(BoardNote(kind=kind, id=id_, x=xy[0], y=xy[1], by="code", at=_now()))
        notes[(kind, id_)] = n
        taken.append(xy)
        if kind == "epic":
            epic_spots.append(xy)
        return n

    def epic_note(slug: str) -> BoardNote:
        if ("epic", slug) not in notes:
            put("epic", slug, _spot(0, 0, EPIC_RING, taken, MIN_GAP, epic_spots))
        return notes[("epic", slug)]

    for e in epics:
        epic_note(e.slug)
    loose = [m for m in missions if m.state != State.COMBINED]
    if any(m.epic not in epic_slugs for m in loose):
        epic_note(UNSORTED)                # the label shows whenever a mission is in no epic, placed or not
    for m in loose + [m for m in missions if m.state == State.COMBINED]:
        if ("mission", m.slug) in notes:
            continue
        took = notes.get(("mission", m.combined_into)) if m.combined_into else None
        anchor = took or epic_note(m.epic if m.epic in epic_slugs else UNSORTED)
        put("mission", m.slug, _spot(anchor.x, anchor.y, SLOT, taken, MIN_GAP))
    return notes

def move(kind: str, id_: str, x: float, y: float, size: float | None = None, by: str = "human") -> BoardNote:
    """Put a note somewhere (a human's drag, or the Theorist); a size is clamped to the board's range."""
    if kind not in ("mission", "epic"):
        raise ValueError(f"unknown note kind {kind!r}")
    if kind == "mission" and not store.get_mission(id_):
        raise ValueError(f"no mission {id_!r}")
    if kind == "epic" and id_ != UNSORTED and not store.get_epic(id_):
        raise ValueError(f"no epic {id_!r}")
    if not all(map(math.isfinite, (x, y))):
        raise ValueError("a position must be a number")
    old = store.board_notes().get((kind, id_))
    size = old.size if size is None and old else size
    if size is not None:
        size = round(min(SIZE_MAX, max(SIZE_MIN, float(size))), 2)
    return store.save_board_note(BoardNote(kind=kind, id=id_, x=round(x, 1), y=round(y, 1), size=size, by=by,
                                           at=_now()))

# ---------- strings ----------

def strings(missions: list[Mission] | None = None) -> list[dict]:
    """Every string on the board: epic to mission, links, and mentions (one string per pair, every kind on it)."""
    missions = store.all_missions() if missions is None else missions
    here = {m.slug for m in missions}
    epics = {e.slug for e in store.all_epics()}
    out = [{"a": f"e:{m.epic}", "b": f"m:{m.slug}", "kind": "epic"}
           for m in missions if m.epic in epics and m.state != State.COMBINED]
    linked = set()
    for k in store.all_links():
        if k.a in here and k.b in here:
            out.append({"a": f"m:{k.a}", "b": f"m:{k.b}", "kind": "link", "why": k.why, "by": k.by})
            linked.add((k.a, k.b))
    pairs: dict[tuple[str, str], list[Mention]] = {}
    for k in store.all_mentions():
        if k.a in here and k.b in here and (k.a, k.b) not in linked:
            pairs.setdefault((k.a, k.b), []).append(k)
    for (a, b), found in pairs.items():
        out.append({"a": f"m:{a}", "b": f"m:{b}", "kind": "mention", "why": "; ".join(_say(k) for k in found),
                    "sub": sorted({k.kind for k in found})})
    return out

def _say(k: Mention) -> str:
    return {"source": f"both cite {k.detail}", "background": f"{k.detail}'s research was shown the other as background",
            "name": f"names {k.detail}"}[k.kind]

def size_of(m: Mission, note: BoardNote | None, string_count: int) -> float:
    """A set size wins; otherwise the stage's size and a little per string."""
    if note and note.size is not None:
        return note.size
    base = STAGE_SIZE.get(m.state, 1.0) + min(MAX_STRING_BONUS, PER_STRING * string_count)
    return round(min(SIZE_MAX, max(SIZE_MIN, base)), 2)

def layout() -> dict:
    """The board as data: each note with its position and size, and the strings. ui.py adds the words."""
    missions = store.all_missions()
    notes = place(missions)
    lines = strings(missions)
    count: dict[str, int] = {}
    for s in lines:
        for end in (s["a"], s["b"]):
            count[end] = count.get(end, 0) + 1
    out = []
    for e in store.all_epics():
        n = notes[("epic", e.slug)]
        out.append({"id": f"e:{e.slug}", "kind": "epic", "slug": e.slug, "x": n.x, "y": n.y,
                    "size": n.size or EPIC_SIZE, "by": n.by})
    if ("epic", UNSORTED) in notes and any(m.state != State.COMBINED and m.epic not in {e["slug"] for e in out}
                                           for m in missions):
        n = notes[("epic", UNSORTED)]
        out.append({"id": f"e:{UNSORTED}", "kind": "unsorted", "slug": UNSORTED, "x": n.x, "y": n.y,
                    "size": 1.0, "by": n.by})
    for m in missions:
        n = notes[("mission", m.slug)]
        out.append({"id": f"m:{m.slug}", "kind": "mission", "slug": m.slug, "x": n.x, "y": n.y,
                    "size": size_of(m, n, count.get(f"m:{m.slug}", 0)), "by": n.by})
    return {"notes": out, "strings": lines}

# ---------- mentions ----------

def page_key(url: str) -> str:
    """One spelling per page: an archived copy counts as its original, without scheme, www, query or a last /."""
    url = wayback.original(url) or url
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    return f"{host}{parts.path.rstrip('/')}" if host else url.strip().rstrip("/")

def _brief(slug: str) -> ResearchBrief | None:
    data = store.load_artifact(slug, "brief")
    return ResearchBrief(**data) if data else None

def _article(slug: str) -> Article | None:
    data = store.load_artifact(slug, "article")
    return Article(**data) if data else None

def cited_pages(m: Mission) -> dict[str, str]:
    """The pages a mission's brief cites (or its checked sources while the brief is being written): key -> url."""
    b = _brief(m.slug)
    sources = b.sources if b else (m.brief_sources.sources if m.brief_sources else [])
    return {page_key(s.url): s.url for s in sources}

def own_text(m: Mission) -> str:
    """What the mission wrote, lowercased: its brief and article."""
    b, a = _brief(m.slug), _article(m.slug)
    parts = []
    if b:
        parts += [b.thesis, b.counterpoint, *(c.text for c in b.claims)]
    if a:
        parts += [a.title, a.dek, *(f"{s.heading} {' '.join(s.paragraphs)}" for s in a.sections)]
    return " ".join(" ".join(parts).lower().split())

def names(m: Mission) -> list[str]:
    """The phrases that count as naming a mission: its topic, its article's title and its slug, each MIN_NAME+ words."""
    a = _article(m.slug)
    found = [m.topic, a.title if a else "", m.slug.replace("-", " ")]
    phrases = [" ".join(p.lower().split()).strip(" .?!") for p in found]
    return [p for p in dict.fromkeys(phrases) if len(p.split()) >= MIN_NAME]

def _names_in(phrases: list[str], text: str) -> str:
    for p in phrases:
        if re.search(rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])", text):
            return p
    return ""

def find_mentions(slug: str) -> int:
    """Find this mission's same-source and name mentions again (after its brief or article was saved)."""
    m = store.get_mission(slug)
    store.delete_mentions(slug, ("source", "name"))
    if m is None:
        return 0
    mine_pages, mine_text, mine_names = cited_pages(m), own_text(m), names(m)
    made = 0
    for other in store.all_missions():
        if other.slug == slug:
            continue
        shared = sorted(set(mine_pages) & set(cited_pages(other)))
        if shared:
            more = f" and {len(shared) - 1} more" if len(shared) > 1 else ""
            store.save_mention(Mention(a=slug, b=other.slug, kind="source", detail=mine_pages[shared[0]] + more))
            made += 1
        said = _names_in(names(other), mine_text) or _names_in(mine_names, own_text(other))
        if said:
            store.save_mention(Mention(a=slug, b=other.slug, kind="name", detail=f'"{said}"'))
            made += 1
    return made

def find_all_mentions() -> int:
    """Every mission's same-source and name mentions, found again (an older desk the first time a board opens)."""
    return sum(find_mentions(m.slug) for m in store.all_missions())

def record_background(slug: str, shown: list[str]) -> None:
    """Research on this mission was shown these published missions as background (related.similar_published)."""
    for k in store.all_mentions():
        if k.kind == "background" and k.detail == slug:
            store.delete_mention(k.a, k.b, k.kind)
    for other in shown:
        if other != slug:
            store.save_mention(Mention(a=slug, b=other, kind="background", detail=slug))
