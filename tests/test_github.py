"""The GitHub Pages channel, against a fake GitHub: the device sign-in, creating or linking the site's repo, keeping
the repo identical to site/, and Bluesky linking to the article there."""
import base64, hashlib, json, os, stat
from pathlib import Path
import httpx, pytest
import agents, orchestrator, settings, store, ui
from agents import bluesky, github
from agents.channel import NoAllowance
from starlette.testclient import TestClient
from test_channels import no_outreach, published, run_at, T0

pytestmark = pytest.mark.usefixtures("stub_agents", "no_outreach")
CLIENT_ID = "Ov23liTESTAPP"

def sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()

class FakeGitHub:
    """github.com's device flow and just enough of api.github.com's repos, Git Data and Pages APIs."""
    def __init__(self):
        self.repos, self.blobs, self.trees, self.commits = {}, {}, {}, {}
        self.calls, self.approved, self.uploads, self.pages_api = [], False, [], True

    def add_repo(self, full, files=None, private=False, push=True):
        repo = {"private": private, "push": push, "head": "", "pages": False}
        self.repos[full] = repo
        if files is not None:
            self._commit(repo, {p: self._blob(b) for p, b in files.items()}, "init")
        return repo

    def _blob(self, data: bytes) -> str:
        self.blobs[sha(data)] = data
        return sha(data)

    def _commit(self, repo, tree: dict, message: str) -> str:
        tsha = hashlib.sha1(json.dumps(sorted(tree.items())).encode()).hexdigest()
        self.trees[tsha] = dict(tree)
        csha = hashlib.sha1(f"{tsha}{repo['head']}{message}".encode()).hexdigest()
        self.commits[csha] = {"tree": tsha, "parents": [repo["head"]] if repo["head"] else [], "message": message}
        repo["head"] = csha
        return csha

    def files(self, full) -> dict[str, bytes]:
        head = self.repos[full]["head"]
        return {p: self.blobs[s] for p, s in self.trees[self.commits[head]["tree"]].items()} if head else {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append(f"{method} {path}")
        if request.url.host == "github.com":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            if form.get("client_id") != CLIENT_ID:
                return httpx.Response(200, json={"error": "incorrect_client_credentials"})
            if path == "/login/device/code":
                assert form["scope"] == "public_repo"
                return httpx.Response(200, json={"device_code": "dev-1", "user_code": "WDJB-MJHT", "interval": 5,
                                                 "verification_uri": "https://github.com/login/device",
                                                 "expires_in": 900})
            if path == "/login/oauth/access_token":
                assert form["device_code"] == "dev-1"
                return httpx.Response(200, json={"access_token": "gho_test", "scope": "public_repo"}
                                      if self.approved else {"error": "authorization_pending"})
        assert request.url.host == "api.github.com"
        if request.headers.get("authorization") != "Bearer gho_test":
            return httpx.Response(401, json={"message": "Bad credentials"})
        body = json.loads(request.content) if request.content else {}
        if path == "/user":
            return httpx.Response(200, json={"login": "octo"})
        if path == "/user/repos" and method == "POST":
            full = f"octo/{body['name']}"
            if full in self.repos:
                return httpx.Response(422, json={"message": "name already exists on this account"})
            self.add_repo(full, {"README.md": b"# site\n"} if body.get("auto_init") else None)
            return httpx.Response(201, json={"full_name": full})
        parts = path.split("/")                       # /repos/owner/name/...
        full, rest = "/".join(parts[2:4]), "/".join(parts[4:])
        repo = self.repos.get(full)
        if not repo:
            return httpx.Response(404, json={"message": "Not Found"})
        if rest == "":
            return httpx.Response(200, json={"full_name": full, "private": repo["private"], "default_branch": "main",
                                             "permissions": {"push": repo["push"]}})
        if rest == "pages":
            if method == "GET":
                return (httpx.Response(200, json={"html_url": f"https://octo.github.io/{parts[3]}/"}) if repo["pages"]
                        else httpx.Response(404, json={"message": "Not Found"}))
            if not self.pages_api:
                return httpx.Response(403, json={"message": "Resource not accessible by integration"})
            repo["pages"] = True
            assert body == {"source": {"branch": "main", "path": "/"}}
            return httpx.Response(201, json={"html_url": f"https://octo.github.io/{parts[3]}/"})
        if rest == "git/ref/heads/main":
            return (httpx.Response(200, json={"object": {"sha": repo["head"]}}) if repo["head"]
                    else httpx.Response(409, json={"message": "Git Repository is empty."}))
        if rest.startswith("git/commits/") and method == "GET":
            return httpx.Response(200, json={"tree": {"sha": self.commits[parts[-1]]["tree"]}})
        if rest.startswith("git/trees/") and method == "GET":
            return httpx.Response(200, json={"tree": [{"path": p, "sha": s, "type": "blob", "mode": "100644"}
                                                      for p, s in self.trees[parts[-1]].items()]})
        if rest == "contents/.nojekyll" and method == "PUT":
            self._commit(repo, {".nojekyll": self._blob(b"")}, body["message"])
            return httpx.Response(201, json={})
        if rest == "git/blobs":
            data = base64.b64decode(body["content"])
            self.uploads.append(data)
            return httpx.Response(201, json={"sha": self._blob(data)})
        if rest == "git/trees":
            assert "base_tree" not in body
            tree = {e["path"]: e["sha"] for e in body["tree"]}
            assert all(s in self.blobs for s in tree.values())
            tsha = hashlib.sha1(json.dumps(sorted(tree.items())).encode()).hexdigest()
            self.trees[tsha] = tree
            return httpx.Response(201, json={"sha": tsha})
        if rest == "git/commits":
            csha = hashlib.sha1(json.dumps(body).encode()).hexdigest()
            self.commits[csha] = {"tree": body["tree"], "parents": body["parents"], "message": body["message"]}
            return httpx.Response(201, json={"sha": csha})
        if rest == "git/refs/heads/main" and method == "PATCH":
            assert self.commits[body["sha"]]["parents"] == [repo["head"]]   # a fast-forward, never a force push
            repo["head"] = body["sha"]
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": f"no fake for {method} {path}"})

@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(github, "TRANSPORT", httpx.MockTransport(fake))
    monkeypatch.setenv("MISSION_LOOP_GITHUB_CLIENT_ID", CLIENT_ID)
    return fake

@pytest.fixture
def signed_in(gh):
    github._save({"token": "gho_test", "login": "octo", "server": "https://github.com"})
    return gh

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(orchestrator, "LOCK_FILE", Path("data/loop.lock"))
    monkeypatch.setattr(orchestrator, "WAKE_FLAG", Path("data/WAKE"))
    ui.GITHUB_LOGIN.clear()
    return TestClient(ui.app)

# ---------- sign-in ----------

def test_off_until_signed_in_and_linked(gh):
    published("a", T0)                                # the fake would record a call
    assert not agents.github_enabled() and gh.calls == []
    assert github in agents.CHANNELS and agents.CHANNELS.index(github) < agents.CHANNELS.index(bluesky)

def test_device_sign_in_saves_a_private_token(gh):
    pending = github.start_login()
    assert pending["user_code"] == "WDJB-MJHT" and pending["verification_uri"] == "https://github.com/login/device"
    assert github.poll_login(pending) == "pending" and not github.connected()
    gh.approved = True
    assert github.poll_login(pending) == "done"
    assert github.connected() and not github.enabled() and github.login_name() == "octo"
    assert stat.S_IMODE(os.stat(github.CRED_FILE).st_mode) == 0o600
    assert "device_code" not in github.saved()

def test_sign_in_needs_the_apps_client_id(gh, monkeypatch):
    monkeypatch.delenv("MISSION_LOOP_GITHUB_CLIENT_ID")
    with pytest.raises(github.LoginFailed, match="client ID"):
        github.start_login()
    monkeypatch.setenv("MISSION_LOOP_GITHUB_CLIENT_ID", "Ov23liSOMEONEELSE")
    with pytest.raises(github.LoginFailed, match="doesn't know"):
        github.start_login()
    assert settings.set_github_client_id(CLIENT_ID) == CLIENT_ID
    with pytest.raises(ValueError):
        settings.set_github_client_id("not a client id!")

# ---------- the site's repo ----------

def test_parse_repo_takes_what_people_paste():
    for text in ("https://github.com/octo/news", "github.com/octo/news.git", "octo/news", "https://octo.github.io/news/",
                 "https://github.com/octo/news/tree/main"):
        assert github.parse_repo(text) == "octo/news", text
    assert github.parse_repo("https://octo.github.io/") == "octo/octo.github.io"
    for bad in ("", "news", "https://github.com/octo", "octo/ne ws"):
        with pytest.raises(ValueError):
            github.parse_repo(bad)
    assert github.pages_address("Octo/octo.github.io") == "https://octo.github.io/"
    assert github.pages_address("octo/news") == "https://octo.github.io/news/"

def test_create_site_makes_the_users_own_github_io_and_pushes_the_site(signed_in):
    published("a", T0)
    assert github.suggested_name() == "octo.github.io"
    assert github.create_site() == "https://octo.github.io/"
    assert github.enabled() and github.repo() == "octo/octo.github.io"
    assert "POST /repos/octo/octo.github.io/pages" not in signed_in.calls   # GitHub serves this one by itself
    assert settings.site_url() == "https://octo.github.io/"                   # feeds and Bluesky use it now
    assert github.sync(T0) is True
    files = signed_in.files("octo/octo.github.io")
    local = {p.relative_to("site").as_posix(): p.read_bytes() for p in Path("site").rglob("*") if p.is_file()}
    assert files == local | {".nojekyll": b"", "README.md": b"# site\n"}       # the auto-made README is kept
    assert signed_in.commits[signed_in.repos["octo/octo.github.io"]["head"]]["message"] == "Publish a"
    signed_in.calls.clear()
    assert github.sync(T0) is False and signed_in.calls == []                  # unchanged: GitHub isn't asked

def test_only_changed_files_are_uploaded_and_removed_pages_go(signed_in):
    published("a", T0); published("b", T0)
    github.create_site("news")
    github.sync(T0)
    signed_in.uploads.clear()
    Path("site/a/index.html").write_text("<p>fixed</p>")
    for f in Path("site/b").iterdir():
        f.unlink()
    Path("site/b").rmdir()
    assert github.sync(T0) is True
    assert signed_in.uploads == [b"<p>fixed</p>"]
    files = signed_in.files("octo/news")
    assert files["a/index.html"] == b"<p>fixed</p>" and not any(p.startswith("b/") for p in files)

def test_a_repo_with_pages_off_gets_pages_turned_on(signed_in):
    signed_in.add_repo("octo/news", {})
    assert github.link_site("https://github.com/octo/news") == "https://octo.github.io/news/"
    assert signed_in.repos["octo/news"]["pages"] and not github.load_state().get("pages_note")

def test_when_pages_cant_be_turned_on_the_user_gets_the_link(signed_in):
    signed_in.pages_api = False
    signed_in.add_repo("octo/news", {})
    assert github.link_site("octo/news") == "https://octo.github.io/news/"
    assert "https://github.com/octo/news/settings/pages" in github.load_state()["pages_note"]

def test_an_empty_repo_gets_a_first_commit(signed_in):
    published("a", T0)
    signed_in.add_repo("octo/news")
    github.link_site("octo/news")
    assert github.sync(T0) and "a/index.html" in signed_in.files("octo/news")

def test_linking_refuses_repos_it_would_harm_or_cant_use(signed_in):
    signed_in.add_repo("octo/app", {"main.py": b"print()", "README.md": b"x"})
    signed_in.add_repo("octo/secret", {}, private=True)
    signed_in.add_repo("someone/site", {}, push=False)
    for repo, why in (("octo/app", "other files"), ("octo/secret", "private"), ("someone/site", "can't push"),
                      ("octo/missing", "no repo")):
        with pytest.raises(ValueError, match=why):
            github.link_site(repo)
    assert not github.enabled()
    signed_in.add_repo("octo/old-site", {"feed.json": b"{}", "x/index.html": b"old"})   # a site already: fine
    github.link_site("octo/old-site")
    with pytest.raises(ValueError, match="already have"):
        github.create_site("old-site")

def test_a_revoked_token_says_so(signed_in):
    github.create_site()
    github._save(github.saved() | {"token": "gho_revoked"})
    published("a", T0)
    with pytest.raises(github.BadLogin):
        github.sync(T0)
    assert "connect GitHub again" in github.load_state()["note"]

# ---------- the channel in the loop ----------

def test_the_loop_pushes_and_records_each_articles_page(signed_in):
    github.create_site("news")
    published("a", T0)
    assert store.get_mission("a").channels["github"].where == "https://octo.github.io/news/a/"
    assert "a/index.html" in signed_in.files("octo/news")
    feed = json.loads(signed_in.files("octo/news")["feed.json"])
    assert feed["items"][0]["url"].startswith("https://octo.github.io/news/")   # the feeds use the site address

def test_bluesky_links_to_the_article_on_github_pages(signed_in, monkeypatch):
    from test_bluesky import FakeBluesky
    sky = FakeBluesky()
    monkeypatch.setattr(bluesky, "TRANSPORT", httpx.MockTransport(sky))
    monkeypatch.setenv("BLUESKY_HANDLE", "loopdesk.bsky.social")
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "app-pass-test")
    github.create_site("news")
    published("a", T0)
    assert sky.posts[0]["embed"]["external"]["uri"] == "https://octo.github.io/news/a/"

def test_bluesky_waits_while_the_site_push_fails(signed_in, monkeypatch):
    github.create_site("news")
    monkeypatch.setenv("BLUESKY_HANDLE", "loopdesk.bsky.social")
    monkeypatch.setenv("BLUESKY_APP_PASSWORD", "app-pass-test")
    github._save(github.saved() | {"token": "gho_revoked"})
    published("a", T0)                                # the no-network Bluesky transport would fail the test
    assert "github" not in store.get_mission("a").channels and "bluesky" not in store.get_mission("a").channels
    with pytest.raises(NoAllowance, match="GitHub Pages"):
        bluesky.post(json.loads(Path("site/a/meta.json").read_text()), T0)

# ---------- the Settings page ----------

def test_settings_walks_through_connect_then_create(gh, client):
    html = client.get("/settings").text
    assert "Connect GitHub" in html
    r = client.post("/settings/github", data={"do": "connect"}, follow_redirects=False)
    assert r.status_code == 303
    html = client.get("/settings").text
    assert "WDJB-MJHT" in html and "https://github.com/login/device" in html
    assert client.post("/settings/github/poll").json() == {"state": "pending"}
    gh.approved = True
    assert client.post("/settings/github/poll").json() == {"state": "done"}
    html = client.get("/settings?done=github_connected").text
    assert "Connected as <strong>octo</strong>" in html and 'value="octo.github.io"' in html
    published("a", T0)
    r = client.post("/settings/github", data={"do": "create", "name": "octo.github.io"}, follow_redirects=False)
    assert r.headers["location"].endswith("done=github_linked")
    assert "a/index.html" in gh.files("octo/octo.github.io")                # pushed right away
    html = client.get("/settings").text
    assert "https://octo.github.io/" in html and "Use a different repo" in html
    client.post("/settings/github", data={"do": "unlink"})
    assert github.connected() and not github.enabled() and settings.site_url() == ""
    client.post("/settings/github", data={"do": "forget"})
    assert not github.CRED_FILE.exists()

def test_settings_links_a_pasted_repo_and_shows_refusals(signed_in, client):
    signed_in.add_repo("octo/app", {"main.py": b"x"})
    html = client.post("/settings/github", data={"do": "link", "repo": "https://github.com/octo/app"}).text
    assert "already holds other files" in html and not github.enabled()
    signed_in.add_repo("octo/news", {})
    r = client.post("/settings/github", data={"do": "link", "repo": "https://github.com/octo/news"},
                    follow_redirects=False)
    assert r.headers["location"].endswith("done=github_linked") and github.repo() == "octo/news"

def test_settings_asks_for_an_oauth_app_when_the_copy_has_none(client):
    html = client.get("/settings").text
    assert "Enable Device Flow" in html and "Connect GitHub" not in html
    client.post("/settings/github", data={"do": "client_id", "client_id": CLIENT_ID})
    assert "Connect GitHub" in client.get("/settings").text

def test_settings_refuses_cross_site_posts(signed_in, client):
    r = client.post("/settings/github", data={"do": "forget"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and github.connected()
