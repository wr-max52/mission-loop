"""This machine's configuration. Nothing machine-specific is written in the code: which models run, where Ollama
is, which 1F916 and Bluesky servers to use and where the site is hosted all come from config.

Each setting is looked up in this order:
1. data/settings.json, which the UI's Settings page writes (stays on this machine, never committed);
2. the environment, i.e. .env, which run.py loads first (also never committed; see .env.example);
3. for a service address only, the service's own public default (Ollama's standard local address, 1f916.ai,
   bsky.social), so a fresh clone can connect. Models have no default: until one is chosen on the Settings page
   (or set in .env), the loop does not start and says why."""
import json, os
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
    return saved().get("site_url") or SITE_URL

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
