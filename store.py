import json, sqlite3
from datetime import datetime, timezone
from pathlib import Path
from contracts import (Mission, Epic, Link, Combine, BoardNote, Mention, DeadLead, ClaimTag, OUT_OF_FLOW, epic_title,
                       slugify)

DB = Path("data/loop.db")
INSPIRATION = Path("data/inspiration.txt") # the human's one line for the mission finder, which the Theorist evolves
INSPIRATION_LOG = Path("data/inspiration_log.jsonl")   # every change to the line: when, who, the line, why
THEORY = Path("data/theory.json")          # the Theorist's last turn: what it saw and what it changed

def get_inspiration() -> str:
    return INSPIRATION.read_text().strip() if INSPIRATION.exists() else ""

def set_inspiration(text: str, by: str = "human", why: str = "") -> str:
    """Save the line, collapsed to one line. Blank removes it, which turns the finder and the Theorist off.
    by is "human" or "theorist"; each change is logged, so the Theorist can see how the line got here."""
    line = " ".join(text.split())
    before = get_inspiration()
    INSPIRATION.parent.mkdir(parents=True, exist_ok=True)
    if line:
        INSPIRATION.write_text(line + "\n")
    else:
        INSPIRATION.unlink(missing_ok=True)
    if line != before:
        with INSPIRATION_LOG.open("a") as f:
            f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "by": by, "line": line,
                                "why": why}) + "\n")
    return line

def inspiration_history(n: int = 10) -> list[dict]:
    """The last n changes to the line, oldest first: {"at", "by", "line", "why"}."""
    try:
        lines = INSPIRATION_LOG.read_text().splitlines()
    except OSError:
        return []
    found = []
    for line in lines[-n:]:
        try:
            found.append(json.loads(line))
        except ValueError:
            continue
    return found

def last_theory() -> dict:
    """The Theorist's last turn, or {} before its first."""
    try:
        data = json.loads(THEORY.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}

def last_ideas() -> dict:
    """The Theorist's last turn that looked across the desk (not a board turn), or {}."""
    last = last_theory()
    return (last.get("ideas") or {}) if last.get("kind") == "board" else last

def save_theory(data: dict) -> None:
    THEORY.parent.mkdir(parents=True, exist_ok=True)
    THEORY.write_text(json.dumps(data))

def conn():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS missions(
            slug TEXT PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS artifacts(
            slug TEXT, kind TEXT, data TEXT, PRIMARY KEY (slug, kind));
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY, slug TEXT, at TEXT,
            from_state TEXT, to_state TEXT, note TEXT);
        CREATE TABLE IF NOT EXISTS epics(
            slug TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active');
        CREATE TABLE IF NOT EXISTS links(
            a TEXT, b TEXT, data TEXT NOT NULL, PRIMARY KEY (a, b));
        CREATE TABLE IF NOT EXISTS combines(
            slug TEXT PRIMARY KEY, status TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS board(
            kind TEXT, id TEXT, data TEXT NOT NULL, PRIMARY KEY (kind, id));
        CREATE TABLE IF NOT EXISTS mentions(
            a TEXT, b TEXT, kind TEXT, data TEXT NOT NULL, PRIMARY KEY (a, b, kind));
        CREATE TABLE IF NOT EXISTS dead_leads(
            slug TEXT PRIMARY KEY, status TEXT NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS claim_tags(
            slug TEXT PRIMARY KEY, data TEXT NOT NULL);
        """)

def save_mission(m: Mission):
    with conn() as c:
        c.execute(
            "INSERT INTO missions VALUES (?, ?, ?) "
            "ON CONFLICT(slug) DO UPDATE SET state = excluded.state, data = excluded.data",
            (m.slug, m.state.value, m.model_dump_json()))

def get_mission(slug: str) -> Mission | None:
    row = conn().execute("SELECT data FROM missions WHERE slug = ?", (slug,)).fetchone()
    return Mission.model_validate_json(row["data"]) if row else None

def all_missions() -> list[Mission]:
    rows = conn().execute("SELECT data FROM missions ORDER BY rowid").fetchall()
    return [Mission.model_validate_json(r["data"]) for r in rows]

def active_missions() -> list[Mission]:
    return [m for m in all_missions() if m.state not in OUT_OF_FLOW]

def save_artifact(slug: str, kind: str, obj):
    if isinstance(obj, list):
        data = "[" + ",".join(o.model_dump_json() for o in obj) + "]"
    else:
        data = obj.model_dump_json()
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO artifacts VALUES (?, ?, ?)", (slug, kind, data))

def delete_artifact(slug: str, kind: str):
    with conn() as c:
        c.execute("DELETE FROM artifacts WHERE slug = ? AND kind = ?", (slug, kind))

def load_artifact(slug: str, kind: str):
    row = conn().execute(
        "SELECT data FROM artifacts WHERE slug = ? AND kind = ?", (slug, kind)).fetchone()
    return json.loads(row["data"]) if row else None

def log_event(slug: str, at: datetime, frm: str, to: str, note: str = ""):
    with conn() as c:
        c.execute("INSERT INTO events (slug, at, from_state, to_state, note) VALUES (?, ?, ?, ?, ?)",
                  (slug, at.isoformat(), frm, to, note))

def events(slug: str) -> list[sqlite3.Row]:
    return conn().execute("SELECT * FROM events WHERE slug = ? ORDER BY id", (slug,)).fetchall()

# ---------- epics ----------

def all_epics(status: str | None = None) -> list[Epic]:
    rows = conn().execute("SELECT * FROM epics ORDER BY rowid").fetchall()
    return [Epic(**dict(r)) for r in rows if status in (None, r["status"])]

def get_epic(slug: str) -> Epic | None:
    row = conn().execute("SELECT * FROM epics WHERE slug = ?", (slug,)).fetchone()
    return Epic(**dict(row)) if row else None

def find_epic(name: str) -> Epic | None:
    """An epic by slug or by title, ignoring case and spacing (tech_influence finds "Tech influence")."""
    names = {n for n in (" ".join(name.split()).lower(), epic_title(name).lower()) if n}
    return next((e for e in all_epics() if {e.slug, e.title.lower()} & names), None) if names else None

def save_epic(e: Epic):
    with conn() as c:
        c.execute("INSERT INTO epics VALUES (?, ?, ?) ON CONFLICT(slug) DO UPDATE SET "
                  "title = excluded.title, status = excluded.status", (e.slug, e.title, e.status))

def create_epic(title: str, status: str = "active") -> Epic:
    """The epic with this title, made if it doesn't exist yet. Raises ValueError for an unusable title."""
    clean = epic_title(title)
    if not clean:
        raise ValueError(f"an epic title needs 1 to 4 words: {title!r}")
    found = find_epic(clean)
    if found:
        return found
    base = slugify(clean) or "epic"
    slug, n = base, 2
    while get_epic(slug):
        slug, n = f"{base}-{n}", n + 1
    epic = Epic(slug=slug, title=clean, status=status)
    save_epic(epic)
    return epic

def delete_epic(slug: str):
    with conn() as c:
        c.execute("DELETE FROM epics WHERE slug = ?", (slug,))

def epic_titles() -> dict[str, str]:
    """slug -> title for every epic, for labels."""
    return {e.slug: e.title for e in all_epics()}

# ---------- links and combines: the Theorist connecting dots ----------
# Links and combine proposals live in their own tables, never on the mission, so a worker saving the mission it
# is busy with can't save over one.

def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)

def get_link(a: str, b: str) -> Link | None:
    a, b = _pair(a, b)
    row = conn().execute("SELECT data FROM links WHERE a = ? AND b = ?", (a, b)).fetchone()
    return Link.model_validate_json(row["data"]) if row else None

def save_link(link: Link) -> Link:
    link.a, link.b = _pair(link.a, link.b)
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO links VALUES (?, ?, ?)", (link.a, link.b, link.model_dump_json()))
    return link

def delete_link(a: str, b: str) -> None:
    a, b = _pair(a, b)
    with conn() as c:
        c.execute("DELETE FROM links WHERE a = ? AND b = ?", (a, b))

def all_links(status: str | None = "active") -> list[Link]:
    rows = conn().execute("SELECT data FROM links ORDER BY rowid").fetchall()
    found = [Link.model_validate_json(r["data"]) for r in rows]
    return [k for k in found if status in (None, k.status)]

def links_of(slug: str, status: str | None = "active") -> list[Link]:
    return [k for k in all_links(status) if slug in (k.a, k.b)]

def get_combine(slug: str) -> Combine | None:
    row = conn().execute("SELECT data FROM combines WHERE slug = ?", (slug,)).fetchone()
    return Combine.model_validate_json(row["data"]) if row else None

def save_combine(c_: Combine) -> None:
    with conn() as c:
        c.execute("INSERT INTO combines VALUES (?, ?, ?) ON CONFLICT(slug) DO UPDATE SET "
                  "status = excluded.status, data = excluded.data", (c_.slug, c_.status, c_.model_dump_json()))

def delete_combine(slug: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM combines WHERE slug = ?", (slug,))

def all_combines(status: str | None = None) -> list[Combine]:
    rows = conn().execute("SELECT data FROM combines ORDER BY rowid").fetchall()
    found = [Combine.model_validate_json(r["data"]) for r in rows]
    return [c for c in found if status in (None, c.status)]

# ---------- dead leads and claim tags: one row per mission, never on the mission ----------

def get_dead_lead(slug: str) -> DeadLead | None:
    row = conn().execute("SELECT data FROM dead_leads WHERE slug = ?", (slug,)).fetchone()
    return DeadLead.model_validate_json(row["data"]) if row else None

def save_dead_lead(d: DeadLead) -> DeadLead:
    with conn() as c:
        c.execute("INSERT INTO dead_leads VALUES (?, ?, ?) ON CONFLICT(slug) DO UPDATE SET "
                  "status = excluded.status, data = excluded.data", (d.slug, d.status, d.model_dump_json()))
    return d

def all_dead_leads(status: str | None = None) -> list[DeadLead]:
    rows = conn().execute("SELECT data FROM dead_leads ORDER BY rowid").fetchall()
    found = [DeadLead.model_validate_json(r["data"]) for r in rows]
    return [d for d in found if status in (None, d.status)]

def get_claim_tag(slug: str) -> ClaimTag | None:
    row = conn().execute("SELECT data FROM claim_tags WHERE slug = ?", (slug,)).fetchone()
    return ClaimTag.model_validate_json(row["data"]) if row else None

def save_claim_tag(t: ClaimTag) -> ClaimTag:
    with conn() as c:
        c.execute("INSERT INTO claim_tags VALUES (?, ?) ON CONFLICT(slug) DO UPDATE SET data = excluded.data",
                  (t.slug, t.model_dump_json()))
    return t

def all_claim_tags() -> dict[str, ClaimTag]:
    rows = conn().execute("SELECT data FROM claim_tags").fetchall()
    return {t.slug: t for t in (ClaimTag.model_validate_json(r["data"]) for r in rows)}

# ---------- the cork board: note positions and the mentions code found ----------

def board_notes() -> dict[tuple[str, str], BoardNote]:
    rows = conn().execute("SELECT data FROM board ORDER BY rowid").fetchall()
    notes = [BoardNote.model_validate_json(r["data"]) for r in rows]
    return {(n.kind, n.id): n for n in notes}

def save_board_note(n: BoardNote) -> BoardNote:
    with conn() as c:
        c.execute("INSERT INTO board VALUES (?, ?, ?) ON CONFLICT(kind, id) DO UPDATE SET data = excluded.data",
                  (n.kind, n.id, n.model_dump_json()))
    return n

def delete_board_note(kind: str, id_: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM board WHERE kind = ? AND id = ?", (kind, id_))

def save_mention(k: Mention) -> Mention:
    k.a, k.b = _pair(k.a, k.b)
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO mentions VALUES (?, ?, ?, ?)", (k.a, k.b, k.kind, k.model_dump_json()))
    return k

def delete_mentions(slug: str, kinds: tuple[str, ...]) -> None:
    """Forget the mentions of these kinds that involve this mission (before code finds them again)."""
    with conn() as c:
        c.execute(f"DELETE FROM mentions WHERE (a = ? OR b = ?) AND kind IN ({','.join('?' * len(kinds))})",
                  (slug, slug, *kinds))

def replace_mentions(kinds: tuple[str, ...], found: list[Mention]) -> None:
    """Forget every mention of these kinds and save the ones found, in one transaction (the board's backfill)."""
    rows = []
    for k in found:
        k.a, k.b = _pair(k.a, k.b)
        rows.append((k.a, k.b, k.kind, k.model_dump_json()))
    with conn() as c:
        c.execute(f"DELETE FROM mentions WHERE kind IN ({','.join('?' * len(kinds))})", kinds)
        c.executemany("INSERT OR REPLACE INTO mentions VALUES (?, ?, ?, ?)", rows)

def delete_mention(a: str, b: str, kind: str) -> None:
    a, b = _pair(a, b)
    with conn() as c:
        c.execute("DELETE FROM mentions WHERE a = ? AND b = ? AND kind = ?", (a, b, kind))

def all_mentions() -> list[Mention]:
    rows = conn().execute("SELECT data FROM mentions ORDER BY rowid").fetchall()
    return [Mention.model_validate_json(r["data"]) for r in rows]
