"""This machine's configuration. Nothing machine-specific is written in the code: which models run, where Ollama
is, which 1F916 and Bluesky servers to use and where the site is hosted all come from config.

Each setting is looked up in this order:
1. data/settings.json, which the UI's Settings page writes (stays on this machine, never committed);
2. the environment, i.e. .env, which run.py loads first (also never committed; see .env.example);
3. for a service address only, the service's own public default (Ollama's standard local address, 1f916.ai,
   bsky.social), so a fresh clone can connect. Models have no default: until one is chosen on the Settings page
   (or set in .env), the loop does not start and says why."""
import json, os, re
from pathlib import Path
from urllib.parse import urlparse

# Outreach (milestone 4) emails points of contact and waits for their comments. Until it is built
# this stays off: research doesn't look for contacts and the validator doesn't check their emails.
OUTREACH = os.environ.get("MISSION_LOOP_OUTREACH") == "1"

SETTINGS_FILE = Path("data/settings.json")

# Where the published site lives on the web, e.g. https://example.com/news/. Optional: without it the feeds in
# site/ use links relative to the feed file, which work wherever the folder is served. The UI's saved value
# wins over MISSION_LOOP_SITE_URL in .env.
SITE_URL = os.environ.get("MISSION_LOOP_SITE_URL", "").strip()

def _env(name: str) -> str:
    return os.environ.get(name, "").strip()

def saved() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        return {}

def save(**values) -> None:
    """Set values in data/settings.json; an empty value removes the setting."""
    data = saved() | values
    data = {k: v for k, v in data.items() if v}
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, indent=2) + "\n")

def site_url() -> str:
    """The site address typed on the Settings page, then MISSION_LOOP_SITE_URL, then the GitHub Pages address of
    the repo linked there (agents/github.py); "" while the site is only on this machine."""
    if saved().get("site_url") or SITE_URL:
        return saved().get("site_url") or SITE_URL
    from agents import github             # late: agents import settings
    return github.site_address()

def _http_address(url: str) -> str:
    """The address without a trailing slash. Raises ValueError unless it is a plain http(s) address."""
    url = url.strip()
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or any(c.isspace() for c in url):
        raise ValueError("not an http(s) address")
    return url.rstrip("/")

# ---------- services: where Ollama, 1F916 and Bluesky are ----------

OLLAMA_DEFAULT = "http://127.0.0.1:11434"  # where the Ollama app listens unless told otherwise
F916_DEFAULT = "https://1f916.ai"
BLUESKY_DEFAULT = "https://bsky.social"
GITHUB_DEFAULT = "https://github.com"
# mission-loop's own GitHub OAuth app (device flow on, no secret needed). A client ID is public: it names the app
# asking, it grants nothing. One app serves every copy; a fork can register its own on the Settings page.
GITHUB_CLIENT_ID = ""

def ollama_host() -> str:
    """The LLM connection: settings.json, then OLLAMA_HOST, then Ollama's standard local address."""
    return saved().get("ollama_host") or _env("OLLAMA_HOST").rstrip("/") or OLLAMA_DEFAULT

def set_ollama_host(url: str) -> str:
    """Save the Ollama address ("" goes back to .env or the standard local address)."""
    url = _http_address(url) if url.strip() else ""
    save(ollama_host=url)
    return ollama_host()

def f916_url() -> str:
    """The 1F916 server to join: settings.json, then MISSION_LOOP_1F916_URL, then the public site. Once joined,
    the citizen's key file records its server and posts always go there (see agents/f916.api)."""
    return saved().get("f916_url") or _env("MISSION_LOOP_1F916_URL").rstrip("/") or F916_DEFAULT

def set_f916_url(url: str) -> str:
    url = _http_address(url) if url.strip() else ""
    save(f916_url="" if url == F916_DEFAULT else url)
    return f916_url()

def bluesky_service() -> str:
    """The Bluesky server for a login typed into .env (a login saved on the Settings page keeps its own)."""
    return _env("BLUESKY_SERVICE").rstrip("/") or BLUESKY_DEFAULT

def github_url() -> str:
    """The GitHub to sign in to and publish on: MISSION_LOOP_GITHUB_URL (a GitHub Enterprise server), else github.com."""
    return _env("MISSION_LOOP_GITHUB_URL").rstrip("/") or GITHUB_DEFAULT

def github_api(web: str = "") -> str:
    """The REST API of a GitHub server: api.github.com for github.com, <server>/api/v3 for an Enterprise one."""
    web = (web or github_url()).rstrip("/")
    return "https://api.github.com" if web == GITHUB_DEFAULT else web + "/api/v3"

def github_client_id() -> str:
    """The OAuth app that asks for the GitHub sign-in: settings.json, then MISSION_LOOP_GITHUB_CLIENT_ID, then
    mission-loop's own."""
    return saved().get("github_client_id") or _env("MISSION_LOOP_GITHUB_CLIENT_ID") or GITHUB_CLIENT_ID

def set_github_client_id(client_id: str) -> str:
    client_id = client_id.strip()
    if client_id and not re.fullmatch(r"[A-Za-z0-9._-]{6,64}", client_id):
        raise ValueError("not an OAuth app client ID")
    save(github_client_id="" if client_id == GITHUB_CLIENT_ID else client_id)
    return github_client_id()

def set_site_url(url: str) -> str:
    """Save the site address from the UI ("" clears it). Raises ValueError unless it is an http(s) address."""
    url = url.strip()
    if url:
        _http_address(url)
    save(site_url=url)
    return url

def animations() -> bool:
    """The little pixel researchers on the Missions page. On unless turned off on the Settings page."""
    return saved().get("animations") != "off"

def set_animations(on: bool) -> None:
    save(animations="" if on else "off")   # nothing stored while on, so a fresh install has them

THEMES = ("system", "light", "dark")

def theme() -> str:
    """The UI's colours: "system" follows the computer's light or dark appearance, else always light or dark."""
    return saved().get("theme") if saved().get("theme") in THEMES else "system"

def set_theme(value: str) -> None:
    if value not in THEMES:
        raise ValueError("not a theme")
    save(theme="" if value == "system" else value)   # nothing stored for the default

# The chat bubble and your messages in the chat panel. Empty means the app's own red ink.
CHAT_COLORS = {                                  # name -> pastel; a human may also pick any colour
    "Sage": "#b9cdb2", "Duck egg": "#b8d8d6", "Powder blue": "#bcd0ea", "Wisteria": "#cbc0e3",
    "Blush": "#eec5c5", "Apricot": "#f3d0b2", "Vellum": "#efe2b2",
}
HEX = re.compile(r"#[0-9a-f]{6}")

def chat_color() -> str:
    c = str(saved().get("chat_color") or "").lower()
    return c if HEX.fullmatch(c) else ""

def set_chat_color(value: str) -> str:
    """"" goes back to the default. Raises ValueError unless it is a #rrggbb colour."""
    value = value.strip().lower()
    if value and not HEX.fullmatch(value):
        raise ValueError("not a colour")
    save(chat_color=value)
    return value

def ink_on(color: str) -> str:
    """Dark or white text, whichever reads better on the colour (WCAG relative luminance)."""
    def channel(h: str) -> float:
        v = int(h, 16) / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(color[i:i + 2]) for i in (1, 3, 5))
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#1d1d1f" if (lum + 0.05) / 0.05 > 1.05 / (lum + 0.05) else "#ffffff"
LOOP_EVERY = 5                             # minutes a loop started from the UI rests when it has nothing to do

def loop_every() -> float:
    """How long a loop started from the UI's play button rests between looks for work, in minutes (Settings page)."""
    try:
        return max(1.0, float(saved().get("loop_every") or LOOP_EVERY))
    except (TypeError, ValueError):
        return float(LOOP_EVERY)

def set_loop_every(minutes: float) -> None:
    minutes = max(1.0, float(minutes))
    save(loop_every="" if minutes == LOOP_EVERY else minutes)   # nothing stored at the default

def auto_mode() -> bool:
    """Auto mode: the loop approves every brief and every proposed epic itself. Publishing still waits for a
    human. Off unless switched on in the UI's top bar (or `run.py auto on`)."""
    return saved().get("auto_mode") == "on"

def set_auto_mode(on: bool) -> None:
    save(auto_mode="on" if on else "")     # nothing stored while off, so a fresh install asks for every approval

# ---------- the two workers: which local model does which steps ----------
# The Research Assistant runs the small model's legwork (searching and reading pages); the Principal
# Investigator runs the big model's writing. Each can be turned off, and then the other one does its steps
# with its own model. With both on, the loop runs them at the same time whenever each has a task.
# Which model each uses is this machine's choice, never the code's: settings.json, then .env, else none.

WORKERS = {
    "assistant": "Research Assistant",
    "pi": "Principal Investigator",
}
MODEL_ENV = {"assistant": "MISSION_LOOP_SEARCH_MODEL", "pi": "MISSION_LOOP_MODEL"}

def other(worker: str) -> str:
    return "pi" if worker == "assistant" else "assistant"

def worker_on(worker: str) -> bool:
    return saved().get(f"{worker}_worker") != "off"

def worker_model(worker: str) -> str:
    """The model this worker is set to, whether or not it is on; "" until one is chosen."""
    return saved().get(f"{worker}_model") or _env(MODEL_ENV[worker])

def worker_for(role: str) -> str:
    """Who does the steps that belong to `role` ("assistant" or "pi"): that worker, or the other one while it is off."""
    return role if worker_on(role) else other(role)

def model_for(role: str) -> str:
    return worker_model(worker_for(role))

def chat_model() -> str:
    """The chat assistant's default model: settings.json, then MISSION_LOOP_CHAT_MODEL, then the Research
    Assistant's model, so Ollama reuses the copy the loop already loaded."""
    return saved().get("chat_model") or _env("MISSION_LOOP_CHAT_MODEL") or model_for("assistant")

def missing_models() -> list[str]:
    """Workers that are on but have no model chosen yet."""
    return [w for w in WORKERS if worker_on(w) and not worker_model(w)]

def parallel() -> bool:
    return all(worker_on(w) for w in WORKERS)

def set_worker(worker: str, model: str, on: bool) -> None:
    """Save one worker's model and switch. Raises ValueError for an unknown worker, a bad model name, no model
    for a worker that is on, or turning off the only worker still on."""
    if worker not in WORKERS:
        raise ValueError(f"no worker {worker!r}")
    model = model.strip()
    if len(model) > 200 or any(c.isspace() for c in model) or (on and not model):
        raise ValueError("pick a model")
    if not on and not worker_on(other(worker)):
        raise ValueError("one worker has to stay on")
    save(**{f"{worker}_model": model, f"{worker}_worker": "" if on else "off"})   # "on" is not stored

# ---------- custom publishing channels ----------
# Anything beyond the built-in channels: each one sends every published article somewhere, in a format you pick
# (agents/custom.py does the sending). Saved in data/settings.json as a list; nothing is built in.

CUSTOM_KINDS = {"webhook": "POST to a web address", "folder": "Write a file into a folder"}
CUSTOM_FORMATS = {"json": "JSON (a JSON Feed 1.1 item)", "markdown": "Markdown", "text": "Plain text"}

def custom_channels() -> list[dict]:
    """[{id, name, kind, target, format}], in the order they were added."""
    found = saved().get("custom_channels")
    return [c for c in found if isinstance(c, dict) and c.get("id")] if isinstance(found, list) else []

def add_custom_channel(name: str, kind: str, target: str, fmt: str) -> dict:
    """Add one. Raises ValueError for a missing name, an unknown kind or format, a webhook that isn't an http(s)
    address or a folder that isn't a full path."""
    name = " ".join(name.split())[:40]
    if not name:
        raise ValueError("a custom channel needs a name")
    if kind not in CUSTOM_KINDS or fmt not in CUSTOM_FORMATS:
        raise ValueError("pick a kind and a format")
    target = target.strip()
    if kind == "webhook":
        target = _http_address(target) if target else ""
    else:
        target = str(Path(target).expanduser()) if target else ""
        if not Path(target).is_absolute():
            raise ValueError("a folder must be a full path")
    if not target:
        raise ValueError("say where to send it")
    base = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")[:30] or "channel"
    taken = {c["id"] for c in custom_channels()}
    cid, n = base, 2
    while cid in taken:
        cid, n = f"{base}-{n}", n + 1
    channel = {"id": cid, "name": name, "kind": kind, "target": target, "format": fmt}
    save(custom_channels=custom_channels() + [channel])
    return channel

def remove_custom_channel(cid: str) -> None:
    save(custom_channels=[c for c in custom_channels() if c["id"] != cid])
