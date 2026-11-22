"""The stop button (data/STOP) and what work in progress does when it is pressed.

The model may finish the turn it is on (up to TURN_GRACE seconds after the press), then it writes out
its result straight away. Everything has to be out by GRACE seconds after the press: whatever has been
written by then is kept as a draft, unchecked, and the loop stops. The next pass checks that draft
first, so a bad one is caught by the validator like any other attempt.
"""
import json, time
from pathlib import Path
from pydantic import BaseModel, ValidationError

FLAG = Path("data/STOP")
TURN_GRACE = 30                            # seconds a tool turn in flight may still take after a press
GRACE = 120                                # seconds from the press until the write-out is cut off

class Interrupted(Exception):
    """A stop cut an agent short. draft is the best output it got out (None if nothing usable)."""
    def __init__(self, draft=None):
        super().__init__("stopped by the stop button")
        self.draft = draft

def requested() -> bool:
    return FLAG.exists()

def press(flag: Path | None = None) -> None:
    """Press stop. Pressing again doesn't move the deadline."""
    flag = flag or FLAG
    flag.parent.mkdir(parents=True, exist_ok=True)
    if not flag.exists():
        flag.touch()

def left(grace: float = GRACE) -> float | None:
    """Seconds until `grace` after the press runs out; None when stop isn't pressed."""
    try:
        pressed = FLAG.stat().st_mtime
    except FileNotFoundError:
        return None
    return pressed + grace - time.time()

def _cuts(text: str):
    """Ways to close a cut-off JSON text, longest first: the whole text, then the text up to each
    comma outside a string, each with whatever brackets are still open closed."""
    stack, in_str, esc, cuts = [], False, False, []
    for i, ch in enumerate(text):
        if in_str:
            if esc: esc = False
            elif ch == "\\": esc = True
            elif ch == '"': in_str = False
        elif ch == '"': in_str = True
        elif ch in "{[": stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack: stack.pop()
        elif ch == ",": cuts.append((i, "".join(reversed(stack))))
    tail = text + ('"' if in_str and not esc else "")
    yield tail + "".join(reversed(stack))
    for i, closers in reversed(cuts):
        yield text[:i] + closers

def _fill(data: dict, model: type[BaseModel]) -> dict:
    """Give missing required top-level fields an empty value so a cut-off draft still loads."""
    for name, field in model.model_fields.items():
        if name not in data and field.is_required():
            data[name] = [] if "list" in str(field.annotation) else ""
    return data

def salvage(text: str, model: type[BaseModel]):
    """The longest prefix of a cut-off JSON reply that makes a valid `model`, or None."""
    start = text.find("{")
    if start < 0:
        return None
    for candidate in _cuts(text[start:]):
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return model.model_validate(_fill(data, model))
        except (ValueError, ValidationError):
            continue
    return None
