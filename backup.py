"""Backup and restore (#120): one zip that holds everything this machine can't get back.

A desk is its database, settings and keys (`data/`, where `data/1f916.json` has no recovery), the missions' files
(`missions/`), `missions.yaml` and the published site (`site/`). `desk()` is that list, in one place, so an export
or a move to another machine can reuse it. What can be rebuilt stays out: the library's index (`library.DB`), the
loop log and the agents' trails (`trail.DIR`), and the running loop's flags and locks.

`make()` writes `mission-loop-backup-<date>.zip`, mode 600, into `settings.backup_dir()` (or a folder given), never
inside the Mission Loop folder, and records when and where in `data/backup.json`. The database is copied with
SQLite's own backup, so a loop that is running doesn't leave a half-written copy. `restore()` refuses while the loop
runs, checks every name in the zip first (no absolute paths, no "..", no links, only the desk's own parts), unpacks
into a staging folder, then moves the current desk aside into `data.before-restore-<date>/` (its `data/`,
`missions/`, `missions.yaml` and `site/` inside it) and puts the backup's in their place.

Nothing here prints or logs a key: only names and counts.
"""
import json, os, shutil, sqlite3, stat, tempfile, zipfile
from datetime import datetime
from pathlib import Path
import clock, library, orchestrator, settings, store, trail
from agents import publisher

CODE = Path(__file__).resolve().parent     # the Mission Loop folder (the repo)
MANIFEST = "mission-loop-backup.json"      # inside every zip: when it was made and what it holds
RECORD = "backup.json"                     # in data/: this machine's last backup (time and path); not backed up
LOG = Path("data/loop.log")                # the loop log (ui.LOG): rebuilt by the next run
ASIDE = "data.before-restore-"             # the folder a restore moves the current desk into
OLD_AFTER_DAYS = 30                        # the setup check warns when a 1F916 key's last backup is older

def data_dir() -> Path:
    return store.DB.parent

def desk() -> list[Path]:
    """What makes up a desk, relative to the Mission Loop folder: the parts a backup holds and a restore replaces."""
    return [_rel(data_dir()), _rel(Path("missions")), _rel(Path("missions.yaml")), _rel(publisher.SITE)]

def left_out() -> list[Path]:
    """What a backup leaves out: what can be rebuilt, the running loop's flags and locks, and this machine's own
    backup record."""
    return [_rel(p) for p in (library.DB, LOG, trail.DIR, orchestrator.STOP_FLAG, orchestrator.WAKE_FLAG,
                              orchestrator.THINK_FLAG, orchestrator.LOCK_FILE, orchestrator.PUBLISH_LOCK,
                              data_dir() / "activity.json", data_dir() / "waiting.json", data_dir() / RECORD)]

def _root() -> Path:
    return Path.cwd().resolve()

def _rel(p: Path) -> Path:
    """A path relative to the folder the loop runs in (its paths are written relative to it)."""
    p = Path(p)
    return Path(os.path.relpath(p.resolve() if p.is_absolute() else _root() / p, _root()))

def _skip(rel: Path) -> bool:
    name = rel.name
    if name.endswith((".tmp", ".part", "-wal", "-shm", "-journal")):
        return True
    return any(rel == out or out in rel.parents for out in left_out())

def check_folder(folder: str | Path) -> Path:
    """The folder a backup goes into, made absolute. Raises ValueError for one inside the Mission Loop folder (the
    repo, or the folder the loop runs in), where a zip of keys could be committed."""
    path = Path(folder).expanduser()
    path = (path if path.is_absolute() else _root() / path).resolve()
    for inside in {CODE, _root()}:
        if path == inside or inside in path.parents:
            raise ValueError(f"{path} is inside the Mission Loop folder; pick a folder outside it")
    return path

def _files() -> list[tuple[Path, Path]]:
    """(file on disk, its name in the zip) for every file the backup holds, in a steady order. Links are skipped."""
    out = []
    for part in desk():
        src = _root() / part
        if src.is_symlink() or not src.exists():
            continue
        if src.is_file():
            out.append((src, part))
            continue
        for folder, dirs, names in os.walk(src):
            dirs[:] = sorted(d for d in dirs if not (Path(folder) / d).is_symlink()
                             and not _skip(_rel(Path(folder) / d)))
            for name in sorted(names):
                path = Path(folder) / name
                rel = _rel(path)
                if not path.is_symlink() and path.is_file() and not _skip(rel):
                    out.append((path, rel))
    return out

def _free(folder: Path, stem: str, suffix: str = "") -> Path:
    """folder/stem+suffix, or stem-2, stem-3 ... when that is taken, so nothing is overwritten."""
    path, n = folder / f"{stem}{suffix}", 2
    while path.exists():
        path, n = folder / f"{stem}-{n}{suffix}", n + 1
    return path

def make(to: str | Path | None = None) -> dict:
    """Write the backup zip. Returns {"path", "files", "bytes", "at"}. Raises ValueError for a folder inside the
    Mission Loop folder, OSError when it can't be written."""
    folder = check_folder(to or settings.backup_dir())
    folder.mkdir(parents=True, exist_ok=True)
    now = clock.now()
    final = _free(folder, f"mission-loop-backup-{now:%Y-%m-%d}", ".zip")
    part = folder / f".{final.name}.part"
    part.unlink(missing_ok=True)
    files = _files()
    db = _rel(store.DB)
    try:
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as raw, zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as zf, \
                tempfile.TemporaryDirectory() as tmp:
            manifest = {"made": now.isoformat(), "desk": [p.as_posix() for p in desk()], "files": len(files)}
            zf.writestr(MANIFEST, json.dumps(manifest, indent=2) + "\n")
            for path, rel in files:
                src = path
                if rel == db:              # a consistent copy, even while the loop writes
                    src = Path(tmp) / rel.name
                    live, copy = sqlite3.connect(path), sqlite3.connect(src)
                    try:
                        live.backup(copy)
                    finally:
                        live.close()
                        copy.close()
                info = zipfile.ZipInfo.from_file(path, rel.as_posix())
                info.compress_type = zipfile.ZIP_DEFLATED
                with src.open("rb") as f, zf.open(info, "w") as out:
                    shutil.copyfileobj(f, out)
        os.replace(part, final)
        final.chmod(0o600)
    finally:
        part.unlink(missing_ok=True)
    done = {"path": str(final), "files": len(files), "bytes": final.stat().st_size, "at": now.isoformat()}
    record = data_dir() / RECORD
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"at": done["at"], "path": done["path"]}) + "\n")
    return done

def last() -> dict:
    """{"at", "path"} of this machine's last backup, or {} for none."""
    try:
        data = json.loads((data_dir() / RECORD).read_text())
        return data if isinstance(data, dict) and data.get("at") else {}
    except (OSError, ValueError):
        return {}

def age_days() -> float | None:
    """Days since the last backup, or None for none."""
    at = last().get("at")
    try:
        return (clock.now() - datetime.fromisoformat(at)).total_seconds() / 86400 if at else None
    except (TypeError, ValueError):
        return None

# ---------- restore ----------

def _members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """The zip's entries, each checked before anything is unpacked. Raises ValueError for a zip that isn't a Mission
    Loop backup or holds a name that could land outside the desk."""
    names = zf.namelist()
    if MANIFEST not in names:
        raise ValueError("not a Mission Loop backup (no manifest)")
    allowed = {p.as_posix() for p in desk()}
    out = []
    for info in zf.infolist():
        name = info.filename
        if name == MANIFEST:
            continue
        parts = name.replace("\\", "/").split("/")
        if (name.startswith(("/", "\\")) or "\\" in name or (len(parts[0]) > 1 and parts[0][1] == ":")
                or ".." in parts or "" in parts[:-1] or "." in parts):
            raise ValueError(f"unsafe name in the backup: {name!r}")
        top = parts[0] if len(parts) > 1 or info.is_dir() else name
        if top not in allowed and name not in allowed:
            raise ValueError(f"not part of a desk: {name!r}")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise ValueError(f"the backup holds a link: {name!r}")
        out.append(info)
    return out

def restore(zip_path: str | Path) -> dict:
    """Put a backup back. Returns {"aside", "files"}: where the desk that was here went, and how many files came
    back. Raises RuntimeError while the loop runs, ValueError for a zip that isn't a safe Mission Loop backup."""
    if orchestrator.loop_running():
        raise RuntimeError("the loop is running; stop it first (python run.py stop)")
    root = _root()
    zip_path = Path(zip_path).expanduser().resolve()
    staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=root))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = _members(zf)
            for info in members:
                target = staging / info.filename
                if staging.resolve() not in target.resolve().parents:
                    raise ValueError(f"unsafe name in the backup: {info.filename!r}")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                mode = (info.external_attr >> 16) & 0o777 or 0o600
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as out, zf.open(info) as src:
                    shutil.copyfileobj(src, out)
                os.chmod(target, mode)
        aside = _free(root, f"{ASIDE}{clock.now():%Y-%m-%d}")
        aside.mkdir(mode=0o700)
        moved, placed = [], []
        try:
            for part in desk():
                if (root / part).exists() or (root / part).is_symlink():
                    (aside / part).parent.mkdir(parents=True, exist_ok=True)
                    os.replace(root / part, aside / part)
                    moved.append(part)
            for part in desk():
                if (staging / part).exists():
                    (root / part).parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staging / part, root / part)
                    placed.append(part)
        except OSError:                    # put the desk that was here back as it was
            for part in placed:
                if (root / part).is_dir():
                    shutil.rmtree(root / part, ignore_errors=True)
                else:
                    (root / part).unlink(missing_ok=True)
            for part in moved:
                os.replace(aside / part, root / part)
            try:
                aside.rmdir()              # only when everything went back; never deletes what it holds
            except OSError:
                pass
            raise
        kept = aside / _rel(data_dir()) / RECORD        # this machine's backup record stays this machine's
        if kept.exists():
            (root / _rel(data_dir())).mkdir(parents=True, exist_ok=True)
            shutil.copy2(kept, root / _rel(data_dir()) / RECORD)
        return {"aside": str(aside), "files": sum(not i.is_dir() for i in members)}
    finally:
        shutil.rmtree(staging, ignore_errors=True)
