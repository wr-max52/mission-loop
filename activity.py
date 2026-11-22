"""What the loop is working on right now, so the UI can show it.

The loop and the UI are separate processes and the database only changes when a task ends, so each
agent's start is recorded in one small file. It is display-only: a failure here never stops a mission.
A killed loop leaves the file behind, so only trust it while the loop holds its lock.
"""
import json, os, time
from pathlib import Path
import trail

FILE = Path("data/activity.json")

AGENTS = {                                 # in pipeline order
    "finder": "Mission finder",            # works for the whole desk, so its slug is ""
    "sources": "Source finder",
    "contacts": "Contact finder",
    "brief": "Brief writer",
    "source_check": "Source checker",
    "writer": "Article writer",
    "article_check": "Article checker",
    "publisher": "Publisher",
    "1f916": "1F916 poster",               # posts published articles, one a day, newest first
    "bluesky": "Bluesky poster",           # announces published articles, about one a pass, newest first
}

def begin(slug: str, agent: str) -> None:
    trail.start(slug, agent)               # the chat assistant reads what each agent did from here
    tmp = FILE.with_suffix(".tmp")
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"slug": slug, "agent": agent, "since": time.time()}))
        os.replace(tmp, FILE)              # atomic, so the UI never reads half a file
    except OSError:
        pass

def clear() -> None:
    trail.stop()
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
