"""Bluesky channel: announces each feed entry on Bluesky as a thread of THREAD (3) posts, every one linking to
the article's other public copies.

Credentials stay on this machine: the UI's Settings page saves them to data/bluesky.json (mode 600), or .env sets
BLUESKY_HANDLE (e.g. missionloop.bsky.social) and BLUESKY_APP_PASSWORD; the saved file wins. Use an app password
from Bluesky's Settings > Privacy and security > App passwords, never the account password. Without a handle and
password, the channel is off. The server is config too: a login saved on the Settings page keeps the server it
was checked against; a login in .env uses BLUESKY_SERVICE (default https://bsky.social, settings.bluesky_service).

A post is at most 300 characters, so the thread carries the three most clickbaity lines the loop already has, word
for word and with no model: the title (the writer's hook), the approved brief's boldest possibility (labeled, its
"based on ..." left to the article) and its most concrete fact. Each post ends with a "Full article: site · 1F916"
line whose words link to the article on the published site (settings.site_url, which is the GitHub Pages address
once a repo is linked) and to its 1F916 post, whichever exist. The first post also gets a link card (the site, else
1F916). With GitHub Pages on, an entry waits until it is on the site.
After each article the channel waits POST_GAP, so a backlog goes out about one a pass instead of all at once.
The orchestrator decides which entry goes next (see agents/channel.py); this module only talks to Bluesky.
"""
import json, os, re
from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx
import settings, store
from agents.channel import NoAllowance, Refused
from contracts import CITATION, POSSIBILITY
from render import POSSIBILITY_LABEL

KEY, NAME = "bluesky", "Bluesky"
TEXT_MAX = 300                             # Bluesky counts graphemes; code points are never fewer, so this is safe
POST_GAP = timedelta(minutes=10)           # with a 15-minute loop, one article a pass
MANUAL_SKIPS_WAIT = True                   # the gap is ours, not Bluesky's: a human's "publish now" goes first
THREAD = 3                                 # posts per article: the hook, the boldest possibility, the hardest fact
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

# ---------- the thread itself ----------

def _clip(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    cut = s[:max(0, limit - 1)]
    cut = cut[:cut.rfind(" ")] if " " in cut[limit // 2:] else cut   # end on a word when one is near
    return cut.rstrip(" ,;:") + "…"

SENTENCE_END = re.compile(r"(?<!\bal)(?<!\bvs)[a-z0-9)\"'”’][.?!](?=\s)")   # not "U.S.", "et al." or "vs."
CLAUSE_END = re.compile(r"[,;:](?=\s)|\s[—–-]\s")

def _cut(s: str, limit: int) -> str:
    """s within limit, ending on a whole sentence when one is near, else a clause, else a word."""
    if len(s) <= limit:
        return s
    head = s[:limit - 1]
    for pattern, closed in ((SENTENCE_END, True), (CLAUSE_END, False)):
        ends = [e for e in (m.end() if closed else m.start() for m in pattern.finditer(head))
                if head[:e].count("(") <= head[:e].count(")")]       # never leave a bracket open
        if ends and ends[-1] >= limit // 2:
            cut = head[:ends[-1]].rstrip(" ,;:—–-")
            return cut if closed else cut + "…"
    return _clip(s, limit)

BASIS = re.compile(r",?\s+based on\b.*$", re.I | re.S)       # the prompts word possibilities "..., based on ..."
SOURCE_NOTE = re.compile(r"\s*\((?=[^()]*\b[\w-]+\.(?:com|org|net|gov|edu|io|ai|co|uk|us)\b)[^()]*\)\s*\.?\s*$")  # "(site.com, ...)"
LEAD_IN = re.compile(r"^(?:However|But|Still|Yet|Moreover|Meanwhile|Indeed|Notably|Instead),?\s+(?=\w)")
CONCRETE = re.compile(r"\d[\d,.]*%?|\"[^\"]{3,}\"|“[^”]{3,}”|(?<!\w)'[^']{3,80}'(?!\w)")

def _tidy(text: str) -> str:
    text = " ".join(re.sub(r"\s*" + CITATION.pattern, "", text).split())
    text = LEAD_IN.sub("", SOURCE_NOTE.sub("", text).rstrip())   # a post is read alone: no "However," opener
    text = text[:1].upper() + text[1:]
    return text if not text or text[-1] in ".?!…\"”'’" else text + "."

def _boldest_possibility(claims: list[dict]) -> str:
    """The first pointed possibility (the prompts lead with them), without the "based on ..." that backs it:
    the claim alone is the hook, and its label says it isn't verified."""
    for c in claims:
        if c.get("kind") == "possibility":
            text = " ".join(c["text"].split())
            bare = BASIS.sub("", text)
            return _tidy(bare if len(bare) >= 40 else text)
    return ""

def _hardest_fact(claims: list[dict]) -> str:
    """The fact with the most concrete details (numbers, quoted words); the first of equals."""
    facts = [c["text"] for c in claims if c.get("kind", "fact") == "fact"]
    return _tidy(max(facts, key=lambda t: len(CONCRETE.findall(t)))) if facts else ""

def links(entry: dict) -> list[tuple[str, str]]:
    """Every other public copy of the article, as (label, address): the site when its address is set (the
    GitHub Pages site, or wherever settings.site_url points), then its 1F916 post if it has one."""
    out = []
    if settings.site_url():
        out.append(("site", settings.site_url().rstrip("/") + f"/{entry['slug']}/"))
    m = store.get_mission(entry["slug"])
    f916 = m.channels.get("1f916") if m else None   # 1F916 posts first in a pass, so a fresh one counts
    if f916 and f916.where.startswith("https://"):
        out.append(("1F916", f916.where))
    return out

def _waiting_for_site(entry: dict) -> bool:
    """True while the GitHub Pages channel is on and hasn't put this entry on the site yet, so the link would
    lead nowhere. It posts first in a pass (agents.CHANNELS), so this only holds while GitHub is failing."""
    from agents import github
    if not github.enabled() or settings.site_url() != github.site_address():
        return False
    m = store.get_mission(entry["slug"])
    return bool(m) and github.KEY not in m.channels

def article_link(entry: dict) -> str:
    """Where the link card goes: the site, else the 1F916 post, else "" when there is no public copy."""
    found = links(entry)
    return found[0][1] if found else ""

def _post(body: str, marks: str, found: list[tuple[str, str]]) -> dict:
    """One post: the labels, the body cut to fit, then a line linking every other channel. The link line
    shows short words ("site", "1F916") and carries the full addresses as facets, so it costs few characters."""
    line = ("Full article: " + " · ".join(label for label, _ in found)) if found else ""
    room = TEXT_MAX - len(marks) - (len(line) + 2 if line else 0)
    text = marks + _cut(body, room) + ("\n\n" + line if line else "")
    facets, at = [], len(text) - len(line)
    for label, url in found:
        i = text.index(label, at)
        start = len(text[:i].encode())
        facets.append({"index": {"byteStart": start, "byteEnd": start + len(label.encode())},
                       "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}]})
        at = i + len(label)
    return {"text": text, "facets": facets}

def thread(entry: dict) -> list[dict]:
    """At most THREAD posts, each {"text", "facets"}, all straight from the approved brief and the article,
    no model: the title (the writer's hook), the brief's boldest possibility, and its most concrete fact.
    Missing pieces fall back to the thesis, then the dek. A possibility keeps its "It is possible that" and
    gets the label, and a pushed-through article says so on every post, since replies are read on their own."""
    brief = store.load_artifact(entry["slug"], "brief") or {}
    claims = brief.get("claims", [])
    picks = [entry["title"], _boldest_possibility(claims), _hardest_fact(claims),
             _tidy(brief.get("thesis", "")), _tidy(entry.get("dek", ""))]
    bodies, seen = [], set()
    for text in picks:
        text, key = " ".join(text.split()), text.strip().rstrip(".?!…").lower()
        if text and key not in seen:                 # the title is sometimes the thesis word for word
            bodies.append(text)
            seen.add(key)
    found, unchecked = links(entry), UNCHECKED_MARK in entry.get("text", "")
    out = []
    for body in bodies[:THREAD]:
        marks = (f"{UNCHECKED} " if unchecked else "") + (f"[{POSSIBILITY_LABEL}] " if POSSIBILITY.search(body) else "")
        out.append(_post(body, marks, found))
    return out

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
    """Announce one feed entry as a thread. Returns the first post's bsky.app address. Raises NoAllowance
    (rate limited), Refused, or anything else for "try again next pass" (noted in the state file for the UI).
    Once the first post is up the article counts as posted: a reply that fails is noted, never retried, so a
    retry can't post the thread twice."""
    if _waiting_for_site(entry):
        raise NoAllowance("waiting for GitHub Pages to put the article on the site")
    try:
        where, note = _send(entry, now)
    except Refused:
        raise
    except Exception as e:
        _save_state(note=f"{type(e).__name__}: {e}")
        raise
    _save_state(next_at=(now + POST_GAP).isoformat(), note=note)
    return where

def _send(entry: dict, now: datetime) -> tuple[str, str]:
    posts, link = thread(entry), article_link(entry)
    created = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    with _client() as c:
        session = login(c)

        def make(p: dict, **extra) -> dict:
            record = {"$type": "app.bsky.feed.post", "text": p["text"], "langs": ["en"], "createdAt": created,
                      **({"facets": p["facets"]} if p["facets"] else {}), **extra}
            made = _check(c.post("/xrpc/com.atproto.repo.createRecord",
                                 headers={"Authorization": f"Bearer {session['accessJwt']}"},
                                 json={"repo": session["did"], "collection": "app.bsky.feed.post", "record": record}),
                          "the post")
            return {"uri": made["uri"], "cid": made["cid"]}

        card = {"embed": {"$type": "app.bsky.embed.external",
                          "external": {"uri": link, "title": entry["title"], "description": entry.get("dek", "")}}}
        root = make(posts[0], **(card if link else {}))
        parent, note = root, ""
        for i, p in enumerate(posts[1:], 2):
            try:
                parent = make(p, reply={"root": root, "parent": parent})
            except Exception as e:
                note = f"post {i} of {len(posts)} in the thread failed: {type(e).__name__}: {e}"
                break
    return web_url(root["uri"], session.get("handle") or session["did"]), note

def check_login(handle: str = "", password: str = "", service: str = "") -> str:
    """Log in without posting (with the given handle, password and server, else the configured ones); returns
    the handle Bluesky knows us by."""
    with _client(service.rstrip("/")) as c:
        return login(c, _clean(handle, password)).get("handle", "")
