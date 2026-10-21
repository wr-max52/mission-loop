import os
from datetime import datetime, timedelta, timezone

def now() -> datetime:
    offset = float(os.environ.get("MISSION_LOOP_CLOCK_OFFSET_HOURS", "0"))
    return datetime.now(timezone.utc) + timedelta(hours=offset)
