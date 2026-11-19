import json, sqlite3
from datetime import datetime
from pathlib import Path
from contracts import Mission

DB = Path("data/loop.db")
INSPIRATION = Path("data/inspiration.txt") # the human's one line for the mission finder

def get_inspiration() -> str:
    return INSPIRATION.read_text().strip() if INSPIRATION.exists() else ""

def set_inspiration(text: str) -> str:
    """Save the line, collapsed to one line. Blank removes it, which turns the finder off."""
    line = " ".join(text.split())
    INSPIRATION.parent.mkdir(parents=True, exist_ok=True)
    if line:
        INSPIRATION.write_text(line + "\n")
    else:
        INSPIRATION.unlink(missing_ok=True)
    return line

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
    return [m for m in all_missions() if m.state.value not in ("DONE", "PARKED")]

def save_artifact(slug: str, kind: str, obj):
    if isinstance(obj, list):
        data = "[" + ",".join(o.model_dump_json() for o in obj) + "]"
    else:
        data = obj.model_dump_json()
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO artifacts VALUES (?, ?, ?)", (slug, kind, data))

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
