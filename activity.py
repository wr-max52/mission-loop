"""What the loop is working on right now, so the UI can show it.

The loop and the UI are separate processes and the database only changes when a task ends, so each
agent's start is recorded in one small file. It is display-only: a failure here never stops a mission.
A killed loop leaves the file behind, so only trust it while the loop holds its lock.
"""
import json, os, time
from pathlib import Path

FILE = Path("data/activity.json")

AGENTS = {                                 # in pipeline order
    "sources": "Source finder",
    "contacts": "Contact finder",
    "brief": "Brief writer",
    "source_check": "Source checker",
    "writer": "Article writer",
    "article_check": "Article checker",
    "publisher": "Publisher",
}

def begin(slug: str, agent: str) -> None:
    tmp = FILE.with_suffix(".tmp")
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"slug": slug, "agent": agent, "since": time.time()}))
        os.replace(tmp, FILE)              # atomic, so the UI never reads half a file
    except OSError:
        pass

def clear() -> None:
    try:
        FILE.unlink(missing_ok=True)
    except OSError:
        pass

def current() -> dict | None:
    try:
        data = json.loads(FILE.read_text())
    except (OSError, ValueError):
        return None
    return data if data.get("agent") in AGENTS else None
