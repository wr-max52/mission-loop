"""macOS notifications when a mission needs a human (or just got published). Best effort only."""
import subprocess
from contracts import Mission, State

MESSAGES = {
    State.BRIEF_READY: "Brief ready. Read it: python run.py show {slug}, then approve or reject",
    State.ARTICLE_READY: "Article ready. Open missions/{slug}/preview.html, then review or reject",
    State.PARKED: "Parked. See why: python run.py show {slug}, then requeue or push",
    State.DONE: "Published to site/{slug}/index.html",
}

def needs_attention(m: Mission) -> str | None:
    if m.state == State.BRIEF_READY and m.approved:
        return None
    if m.state == State.ARTICLE_READY and m.reviewed:
        return None
    msg = MESSAGES.get(m.state)
    return msg.format(slug=m.slug) if msg else None

def send(title: str, message: str) -> None:
    script = f"display notification {_q(message)} with title {_q(title)}"
    try:
        subprocess.run(["osascript", "-e", script], timeout=5, capture_output=True)
    except Exception:
        pass                               # not on macOS, or notifications blocked: the log still has it

def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

def announce(missions: list[Mission], seen: set[tuple[str, str]]) -> list[str]:
    """Notify once per (mission, state). Returns the lines it announced, for the log."""
    lines = []
    for m in missions:
        key = (m.slug, m.state.value)
        msg = needs_attention(m)
        if msg and key not in seen:
            send(f"Mission Loop: {m.slug}", msg)
            lines.append(f"{m.slug}: {msg}")
        seen.add(key)
    return lines
