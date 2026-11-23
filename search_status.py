"""Whether web search works, in code (no model). A search tool's error is sorted into a kind (out of credit, key
refused, rate limited, unreachable) with one plain sentence; "no results" is not an outage. The latest outage is
kept in data/search_status.json until a search works again, so the setup check, the top bar, the finder and the
step log all say the same short thing instead of each showing the raw error."""
import json, re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

STATUS = Path("data/search_status.json")
SOURCE = "Brave Search"                    # the one web search source mcp.json runs today
RATE_WAIT = 5.0                            # seconds before the one retry of a rate-limited search
FINDER_RETRY = 30 * 60                     # seconds the finder waits out an outage before it tries search again

KINDS = {
    "credit": "is out of prepaid credit",
    "key": "refused the key",
    "rate": "is over its rate limit",
    "unreachable": "can't be reached",
}
FIX = {
    "credit": "Top up the search provider's credit, or add another search source.",
    "key": "Paste a working key under Web search, or add another search source.",
    "rate": "Wait a little, or add another search source.",
    "unreachable": "Check this machine's internet connection, or add another search source.",
}

@dataclass
class Outage:
    kind: str                              # a key of KINDS
    source: str = SOURCE

    @property
    def reason(self) -> str:
        return f"{self.source} {KINDS[self.kind]}"

_CODE = re.compile(r"^\s*(?:error:\s*)?(?:[\w.]+ failed:\s*)?\D{0,60}?\b(40[1-3]|429)\b")
_CREDIT = re.compile(r"credit|quota|payment required|insufficient balance|billing", re.I)
_RATE = re.compile(r"rate.?limit|too many requests", re.I)
_KEY = re.compile(r"invalid (api )?key|unauthori[sz]ed|subscription token|forbidden", re.I)
_DOWN = re.compile(r"fetch failed|connecterror|connection (refused|reset|error)|enotfound|econnrefused|"
                   r"getaddrinfo|network is unreachable|name or service not known|timed out|timeout", re.I)

def classify(result: str, source: str = SOURCE) -> Outage | None:
    """The outage a search tool's answer shows, or None: a working answer, no results, or an error that isn't
    about the search source itself (a bad argument, say)."""
    if not result.startswith("ERROR"):
        return None
    head = result[:600]
    code = _CODE.search(head.lower())
    status = code.group(1) if code else ""
    if status == "402" or _CREDIT.search(head):
        return Outage("credit", source)
    if status == "429" or _RATE.search(head):
        return Outage("rate", source)
    if status in ("401", "403") or _KEY.search(head):
        return Outage("key", source)
    if _DOWN.search(head):
        return Outage("unreachable", source)
    return None

def note(outage: Outage) -> str:
    """What a search tool answers the model with while search is down, instead of the raw error."""
    return (f"Web search is down ({outage.reason}). Don't search again in this run. Fetch the pages you already "
            "have, and follow their links with page_links.")

def _now() -> datetime:
    return datetime.now(timezone.utc)

def record(outage: Outage) -> bool:
    """Keep this outage; True if it is new (search was working, or down for another reason)."""
    old = current()
    new = not old or old.get("reason") != outage.reason
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps({
        "kind": outage.kind, "source": outage.source, "reason": outage.reason, "fix": FIX[outage.kind],
        "since": _now().isoformat() if new else old.get("since"),
        "checked": _now().isoformat(),
        "finder_noted": False if new else bool(old.get("finder_noted")),
    }))
    return new

def clear() -> bool:
    """A search worked: search is up. True if it was down."""
    if not STATUS.exists():
        return False
    STATUS.unlink(missing_ok=True)
    return True

def current() -> dict | None:
    """The latest outage, or None while search works (as far as anyone has seen)."""
    try:
        data = json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("reason") else None

def finder_should_wait() -> tuple[bool, str]:
    """(True, line to log or "") while search is down and the finder tried it less than FINDER_RETRY ago. The
    line is given once per outage, so a resting loop doesn't log the same skip every sweep."""
    data = current()
    if not data:
        return False, ""
    try:
        checked = datetime.fromisoformat(data["checked"])
    except (KeyError, ValueError):
        return False, ""
    if (_now() - checked).total_seconds() >= FINDER_RETRY:
        return False, ""                   # long enough: let the finder try search again
    if data.get("finder_noted"):
        return True, ""
    data["finder_noted"] = True
    STATUS.write_text(json.dumps(data))
    return True, f"finder skipped: web search is down ({data['reason']})"
