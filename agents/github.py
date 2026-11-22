"""GitHub Pages channel: keeps a public GitHub repo identical to site/, so GitHub Pages serves the published site
and Bluesky posts can link to each article there.

Setting it up is two steps on the Settings page, done once:
1. Connect GitHub. GitHub's device flow: the page shows a short code, you enter it at github.com/login/device and
   approve. No password or token is ever typed into the app. The token GitHub hands back is saved to
   data/github.json (mode 600, never committed) and can only reach public repos (scope public_repo).
2. Pick the site's repo: "Create my site" makes a new public repo (by default <login>.github.io, which GitHub
   serves at https://<login>.github.io/ with nothing else to switch on), or paste the link of a repo you made.

Then every time the loop posts to channels, sync() pushes whatever changed in site/ as one commit (GitHub's Git
Data API: only changed files are uploaded; nothing needs git installed). The repo holds the site and nothing else:
files that aren't in site/ are removed, except KEEP (a custom domain's CNAME, a README, a licence). To protect a
repo that holds something else, linking refuses one that has other files and no site in it yet.

The sign-in needs this app's GitHub OAuth app (settings.github_client_id): one registered app serves every copy
of mission-loop; a fork can register its own on the Settings page. The orchestrator decides which feed entry goes
next (see agents/channel.py); this module only talks to GitHub.
"""
import base64, hashlib, json, os, re, time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
import httpx
import settings
from agents.channel import NoAllowance, Refused

KEY, NAME = "github", "GitHub Pages"
SCOPE = "public_repo"                     # public repos only: enough to create the site's repo and push to it
CRED_FILE = Path("data/github.json")      # {"token", "login", "server", "repo", "branch", "site"}
STATE_FILE = Path("data/github_state.json")  # {"pushed", "repo", "at", "note", "pages_note"}: last push, why the
                                          # last try failed, and whether Pages still needs switching on by hand
SITE = Path("site")
KEEP = {"CNAME", "README.md", "LICENSE", "LICENSE.md", ".gitignore"}  # not ours, but kept in the repo
NOJEKYLL = ".nojekyll"                    # serve the files as they are (no Jekyll build, nothing hidden)
DEFAULT_NAME = "{login}.github.io"        # the repo name GitHub serves at the top of the user's own address
TRANSPORT = None                          # tests put an httpx.MockTransport here so nothing leaves the machine
REPO_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
OWNER = re.compile(r"[A-Za-z0-9-]{1,39}")

class BadLogin(Exception):
    """GitHub no longer accepts the saved token (revoked, or the app's access was removed). Connect again."""

class LoginFailed(Exception):
    """The device sign-in ended without a token: denied, expired, or the OAuth app isn't set up for it."""

# ---------- the saved connection ----------

def saved() -> dict:
    try:
        data = json.loads(CRED_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}

def _save(data: dict) -> None:
    """Write data/github.json readable only by this user."""
    CRED_FILE.parent.mkdir(parents=True, exist_ok=True)
    CRED_FILE.unlink(missing_ok=True)
    fd = os.open(CRED_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=2) + "\n")

def connected() -> bool:
    return bool(saved().get("token"))

def enabled() -> bool:
    data = saved()
    return bool(data.get("token") and data.get("repo"))

def login_name() -> str:
    return str(saved().get("login") or "")

def repo() -> str:
    return str(saved().get("repo") or "")

def site_address() -> str:
    """Where GitHub Pages serves the linked repo, with a trailing slash; "" until one is linked."""
    data = saved()
    return str(data.get("site") or "") if data.get("token") and data.get("repo") else ""

def forget() -> None:
    """Disconnect: delete the token (GitHub keeps the repo and the site)."""
    CRED_FILE.unlink(missing_ok=True)
    STATE_FILE.unlink(missing_ok=True)

def unlink_site() -> None:
    data = saved()
    for k in ("repo", "branch", "site"):
        data.pop(k, None)
    _save(data)
    STATE_FILE.unlink(missing_ok=True)

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}

def _save_state(**changes) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(load_state() | changes))

def wait_until() -> datetime | None:
    return None                           # no cap: a push costs nothing

# ---------- talking to GitHub ----------

def _web() -> str:
    return str(saved().get("server") or "") or settings.github_url()

def _client(base: str, token: str = "") -> httpx.Client:
    headers = {"User-Agent": "mission-loop", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=base, timeout=30, transport=TRANSPORT, headers=headers)

def _api(token: str = "") -> httpx.Client:
    return _client(settings.github_api(_web()), token or str(saved().get("token") or ""))

def _error(r: httpx.Response) -> str:
    try:
        body = r.json()
        return str(body.get("message") or body.get("error_description") or body.get("error") or r.text)
    except ValueError:
        return r.text.strip() or f"HTTP {r.status_code}"

def _check(r: httpx.Response, what: str) -> dict | list:
    if r.status_code in (200, 201):
        return r.json()
    if r.status_code == 401:
        raise BadLogin(f"GitHub refused the saved sign-in ({_error(r)}); connect GitHub again on the Settings page")
    if r.status_code == 429 or (r.status_code == 403 and r.headers.get("x-ratelimit-remaining") == "0"):
        raise NoAllowance(f"GitHub rate limit: {_error(r)}")
    raise RuntimeError(f"GitHub answered HTTP {r.status_code} to {what}: {_error(r)}")

# ---------- step 1: connect, with GitHub's device flow ----------

def start_login() -> dict:
    """Ask GitHub for a sign-in code. Returns {"user_code", "verification_uri", "device_code", "interval",
    "expires_at"}: show the code and the address, then call poll_login every `interval` seconds."""
    client_id = settings.github_client_id()
    if not client_id:
        raise LoginFailed("this copy has no GitHub OAuth app yet; add its client ID on the Settings page")
    with _client(settings.github_url()) as c:
        r = c.post("/login/device/code", data={"client_id": client_id, "scope": SCOPE},
                   headers={"Accept": "application/json"})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "device_code" not in body:
        raise LoginFailed(_login_error(body.get("error", ""), body.get("error_description", "") or r.text))
    return {"user_code": body["user_code"], "verification_uri": body["verification_uri"],
            "device_code": body["device_code"], "interval": int(body.get("interval", 5)),
            "expires_at": time.time() + int(body.get("expires_in", 900)), "server": settings.github_url()}

def _login_error(code: str, description: str) -> str:
    return {
        "access_denied": "the sign-in was cancelled on GitHub",
        "expired_token": "the code expired before it was entered; start again",
        "device_flow_disabled": "the GitHub OAuth app doesn't have Device Flow turned on (its settings on GitHub)",
        "incorrect_client_credentials": "GitHub doesn't know that OAuth app client ID",
        "unauthorized_client": "GitHub doesn't know that OAuth app client ID",
    }.get(code, f"{code}: {description}".strip(": ") or "GitHub refused the sign-in")

def poll_login(pending: dict) -> str:
    """One check on a sign-in started with start_login: "pending" (keep polling), "slow_down" (poll 5 s less
    often), or "done" once GitHub handed over a token, which is then saved. Raises LoginFailed."""
    if time.time() > pending["expires_at"]:
        raise LoginFailed(_login_error("expired_token", ""))
    with _client(pending["server"]) as c:
        body = c.post("/login/oauth/access_token", headers={"Accept": "application/json"},
                      data={"client_id": settings.github_client_id(), "device_code": pending["device_code"],
                            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}).json()
    if body.get("error") in ("authorization_pending", "slow_down"):
        return "pending" if body["error"] == "authorization_pending" else "slow_down"
    if body.get("error") or not body.get("access_token"):
        raise LoginFailed(_login_error(body.get("error", ""), body.get("error_description", "")))
    token = body["access_token"]
    with _client(settings.github_api(pending["server"]), token) as c:
        me = _check(c.get("/user"), "who am I")
    old = saved()
    keep = {k: old[k] for k in ("repo", "branch", "site") if k in old} if old.get("login") == me["login"] else {}
    _save({"token": token, "login": me["login"], "server": pending["server"]} | keep)
    return "done"

# ---------- step 2: the site's repo ----------

def suggested_name() -> str:
    return DEFAULT_NAME.format(login=login_name()) if login_name() else "mission-loop-site"

def parse_repo(text: str) -> str:
    """"owner/name" from what someone pastes: https://github.com/owner/name(.git), owner/name, or the Pages
    address https://owner.github.io/name/. Raises ValueError for anything else."""
    text = text.strip()
    if "://" not in text:
        first = text.lstrip("/").split("/")[0]
        text = ("https://" if "." in first else "https://github.com/") + text.lstrip("/")
    parts = urlparse(text)
    path = [p for p in parts.path.split("/") if p]
    host = parts.netloc.lower()
    if host.endswith(".github.io"):
        owner = host.removesuffix(".github.io")
        found = (owner, path[0] if path else host)
    elif len(path) >= 2:
        found = (path[0], path[1].removesuffix(".git"))
    else:
        raise ValueError("paste a GitHub repo link, like https://github.com/you/your-site")
    if not (OWNER.fullmatch(found[0]) and REPO_NAME.fullmatch(found[1])):
        raise ValueError("that doesn't look like a GitHub repo")
    return f"{found[0]}/{found[1]}"

def pages_address(full: str) -> str:
    """Where GitHub Pages serves a repo when it has no custom domain."""
    owner, name = full.split("/")
    if name.lower() == f"{owner.lower()}.github.io":
        return f"https://{name.lower()}/"
    return f"https://{owner.lower()}.github.io/{name}/"

def create_site(name: str = "") -> str:
    """Make a new public repo for the site and link it. Returns the site address."""
    name = (name.strip() or suggested_name())
    if not REPO_NAME.fullmatch(name):
        raise ValueError("a repo name is letters, digits, '.', '_' or '-'")
    with _api() as c:
        r = c.post("/user/repos", json={"name": name, "description": "Articles published by mission-loop",
                                        "homepage": pages_address(f"{login_name()}/{name}"), "auto_init": True,
                                        "has_issues": False, "has_projects": False, "has_wiki": False})
        if r.status_code == 422:
            raise ValueError(f"you already have a repo called {name}; paste its link instead, or pick another name")
        made = _check(r, "creating the repo")
    return link_site(made["full_name"])

def link_site(text: str) -> str:
    """Check the pasted repo can hold the site, turn GitHub Pages on for it, and save it. Returns the site
    address. Raises ValueError with what is wrong (private, no push access, holds something else)."""
    full = parse_repo(text)
    with _api() as c:
        r = c.get(f"/repos/{full}")
        if r.status_code == 404:
            raise ValueError(f"GitHub has no repo {full} that {login_name() or 'this account'} can see")
        info = _check(r, "reading the repo")
        if info.get("private"):
            raise ValueError(f"{full} is private; make it public (GitHub Pages and this app's access need that)")
        if not info.get("permissions", {}).get("push"):
            raise ValueError(f"{login_name() or 'this account'} can't push to {full}")
        full, branch = info["full_name"], info.get("default_branch") or "main"
        others = [p for p in _remote_files(c, full, branch) if p not in KEEP and p != NOJEKYLL]
        if others and "feed.json" not in others:
            raise ValueError(f"{full} already holds other files ({', '.join(sorted(others)[:3])}…); the site "
                             "replaces everything in its repo, so use a new or empty one")
        site, note = _turn_on_pages(c, full, branch)
    old = saved()
    _save(old | {"repo": full, "branch": branch, "site": site})
    _save_state(pushed="", repo=full, note="", pages_note=note)
    return site

def _turn_on_pages(c: httpx.Client, full: str, branch: str) -> tuple[str, str]:
    """The site's address, and a note when Pages still needs switching on by hand. A <login>.github.io repo is
    served without asking; for any other, Pages is turned on to publish the default branch as it is."""
    r = c.get(f"/repos/{full}/pages")
    if r.status_code == 200:
        return r.json().get("html_url") or pages_address(full), ""
    if full.split("/")[1].lower() == f"{full.split('/')[0].lower()}.github.io":
        return pages_address(full), ""
    r = c.post(f"/repos/{full}/pages", json={"source": {"branch": branch, "path": "/"}})
    if r.status_code in (201, 409):       # 409: already on
        return (r.json().get("html_url") if r.status_code == 201 else "") or pages_address(full), ""
    return pages_address(full), (f"turn on GitHub Pages for {full}: Settings > Pages > Deploy from a branch > "
                                 f"{branch}, / (root), at {_web()}/{full}/settings/pages")

# ---------- keeping the repo identical to site/ ----------

def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()

def _local_files(site: Path) -> dict[str, bytes]:
    files = {p.relative_to(site).as_posix(): p.read_bytes() for p in sorted(site.rglob("*")) if p.is_file()}
    files.setdefault(NOJEKYLL, b"")
    return files

def _head(c: httpx.Client, full: str, branch: str) -> str:
    """The branch's latest commit, or "" while the repo is empty."""
    r = c.get(f"/repos/{full}/git/ref/heads/{branch}")
    if r.status_code in (404, 409):
        return ""
    return _check(r, "reading the branch")["object"]["sha"]

def _tree(c: httpx.Client, full: str, commit: str) -> dict[str, str]:
    """path -> blob sha for every file in a commit."""
    tree = _check(c.get(f"/repos/{full}/git/commits/{commit}"), "reading the last commit")["tree"]["sha"]
    found = _check(c.get(f"/repos/{full}/git/trees/{tree}", params={"recursive": "1"}), "reading the files")
    return {e["path"]: e["sha"] for e in found.get("tree", []) if e.get("type") == "blob"}

def _remote_files(c: httpx.Client, full: str, branch: str) -> dict[str, str]:
    head = _head(c, full, branch)
    return _tree(c, full, head) if head else {}

def _fingerprint(files: dict[str, str]) -> str:
    return hashlib.sha1(json.dumps(sorted(files.items())).encode()).hexdigest()

def sync(now: datetime | None = None, site: Path | None = None) -> bool:
    """Push site/ to the repo when it changed since the last push. Returns True when a commit was made."""
    data = saved()
    full, branch = data.get("repo", ""), data.get("branch", "main")
    local = _local_files(site or SITE)
    shas = {p: _blob_sha(b) for p, b in local.items()}
    state = load_state()
    if state.get("repo") == full and state.get("pushed") == _fingerprint(shas):
        return False                      # nothing changed here since the last push
    try:
        pushed = _push(full, branch, local, shas)
    except Exception as e:
        _save_state(note=f"{type(e).__name__}: {e}")
        raise
    _save_state(repo=full, pushed=_fingerprint(shas), note="", at=(now or datetime.now()).isoformat())
    return pushed

def _push(full: str, branch: str, local: dict[str, bytes], shas: dict[str, str]) -> bool:
    with _api() as c:
        head = _head(c, full, branch)
        if not head:                      # an empty repo: the Git Data API needs one commit to build on
            _check(c.put(f"/repos/{full}/contents/{NOJEKYLL}",
                         json={"message": "Start the site", "content": "", "branch": branch}), "starting the repo")
            head = _head(c, full, branch)
        remote = _tree(c, full, head)
        wanted = shas | {p: s for p, s in remote.items() if p in KEEP and p not in shas}
        if wanted == remote:
            return False
        for path, sha in shas.items():
            if remote.get(path) != sha:
                made = _check(c.post(f"/repos/{full}/git/blobs", json={
                    "content": base64.b64encode(local[path]).decode(), "encoding": "base64"}), f"uploading {path}")
                if made["sha"] != sha:
                    raise RuntimeError(f"GitHub stored {path} differently than it was sent")
        tree = _check(c.post(f"/repos/{full}/git/trees", json={"tree": [
            {"path": p, "mode": "100644", "type": "blob", "sha": s} for p, s in sorted(wanted.items())]}),
            "building the files")["sha"]
        added = sorted({p.split("/")[0] for p in shas if p.endswith("/meta.json") and p not in remote})   # new articles
        message = ("Publish " + ", ".join(added)) if added else "Update the site"
        commit = _check(c.post(f"/repos/{full}/git/commits", json={
            "message": message[:200], "tree": tree, "parents": [head]}), "making the commit")["sha"]
        _check(c.patch(f"/repos/{full}/git/refs/heads/{branch}", json={"sha": commit}), "moving the branch")
    return True

def post(entry: dict, now: datetime) -> str:
    """The entry is on the site once site/ is pushed: push if needed and return the article's address there."""
    if not (SITE / entry["slug"] / "index.html").exists():
        raise Refused("its page isn't in site/")
    sync(now)
    return site_address() + f"{entry['slug']}/"

def check() -> str:
    """Who the saved token signs in as (raises BadLogin when GitHub refuses it)."""
    with _api() as c:
        return _check(c.get("/user"), "who am I")["login"]
