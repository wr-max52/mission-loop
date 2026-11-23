"""The time, and how the UI and `run.py status` put a span of time into words (a publishing backlog, #119)."""
import os
from datetime import datetime, timedelta, timezone
from i18n import _, _n

def now() -> datetime:
    offset = float(os.environ.get("MISSION_LOOP_CLOCK_OFFSET_HOURS", "0"))
    return datetime.now(timezone.utc) + timedelta(hours=offset)

def _half_up(x: float) -> int:
    return int(x + 0.5)                    # round() rounds 2.5 to 2

def about(span: timedelta) -> str:
    """A rough length of time, in the largest unit that reads well: "about 40 minutes", "about 5 hours",
    "about 9 days", "about 3 weeks". Days run up to two weeks; under half a minute is "under a minute"."""
    seconds = max(0.0, span.total_seconds())
    if seconds < 30:
        return _("under a minute")
    if (minutes := _half_up(seconds / 60)) < 60:
        return _n("about {n} minute", "about {n} minutes", minutes)
    if (hours := _half_up(seconds / 3600)) < 24:
        return _n("about {n} hour", "about {n} hours", hours)
    if (days := _half_up(seconds / 86400)) < 14:
        return _n("about {n} day", "about {n} days", days)
    return _n("about {n} week", "about {n} weeks", _half_up(seconds / 604800))

def every(pace: timedelta) -> str:
    """How often one goes out: "one a day", "one every 10 minutes", "one every 2 hours"."""
    seconds = round(pace.total_seconds())
    for size, one, many in ((604800, "one a week", "one every {n} weeks"), (86400, "one a day", "one every {n} days"),
                            (3600, "one an hour", "one every {n} hours")):
        if seconds >= size and seconds % size == 0:
            return _n(one, many, seconds // size)
    return _n("one a minute", "one every {n} minutes", max(1, _half_up(seconds / 60)))

def backlog(span: timedelta | None, pace: timedelta | None) -> str:
    """A capped queue's estimate, "about 3 weeks (one a day)", or "" without one (a place with no cap)."""
    if span is None or not pace:
        return ""
    return _("{time} ({pace})").format(time=about(span), pace=every(pace))
