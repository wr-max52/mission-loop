"""Desk profiles (#122): the words that say what kind of desk this is, kept in files so a new kind of desk is one
file, not a code change. A profile is a Markdown file with a `## ` heading per block (BLOCKS); each block is a few
words the agents' prompts read. The checks never change with the profile: the source check, the article check,
"It is possible that", 3 supporting sites and the rating bar are in the code and the same in every profile.

Built-in profiles live in `profiles/` next to this file (investigative is the default and holds the words the
prompts always had). Custom ones live in `data/profiles/` on this machine; a block missing from a custom file uses
investigative's. The prompts read the blocks when they are built, so a switch applies at the next agent step."""
import re
from pathlib import Path
import settings

BUILT_IN = Path(__file__).parent / "profiles"
CUSTOM = Path("data/profiles")             # on this machine only, like the rest of data/
DEFAULT = settings.PROFILE_DEFAULT
BUILT_INS = ("investigative", "explainer", "literature-review", "market-scan")   # the picker's order
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")   # a file name: no dots, slashes or anything that leaves the folder

# Each block's key and its heading in the file. What each one is used for:
#   desk: what the desk is, as the finder and the Theorist hear it ("an independent investigative desk")
#   researcher: the researcher's opening lines, who it is and what its job is
#   primary_sources: the researcher's rule on which primary material to go beneath the coverage for
#   writer: the writer's opening line
#   rater: what the rater counts as a credible source (the score bar itself is in the code)
BLOCKS = {"desk": "Desk", "researcher": "Researcher", "primary_sources": "Primary sources", "writer": "Writer",
          "rater": "Rater"}

def _key(heading: str) -> str:
    return re.sub(r"[\s-]+", "_", heading.strip().lower())

def parse(text: str) -> tuple[str, dict[str, str]]:
    """The file's title (its `# ` line, if any) and its blocks by key: the lines under each `## ` heading, with the
    blank lines around them dropped. Anything before the first block is a description for people."""
    title, blocks, key, lines = "", {}, None, []
    def close():
        if key is not None:
            blocks[key] = "\n".join(lines).strip("\n")
    for line in text.splitlines():
        if line.startswith("## "):
            close()
            key, lines = _key(line[3:]), []
        elif key is None:
            if line.startswith("# ") and not title:
                title = line[2:].strip()
        else:
            lines.append(line.rstrip())
    close()
    return title, blocks

def path(name: str) -> Path | None:
    """Where a profile's file is: a built-in in profiles/, any other name in data/profiles/. None for a name that
    isn't a plain file name or has no file."""
    if not NAME.match(name or ""):
        return None
    p = (BUILT_IN if name in BUILT_INS else CUSTOM) / f"{name}.md"
    return p if p.is_file() else None

def custom_names() -> list[str]:
    """The profiles in data/profiles/, by name. A file named like a built-in is left out: the built-in wins."""
    if not CUSTOM.is_dir():
        return []
    return sorted(p.stem for p in CUSTOM.glob("*.md")
                  if p.is_file() and NAME.match(p.stem) and p.stem not in BUILT_INS)

def names() -> list[str]:
    return [n for n in BUILT_INS if path(n)] + custom_names()

def is_custom(name: str) -> bool:
    return name not in BUILT_INS

def current() -> str:
    """The profile in use: the one picked on Settings → Research, or investigative when none is picked or its file
    is gone."""
    name = settings.profile()
    return name if path(name) else DEFAULT

def _read(name: str) -> tuple[str, dict[str, str]]:
    p = path(name)
    try:
        return parse(p.read_text()) if p else ("", {})
    except (OSError, UnicodeDecodeError):
        return "", {}

def title(name: str) -> str:
    return _read(name)[0] or name

def description(name: str) -> str:
    """The first paragraph under the file's title, for the picker's hint; "" if it has none."""
    p = path(name)
    try:
        text = p.read_text() if p else ""
    except (OSError, UnicodeDecodeError):
        return ""
    para = []
    for line in text.splitlines():
        if line.startswith("## "):
            break
        if line.startswith("# ") and not para:
            continue
        if line.strip():
            para.append(line.strip())
        elif para:
            break
    return " ".join(para)

def missing(name: str) -> list[str]:
    """The blocks a profile doesn't set (absent or empty), which use investigative's words."""
    found = _read(name)[1]
    return [k for k in BLOCKS if not found.get(k, "").strip()]

def unknown(name: str) -> list[str]:
    """Headings in a profile that are no block, as written: a typo there means that block falls back."""
    return [k for k in _read(name)[1] if k not in BLOCKS]

def blocks(name: str | None = None) -> dict[str, str]:
    """Every block's words for a profile (the one in use by default), investigative's where it has none."""
    base = _read(DEFAULT)[1]
    own = _read(name or current())[1]
    out = {k: own.get(k, "").strip("\n") if own.get(k, "").strip() else base.get(k, "") for k in BLOCKS}
    out["desk"] = " ".join(out["desk"].split())    # read inside a sentence: one line
    return out

def block(key: str) -> str:
    return blocks()[key]

def indented(key: str, by: str = "  ") -> str:
    """A block that continues a bullet or a labelled line in a prompt: every line after the first indented, so a
    profile file can be written without the prompt's indentation."""
    return block(key).replace("\n", "\n" + by)

def copy(name: str) -> tuple[str, Path]:
    """Copy a profile's file into data/profiles/ to be edited, as my-<name> (my-<name>-2 and so on if taken).
    Returns the new profile's name and its file."""
    src = path(name)
    if not src:
        raise ValueError(f"no profile named {name!r}")
    base = name if name.startswith("my-") else f"my-{name}"
    base = base[:36].rstrip("-")
    new, n = base, 2
    while (CUSTOM / f"{new}.md").exists() or new in BUILT_INS:
        new, n = f"{base}-{n}", n + 1
    CUSTOM.mkdir(parents=True, exist_ok=True)
    dest = CUSTOM / f"{new}.md"
    dest.write_text(src.read_text())
    return new, dest
