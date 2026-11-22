"""Bluesky channel: announces each feed entry on Bluesky with a short post (title and dek) and a link card.

Credentials stay on this machine: the UI's Settings page saves them to data/bluesky.json (mode 600), or .env sets
BLUESKY_HANDLE (e.g. missionloop.bsky.social) and BLUESKY_APP_PASSWORD; the saved file wins. Use an app password
from Bluesky's Settings > Privacy and security > App passwords, never the account password. Without a handle and
password, the channel is off. The server is config too: a login saved on the Settings page keeps the server it
was checked against; a login in .env uses BLUESKY_SERVICE (default https://bsky.social, settings.bluesky_service).

A post is at most 300 characters, so it carries only the title and the dek; the full article, with its sources and
its "Possibility, not verified" labels, is behind the link. The link goes to the article on the published site when
a site address is set (settings.site_url), else to its 1F916 post if it has one; with neither, the post has no link.
After each post the channel waits POST_GAP, so a backlog goes out about one a pass instead of all at once.
The orchestrator decides which entry goes next (see agents/channel.py); this module only talks to Bluesky.
"""
import json, os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx
import settings, store
from agents.channel import NoAllowance, Refused
from contracts import POSSIBILITY
from render import POSSIBILITY_LABEL

KEY, NAME = "bluesky", "Bluesky"
TEXT_MAX = 300                             # Bluesky counts graphemes; code points are never fewer, so this is safe
POST_GAP = timedelta(minutes=10)           # with a 15-minute loop, one post a pass
UNCHECKED = "[Failed automated checks]"
UNCHECKED_MARK = "[Failed automated checks:"  # what render.article_text puts near the top of an unchecked article
CRED_FILE = Path("data/bluesky.json")      # {"handle", "app_password", "service"}: saved from the Settings page
STATE_FILE = Path("data/bluesky_state.json")  # {"next_at", "note"}: pacing, and why the last try failed
TRANSPORT = None                           # tests put an httpx.MockTransport here so nothing leaves the machine

class BadLogin(Exception):
    """Bluesky doesn't accept the handle and app password. Nothing will post until they are fixed."""

# ---------- credentials and pacing ----------

def _clean(handle: str, password: str) -> tuple[str, str] | None:
    handle, password = handle.strip().lstrip("@"), password.strip()
    return (handle, password) if handle and password else None

def _saved() -> dict:
    try:
        data = json.loads(CRED_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}

def saved_credentials() -> tuple[str, str] | None:
    data = _saved()
    return _clean(str(data.get("handle", "")), str(data.get("app_password", "")))

def credentials() -> tuple[str, str] | None:
    return saved_credentials() or _clean(os.environ.get("BLUESKY_HANDLE", ""),
                                         os.environ.get("BLUESKY_APP_PASSWORD", ""))

def save_credentials(handle: str, password: str, service: str = "") -> None:
    """Write data/bluesky.json readable only by this user, with the server they were checked against.
    Check them with check_login first."""
    found = _clean(handle, password)
    if not found:
        raise ValueError("a handle and an app password are both needed")
    CRED_FILE.parent.mkdir(parents=True, exist_ok=True)
    CRED_FILE.unlink(missing_ok=True)
    fd = os.open(CRED_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"handle": found[0], "app_password": found[1],
                            "service": service.rstrip("/") or _service()}, indent=2) + "\n")

def forget_credentials() -> None:
    CRED_FILE.unlink(missing_ok=True)

def enabled() -> bool:
    return credentials() is not None

def handle() -> str:
    found = credentials()
    return found[0] if found else ""

def _service() -> str:
    """A saved login's own server, else the configured one."""
    data = _saved()
    return (str(data.get("service") or "").rstrip("/") if saved_credentials() else "") or settings.bluesky_service()

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}

def _save_state(**changes) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(load_state() | changes))

def wait_until() -> datetime | None:
    raw = load_state().get("next_at")
    return datetime.fromisoformat(raw) if raw else None

# ---------- the post itself ----------

def _clip(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    cut = s[:max(0, limit - 1)]
    cut = cut[:cut.rfind(" ")] if " " in cut[limit // 2:] else cut   # end on a word when one is near
    return cut.rstrip(" ,;:") + "…"

def post_text(entry: dict) -> str:
    """Title, then the dek, within 300 characters. A dek that states a possibility keeps its label, and an
    article a human pushed through despite failing its checks says so first."""
    title = _clip(" ".join(entry["title"].split()), 120)
    dek = " ".join(entry.get("dek", "").split())
    if POSSIBILITY.search(dek):
        dek = f"[{POSSIBILITY_LABEL}] {dek}"
    if UNCHECKED_MARK in entry.get("text", ""):
        dek = f"{UNCHECKED} {dek}"
    return title + "\n\n" + _clip(dek, TEXT_MAX - len(title) - 2)

def article_link(entry: dict) -> str:
    """Where readers can read the whole article, or "" when there is no public copy."""
    if settings.site_url():
        return settings.site_url().rstrip("/") + f"/{entry['slug']}/"
    m = store.get_mission(entry["slug"])
    f916 = m.channels.get("1f916") if m else None   # 1F916 posts first in a pass, so a fresh one counts
    return f916.where if f916 and f916.where.startswith("https://") else ""

def _client(service: str = "") -> httpx.Client:
    return httpx.Client(base_url=service or _service(), timeout=30, transport=TRANSPORT, headers={"User-Agent": "mission-loop"})

def _error(r: httpx.Response) -> str:
    try:
        body = r.json()
        return f'{body.get("error", "")}: {body.get("message", "")}'.strip(": ") or r.text
    except ValueError:
        return r.text.strip() or f"HTTP {r.status_code}"

def _check(r: httpx.Response, what: str) -> dict:
    if r.status_code == 200:
        return r.json()
    if r.status_code == 429:
        raise NoAllowance(_error(r))
    if r.status_code == 401 or (what == "login" and r.status_code == 400):
        raise BadLogin(_error(r))
    if 400 <= r.status_code < 500:
        raise Refused(f"HTTP {r.status_code}: {_error(r)}")
    raise RuntimeError(f"Bluesky answered HTTP {r.status_code} to {what}: {_error(r)}")

def login(c: httpx.Client, creds: tuple[str, str] | None = None) -> dict:
    """A session: {"did", "handle", "accessJwt", ...}. Raises BadLogin when the app password is wrong."""
    found = creds or credentials()
    if not found:
        raise BadLogin("no Bluesky handle and app password: add them on the Settings page or in .env")
    return _check(c.post("/xrpc/com.atproto.server.createSession",
                         json={"identifier": found[0], "password": found[1]}), "login")

def web_url(uri: str, actor: str) -> str:
    """at://did/app.bsky.feed.post/<rkey> -> the bsky.app page for that post."""
    return f"https://bsky.app/profile/{actor}/post/{uri.rsplit('/', 1)[-1]}"

def post(entry: dict, now: datetime) -> str:
    """Announce one feed entry. Returns the post's bsky.app address. Raises NoAllowance (rate limited),
    Refused, or anything else for "try again next pass" (noted in the state file for the UI)."""
    try:
        where = _send(entry, now)
    except Refused:
        raise
    except Exception as e:
        _save_state(note=f"{type(e).__name__}: {e}")
        raise
    _save_state(next_at=(now + POST_GAP).isoformat(), note="")
    return where

def _send(entry: dict, now: datetime) -> str:
    record = {"$type": "app.bsky.feed.post", "text": post_text(entry), "langs": ["en"],
              "createdAt": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
    link = article_link(entry)
    if link:
        record["embed"] = {"$type": "app.bsky.embed.external",
                           "external": {"uri": link, "title": entry["title"], "description": entry.get("dek", "")}}
    with _client() as c:
        session = login(c)
        made = _check(c.post("/xrpc/com.atproto.repo.createRecord",
                             headers={"Authorization": f"Bearer {session['accessJwt']}"},
                             json={"repo": session["did"], "collection": "app.bsky.feed.post", "record": record}),
                      "the post")
    return web_url(made["uri"], session.get("handle") or session["did"])

def check_login(handle: str = "", password: str = "", service: str = "") -> str:
    """Log in without posting (with the given handle, password and server, else the configured ones); returns
    the handle Bluesky knows us by."""
    with _client(service.rstrip("/")) as c:
        return login(c, _clean(handle, password)).get("handle", "")
