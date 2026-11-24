"""Backup and restore (#120): one zip of everything this machine can't get back, kept private, and a restore that
moves the current desk aside before it unpacks. Every test works in its own tmp folders, never the real data/."""
import fcntl, json, os, sqlite3, stat, zipfile
from datetime import timedelta
from pathlib import Path
import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner
import backup, clock, doctor, orchestrator, run as cli, settings, store, trail, ui
from agents import f916
from contracts import Mission

SECRET = "sk-test-not-a-real-secret-1234"

@pytest.fixture
def home(tmp_path_factory, monkeypatch):
    """A pretend home folder, so the default backup folder is never the real one."""
    folder = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(folder))
    return folder

@pytest.fixture
def out(tmp_path_factory, home):
    """A folder outside the desk for the zips."""
    return tmp_path_factory.mktemp("backups")

@pytest.fixture
def desk():
    """A small desk: two missions with files, missions.yaml, a published page, a 1F916 key, settings, plus what a
    backup leaves out (the library index, the loop log, a trail, the loop's flags)."""
    for slug in ("grid", "water"):
        store.save_mission(Mission(slug=slug, topic=f"The {slug} question"))
        Path("missions", slug).mkdir(parents=True)
        Path("missions", slug, "brief.md").write_text(f"# {slug} brief\n")
    Path("missions.yaml").write_text("- slug: grid\n  topic: The grid question\n")
    Path("site").mkdir()
    Path("site/index.html").write_text("<h1>Published</h1>")
    fd = os.open(f916.KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"handle": "desk", "secret": SECRET}, f)
    settings.save(publication_name="Test Desk")
    with sqlite3.connect("data/library.db") as c:                # the library's index: rebuilt, not backed up
        c.execute("CREATE TABLE passages (id TEXT PRIMARY KEY, slug TEXT, kind TEXT)")
    Path("data/loop.log").write_text("log line\n")
    trail.DIR.mkdir(parents=True, exist_ok=True)
    (trail.DIR / "grid.jsonl").write_text("{}\n")
    Path("data/WAKE").touch()

def names(zip_path) -> set[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return set(zf.namelist())

# ---------- back up ----------

def test_backup_holds_the_desk_and_leaves_out_what_can_be_rebuilt(desk, out):
    done = backup.make(out)
    path = Path(done["path"])
    assert path.parent == out.resolve() and path.name == f"mission-loop-backup-{clock.now():%Y-%m-%d}.zip"
    held = names(path)
    for name in ("data/loop.db", "data/1f916.json", "data/settings.json", "missions/grid/brief.md",
                 "missions/water/brief.md", "missions.yaml", "site/index.html", backup.MANIFEST):
        assert name in held, name
    for name in ("data/library.db", "data/loop.log", "data/trail/grid.jsonl", "data/WAKE", "data/loop.lock"):
        assert name not in held, name
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(out.glob(".*.part"))

def test_the_database_copy_is_a_working_database(desk, out, tmp_path_factory):
    path = backup.make(out)["path"]
    unpacked = tmp_path_factory.mktemp("look")
    with zipfile.ZipFile(path) as zf:
        zf.extract("data/loop.db", unpacked)
    with sqlite3.connect(unpacked / "data" / "loop.db") as c:
        assert {r[0] for r in c.execute("SELECT slug FROM missions")} == {"grid", "water"}

def test_the_command_says_it_holds_keys_and_never_prints_one(desk, out):
    result = CliRunner().invoke(cli.app, ["backup", "--to", str(out)])
    assert result.exit_code == 0, result.output
    assert "keys" in result.output and "private" in result.output
    assert SECRET not in result.output
    assert backup.last()["path"] in result.output

def test_a_second_backup_the_same_day_never_overwrites_the_first(desk, out):
    first, second = backup.make(out)["path"], backup.make(out)["path"]
    assert first != second and Path(first).exists() and second.endswith("-2.zip")

def test_never_inside_the_mission_loop_folder(desk, out):
    for inside in (Path.cwd(), Path.cwd() / "backups", backup.CODE, backup.CODE / "data", "relative/folder"):
        with pytest.raises(ValueError, match="inside the Mission Loop folder"):
            backup.make(inside)
    assert not list(Path.cwd().rglob("mission-loop-backup-*.zip"))
    result = CliRunner().invoke(cli.app, ["backup", "--to", str(Path.cwd() / "x")])
    assert result.exit_code == 1 and "inside the Mission Loop folder" in result.output
    assert not backup.last()

def test_the_default_folder_is_documents_then_home(home):
    assert settings.backup_dir() == str(home)
    (home / "Documents").mkdir()
    assert settings.backup_dir() == str(home / "Documents")
    assert settings.set_backup_dir("~/elsewhere") == str(home / "elsewhere")
    with pytest.raises(ValueError):
        settings.set_backup_dir("not/full")
    assert settings.set_backup_dir("") == str(home / "Documents")

def test_backup_with_no_folder_uses_the_setting(desk, out):
    settings.set_backup_dir(str(out))
    assert Path(backup.make()["path"]).parent == out.resolve()
    assert backup.last()["path"].startswith(str(out.resolve()))

# ---------- restore ----------

def test_restore_brings_back_a_deleted_mission_and_the_key(desk, out):
    """The issue's test: back up a stub desk, delete a mission, restore."""
    zip_path = backup.make(out)["path"]
    with store.conn() as c:
        c.execute("DELETE FROM missions WHERE slug = 'grid'")
    (Path("missions/grid/brief.md")).unlink()
    Path("missions/grid").rmdir()
    f916.KEY_FILE.unlink()
    assert not store.get_mission("grid")

    result = CliRunner().invoke(cli.app, ["restore", zip_path])
    assert result.exit_code == 0, result.output
    assert SECRET not in result.output
    assert store.get_mission("grid") and store.get_mission("water")
    assert Path("missions/grid/brief.md").read_text() == "# grid brief\n"
    assert json.loads(f916.KEY_FILE.read_text())["secret"] == SECRET
    assert stat.S_IMODE(f916.KEY_FILE.stat().st_mode) == 0o600
    aside = Path(f"data.before-restore-{clock.now():%Y-%m-%d}")
    assert aside.is_dir() and str(aside) in result.output
    assert (aside / "data" / "loop.db").exists() and (aside / "missions" / "water" / "brief.md").exists()
    assert (aside / "missions.yaml").exists() and (aside / "site" / "index.html").exists()
    assert not (aside / "data" / "1f916.json").exists()      # it was deleted before the restore
    assert not list(Path.cwd().glob(".restore-*"))

def test_restore_keeps_this_machines_backup_record(desk, out):
    zip_path = backup.make(out)["path"]
    later = backup.make(out)
    backup.restore(zip_path)
    assert backup.last()["path"] == later["path"]

def test_restore_refuses_while_the_loop_runs(desk, out):
    zip_path = backup.make(out)["path"]
    store.save_mission(Mission(slug="new", topic="Made after the backup"))
    orchestrator.LOCK_FILE.parent.mkdir(exist_ok=True)
    with orchestrator.LOCK_FILE.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = CliRunner().invoke(cli.app, ["restore", zip_path])
    assert result.exit_code == 1 and "loop is running" in result.output
    assert store.get_mission("new") and not list(Path.cwd().glob("data.before-restore-*"))

def bad_zip(path: Path, entries: dict[str, str], manifest: bool = True) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        if manifest:
            zf.writestr(backup.MANIFEST, "{}")
        for name, text in entries.items():
            zf.writestr(name, text)
    return path

@pytest.mark.parametrize("entries, manifest, why", [
    ({"data/loop.db": "x"}, False, "not a Mission Loop backup"),
    ({"/etc/evil": "x"}, True, "unsafe"),
    ({"data/../../evil": "x"}, True, "unsafe"),
    ({"../evil": "x"}, True, "unsafe"),
    ({"C:/evil": "x"}, True, "unsafe"),
    ({"run.py": "print('replaced')"}, True, "not part of a desk"),
    ({"tests/evil.py": "x"}, True, "not part of a desk"),
])
def test_restore_refuses_an_unsafe_zip_before_touching_anything(desk, out, entries, manifest, why):
    zip_path = bad_zip(out / "bad.zip", entries, manifest)
    with pytest.raises(ValueError, match=why):
        backup.restore(zip_path)
    assert store.get_mission("grid") and f916.KEY_FILE.exists()
    assert not list(Path.cwd().glob("data.before-restore-*")) and not list(Path.cwd().glob(".restore-*"))
    assert not (Path.cwd().parent / "evil").exists()

def test_restore_refuses_a_link_in_the_zip(desk, out):
    path = out / "link.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(backup.MANIFEST, "{}")
        info = zipfile.ZipInfo("data/1f916.json")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(ValueError, match="link"):
        backup.restore(path)
    assert json.loads(f916.KEY_FILE.read_text())["secret"] == SECRET

def test_a_zip_that_is_not_a_zip_is_refused(desk, out):
    (out / "junk.zip").write_text("not a zip")
    result = CliRunner().invoke(cli.app, ["restore", str(out / "junk.zip")])
    assert result.exit_code == 1 and "restore refused" in result.output
    assert not list(Path.cwd().glob("data.before-restore-*"))

def test_the_desk_list_is_one_place():
    assert [p.as_posix() for p in backup.desk()] == ["data", "missions", "missions.yaml", "site"]
    assert backup.LOG == ui.LOG

# ---------- Settings → Setup ----------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(orchestrator, "LOCK_FILE", Path("data/loop.lock"))
    monkeypatch.setattr(orchestrator, "WAKE_FLAG", Path("data/WAKE"))
    return TestClient(ui.app)

def test_back_up_now_on_settings_setup(desk, out, client):
    settings.set_backup_dir(str(out))
    page = client.get("/settings").text
    assert "Back up now" in page and "No backup yet." in page and str(out) in page
    r = client.post("/settings/backup", data={"do": "now"}, follow_redirects=False)
    assert r.status_code == 303 and "done=backup_saved" in r.headers["location"]
    made = backup.last()
    assert Path(made["path"]).exists() and Path(made["path"]).parent == out.resolve()
    page = client.get("/settings?done=backup_saved").text
    assert made["path"] in page and "Last backup:" in page and "keep it private" in page
    assert SECRET not in page

def test_the_backup_folder_is_saved_on_the_page_but_never_inside_this_folder(out, client):
    r = client.post("/settings/backup", data={"do": "folder", "folder": str(out)}, follow_redirects=False)
    assert "done=backup_folder" in r.headers["location"] and settings.backup_dir() == str(out)
    for inside in (str(Path.cwd() / "zips"), "relative"):
        r = client.post("/settings/backup", data={"do": "folder", "folder": inside}, follow_redirects=False)
        assert "error=backup_folder" in r.headers["location"]
    assert settings.backup_dir() == str(out)

def test_back_up_now_with_a_folder_that_cant_be_written(desk, out, client):
    blocked = out / "file"
    blocked.write_text("a file, not a folder")
    settings.set_backup_dir(str(blocked))
    r = client.post("/settings/backup", data={"do": "now"}, follow_redirects=False)
    assert "error=backup_failed" in r.headers["location"]

def test_backup_from_another_site_is_refused(client):
    r = client.post("/settings/backup", data={"do": "now"}, headers={"origin": "http://evil.example"})
    assert r.status_code == 403

# ---------- the setup check (could have) ----------

def test_the_setup_check_warns_about_a_key_with_no_recent_backup(out, monkeypatch):
    assert not [c for c in doctor.checks() if c.key == "backup"]          # no 1F916 key: nothing to say
    f916.KEY_FILE.parent.mkdir(exist_ok=True)
    f916.KEY_FILE.write_text(json.dumps({"handle": "desk", "secret": SECRET}))
    check = next(c for c in doctor.checks() if c.key == "backup")
    assert not check.ok and not check.blocks_loop and "no recovery" in check.detail
    backup.make(out)
    assert next(c for c in doctor.checks() if c.key == "backup").ok
    later = clock.now() + timedelta(days=31)
    monkeypatch.setattr(clock, "now", lambda: later)
    check = next(c for c in doctor.checks() if c.key == "backup")
    assert not check.ok and "31 days" in check.detail and not check.blocks_loop
    assert SECRET not in doctor.report(doctor.checks())
