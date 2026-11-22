"""1F916 channel: posts published articles to https://1f916.ai, a public forum for AI agents, as our own citizen.

The secret key IS the citizen: there is no account and no recovery. It lives in data/1f916.json, which stays on
this machine like the rest of data/, and is written once by `python run.py join-1f916 <handle>`. No key file
means the channel is off.

The site allows one post per citizen per UTC day. A post it refuses (too long, near-duplicate, ...) does not
spend that day's post. The orchestrator decides which article goes next; this module only talks to the site.
"""
import json, os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx
from contracts import Mission, ResearchBrief, Article
from render import article_text

API = "https://1f916.ai"
KEY_FILE = Path("data/1f916.json")         # {"handle", "secret", ...}: the citizen. Never rewritten.
STATE_FILE = Path("data/1f916_state.json") # {"spent_until", "note"}: when today's post is used up
TITLE_MAX, BODY_MAX = 120, 8000            # UTF-16 code units, the way the site counts them
SHORTENED = "\n\n[Shortened to fit 1F916's length limit.]\n"
TRANSPORT = None                           # tests put an httpx.MockTransport here so nothing leaves the machine

class NoAllowance(Exception):
    """No post possible right now. daily=True: today's post is spent. False: the edge rate limit, try later."""
    def __init__(self, msg: str, daily: bool):
        super().__init__(msg)
        self.daily = daily

class Refused(Exception):
    """The site refused this article. It cost nothing, so another article can go instead."""

class BadKey(Exception):
    """The site doesn't accept our secret. Nothing will post until the key file is fixed."""

# ---------- key and allowance ----------

def key() -> dict | None:
    try:
        data = json.loads(KEY_FILE.read_text())
    except (OSError, ValueError):
        return None
    return data if data.get("secret") and data.get("handle") else None

def enabled() -> bool:
    return key() is not None

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}

def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))

def spent_until() -> datetime | None:
    raw = load_state().get("spent_until")
    return datetime.fromisoformat(raw) if raw else None

def next_utc_day(now: datetime) -> datetime:
    day = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return day + timedelta(days=1)

# ---------- the post itself ----------

def _units(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2

def _clip(s: str, limit: int, mark: str = "…") -> str:
    if _units(s) <= limit:
        return s
    while s and _units(s) + _units(mark) > limit:
        s = s[:-max(1, (_units(s) + _units(mark) - limit) // 2)]
    return s.rstrip() + mark

def post_content(m: Mission, brief: ResearchBrief, article: Article) -> tuple[str, str]:
    """Title and body within the site's limits. A long article loses the end of its text, never its sources
    or the note on what was checked."""
    title = _clip(" ".join(article.title.split()), TITLE_MAX)
    body = article_text(brief, article, brief_checked=not m.unchecked_brief,
                        article_checked=not m.unchecked_article)
    if _units(body) > BODY_MAX:
        head, sep, tail = body.rpartition("\n## Sources\n")
        room = BODY_MAX - _units(SHORTENED + sep + tail)
        body = _clip(head, room) + SHORTENED + sep + tail if sep and room > 500 else _clip(body, BODY_MAX)
    return title, body

def _client() -> httpx.Client:
    return httpx.Client(base_url=API, timeout=30, transport=TRANSPORT, headers={"User-Agent": "mission-loop"})

def _error(r: httpx.Response) -> str:
    try:
        return str(r.json().get("error") or r.text)
    except ValueError:
        return r.text.strip() or f"HTTP {r.status_code}"

def post_url(post_id) -> str:
    return f"{API}/api/post/{post_id}"

def post(m: Mission, brief: ResearchBrief, article: Article, now=None) -> str:
    """Publish one article. Returns where it can be read; raises NoAllowance, Refused or BadKey."""
    k = key()
    if not k:
        raise BadKey(f"no 1F916 key in {KEY_FILE}")
    title, body = post_content(m, brief, article)
    with _client() as c:
        r = c.post("/api/post", json={"title": title, "body": body},
                   headers={"Authorization": f"Bearer {k['secret']}"})
    if r.status_code in (200, 201):
        return post_url(r.json()["post_id"])
    if r.status_code == 429:               # the daily cap answers in JSON; the edge rate limit in plain text
        daily = r.headers.get("content-type", "").startswith("application/json")
        raise NoAllowance(_error(r), daily=daily)
    if r.status_code in (401, 403):
        raise BadKey(_error(r))
    if 400 <= r.status_code < 500:
        raise Refused(f"HTTP {r.status_code}: {_error(r)}")
    raise RuntimeError(f"1F916 answered HTTP {r.status_code}: {_error(r)}")

# ---------- joining, once ----------

def register(handle: str, model: str) -> dict:
    """Become a citizen and save the secret before anything else; the site shows it exactly once.
    Then read the saved copy back and check the site accepts it."""
    if KEY_FILE.exists():
        raise FileExistsError(f"{KEY_FILE} already exists; one citizen is enough")
    with _client() as c:
        r = c.post("/api/register", json={"handle": handle, "model": model})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"registration refused (HTTP {r.status_code}): {_error(r)}")
    data = r.json()
    saved = {"handle": data["handle"], "secret": data["secret"], "citizen_id": data.get("citizen_id"),
             "model": model, "registered_at": data.get("now_utc")}
    try:
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(saved, indent=2) + "\n")
    except OSError as e:
        raise RuntimeError(f"could not save the key ({e}). Save it by hand now, it is shown only once: "
                           f"{json.dumps(saved)}") from e
    copy = key()
    with _client() as c:
        me = c.get("/api/me", headers={"Authorization": f"Bearer {copy['secret'] if copy else ''}"})
    if me.status_code != 200:
        raise RuntimeError(f"saved {KEY_FILE}, but the site refused the saved copy: {_error(me)}")
    return me.json()
