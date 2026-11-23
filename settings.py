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
BLUESKY_APPVIEW = "https://public.api.bsky.app"   # Bluesky's public read API: people search and reply threads, no login
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

# ---------- model providers: Ollama, and cloud APIs plugged in with a key ----------
# A worker's model is a bare name for Ollama ("qwen3:30b", exactly as before), or "<provider>/<model>" for any
# other provider ("anthropic/claude-sonnet-5-5", "openrouter/meta-llama/llama-4"). A provider is on once it has a
# key: pasted on the Settings page (saved to data/providers.json, mode 600, never committed) or in .env. Each one
# here is only its public API address and where its keys come from; no model names.
#   api: "ollama", "anthropic" (Messages API) or "openai" (the OpenAI chat completions shape, which most speak)
#   schema: False where the provider can't hold a reply to a JSON schema (it gets JSON mode and the schema in words)
#   tokens: the request field for the reply cap; temperature: False where reasoning models refuse to be set
#   embed: False where the provider has no embeddings API, so the library can't use it (library.py)
PROVIDERS = {
    "ollama": {"name": "Ollama", "api": "ollama", "url": OLLAMA_DEFAULT, "key_env": "",
               "keys": "https://ollama.com/download", "blurb": "Free, on this machine. Pull models with ollama pull."},
    "anthropic": {"name": "Anthropic", "api": "anthropic", "url": "https://api.anthropic.com",
                  "key_env": "ANTHROPIC_API_KEY", "keys": "https://console.anthropic.com/settings/keys",
                  "blurb": "Claude models.", "embed": False},
    "openai": {"name": "OpenAI", "api": "openai", "url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY",
               "keys": "https://platform.openai.com/api-keys", "blurb": "GPT models.",
               "tokens": "max_completion_tokens", "temperature": False},
    "xai": {"name": "xAI", "api": "openai", "url": "https://api.x.ai/v1", "key_env": "XAI_API_KEY",
            "keys": "https://console.x.ai", "blurb": "Grok models."},
    "deepseek": {"name": "DeepSeek", "api": "openai", "url": "https://api.deepseek.com", "key_env": "DEEPSEEK_API_KEY",
                 "keys": "https://platform.deepseek.com/api_keys", "blurb": "DeepSeek chat and reasoner.",
                 "schema": False, "embed": False},
    "gemini": {"name": "Google Gemini", "api": "openai", "url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "key_env": "GEMINI_API_KEY", "keys": "https://aistudio.google.com/apikey", "blurb": "Gemini models."},
    "mistral": {"name": "Mistral", "api": "openai", "url": "https://api.mistral.ai/v1", "key_env": "MISTRAL_API_KEY",
                "keys": "https://console.mistral.ai/api-keys", "blurb": "Mistral models."},
    "groq": {"name": "Groq", "api": "openai", "url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY",
             "keys": "https://console.groq.com/keys", "blurb": "Open models, served very fast.", "embed": False},
    "openrouter": {"name": "OpenRouter", "api": "openai", "url": "https://openrouter.ai/api/v1",
                   "key_env": "OPENROUTER_API_KEY", "keys": "https://openrouter.ai/keys",
                   "blurb": "Hundreds of models from every lab behind one key."},
    "together": {"name": "Together AI", "api": "openai", "url": "https://api.together.xyz/v1",
                 "key_env": "TOGETHER_API_KEY", "keys": "https://api.together.ai/settings/api-keys",
                 "blurb": "Open models in the cloud."},
    "custom": {"name": "OpenAI-compatible server", "api": "openai", "url": "", "key_env": "MISSION_LOOP_COMPAT_KEY",
               "url_env": "MISSION_LOOP_COMPAT_URL", "keys": "", "optional_key": True,
               "blurb": "Any OpenAI-compatible server: LM Studio, vLLM, llama.cpp, LiteLLM."},
}
LOCAL = "ollama"
PROVIDERS_FILE = Path("data/providers.json")

def split_model(ref: str) -> tuple[str, str]:
    """"anthropic/claude-x" -> ("anthropic", "claude-x"); a bare name, or one whose first part is no provider
    (Ollama's own "user/model" or "hf.co/..."), is an Ollama model."""
    head, sep, rest = (ref or "").partition("/")
    if sep and rest and head in PROVIDERS:
        return head, rest
    return LOCAL, ref or ""

def model_ref(provider: str, name: str) -> str:
    return name if provider == LOCAL else f"{provider}/{name}"

def _provider_file() -> dict:
    try:
        data = json.loads(PROVIDERS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}

def provider_key(pid: str) -> str:
    """The provider's API key: the one saved on the Settings page, then its variable in .env; "" for none."""
    saved_key = str((_provider_file().get(pid) or {}).get("key") or "")
    env = PROVIDERS.get(pid, {}).get("key_env") or ""
    return saved_key or (_env(env) if env else "")

def provider_url(pid: str) -> str:
    """Where the provider's API is: saved on the Settings page, then .env (a custom server), then its public address."""
    if pid == LOCAL:
        return ollama_host()
    spec = PROVIDERS.get(pid, {})
    found = str((_provider_file().get(pid) or {}).get("url") or "")
    return (found or (_env(spec["url_env"]) if spec.get("url_env") else "") or spec.get("url", "")).rstrip("/")

def provider_saved(pid: str) -> bool:
    """Set up on the Settings page (so it can be disconnected there), not only in .env."""
    return pid in _provider_file()

def provider_on(pid: str) -> bool:
    """Can its models be used? Ollama always can be tried; a cloud provider needs a key (a custom server an address)."""
    if pid == LOCAL:
        return True
    if pid not in PROVIDERS:
        return False
    if PROVIDERS[pid].get("optional_key"):
        return bool(provider_url(pid))
    return bool(provider_key(pid) and provider_url(pid))

def cloud_providers() -> list[str]:
    """Every provider other than Ollama that is on, in the order above."""
    return [p for p in PROVIDERS if p != LOCAL and provider_on(p)]

def save_provider(pid: str, key: str = "", url: str = "") -> None:
    """Keep a provider's key (and a custom server's address) in data/providers.json, readable by this user only.
    Raises ValueError for an unknown provider, a key with spaces in it, a missing key or a bad address."""
    if pid not in PROVIDERS or pid == LOCAL:
        raise ValueError(f"no provider {pid!r}")
    key, url = key.strip(), url.strip()
    if len(key) > 500 or any(c.isspace() for c in key):
        raise ValueError("that doesn't look like an API key")
    if not key and not PROVIDERS[pid].get("optional_key"):
        raise ValueError("paste the API key")
    url = _http_address(url) if url else ""
    if not PROVIDERS[pid]["url"] and not url:   # a custom server has no public address to fall back on
        raise ValueError("say where the server is")
    data = _provider_file() | {pid: {k: v for k, v in (("key", key), ("url", url)) if v}}
    _write_private(PROVIDERS_FILE, data)

def forget_provider(pid: str) -> None:
    data = _provider_file()
    if data.pop(pid, None) is not None:
        _write_private(PROVIDERS_FILE, data)

def _write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.touch(mode=0o600)
    tmp.chmod(0o600)
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)

# ---------- the library: retrieval over the desk's own work, for the Theorist and the chat (library.py) ----------
# It always ranks by words; an embedding model adds ranking by meaning. Like every model, which one is this
# machine's choice: "" (words only), an Ollama model ("nomic-embed-text") or "<provider>/<model>" for a connected
# provider with an embeddings API. How many passages a search returns is a pick from TOP_N_CHOICES.

EMBED_ENV = "MISSION_LOOP_EMBED_MODEL"
TOP_N_CHOICES = (3, 5, 8, 10, 15, 20)
TOP_N = 5

def can_embed(pid: str) -> bool:
    return pid in PROVIDERS and PROVIDERS[pid].get("embed", True)

def embed_providers() -> list[str]:
    """Providers whose embedding models the library can use now: Ollama, then each connected one with an embeddings API."""
    return [LOCAL] + [p for p in cloud_providers() if can_embed(p)]

WORDS_ONLY = "-"   # saved as embed_model: words only, even while .env names a model

def embed_model() -> str:
    """The library's embedding model: settings.json, then MISSION_LOOP_EMBED_MODEL; "" ranks by words only."""
    got = saved().get("embed_model")
    if got == WORDS_ONLY:
        return ""
    return got or _env(EMBED_ENV)

def set_embed_model(ref: str) -> str:
    """Save the embedding model: a model name, "" to go back to .env, or WORDS_ONLY to rank by words only (stored
    only while .env names a model; otherwise nothing is stored). A name equal to .env's is not stored either, so a
    later .env edit still applies. Raises ValueError for a bad name or a provider without embeddings."""
    ref = ref.strip()
    if len(ref) > 200 or any(c.isspace() for c in ref):
        raise ValueError("that isn't a model name")
    env = _env(EMBED_ENV)
    if ref == WORDS_ONLY:
        ref = WORDS_ONLY if env else ""
    elif ref and not can_embed(split_model(ref)[0]):
        raise ValueError(f"{PROVIDERS[split_model(ref)[0]]['name']} offers no embedding models")
    elif ref == env:
        ref = ""
    save(embed_model=ref)
    return embed_model()

def library_top_n() -> int:
    """How many passages a library search returns (Settings page)."""
    try:
        n = int(saved().get("library_top_n") or TOP_N)
    except (TypeError, ValueError):
        return TOP_N
    return n if n in TOP_N_CHOICES else TOP_N

def set_library_top_n(n: int) -> None:
    if n not in TOP_N_CHOICES:
        raise ValueError(f"pick one of {', '.join(map(str, TOP_N_CHOICES))}")
    save(library_top_n="" if n == TOP_N else n)   # nothing stored at the default

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

# The UI's language (i18n.py). English is the default and stores nothing; the published site stays English.
LANGUAGES = {"en": "English", "zh": "中文", "fr": "Français", "es": "Español", "pt": "Português", "de": "Deutsch"}
LANGUAGE_NAMES = {"en": "English", "zh": "Chinese", "fr": "French", "es": "Spanish", "pt": "Portuguese",
                  "de": "German"}                   # in English, for the chat model's prompt

def language() -> str:
    return saved().get("language") if saved().get("language") in LANGUAGES else "en"

def set_language(code: str) -> None:
    if code not in LANGUAGES:
        raise ValueError("not a language")
    save(language="" if code == "en" else code)

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

# The cork board's look (board.py, ui.board_page): the surface, the cards, and what joins them. Saved only when
# not the default (cork, sticky notes, red string), like the theme.
BOARD_SURFACES = {"cork": "Cork", "paper": "Paper", "grid": "Grid", "dark": "Night", "fridge": "Fridge", "arcade": "Arcade"}
BOARD_CARDS = {"sticky": "Sticky notes", "magnets": "Fridge magnets", "hologram": "Hologram", "pixel": "Pixel"}
BOARD_STRINGS = {                                # kind -> its colours (a human may also pick any colour)
    "string": {"Red": "#c0242b", "Black": "#2b2620", "Blue": "#2f5fb0", "Green": "#2f8a4a", "Gold": "#c99a1e"},
    "laser": {"Red": "#ff3344", "Green": "#39ff88", "Blue": "#3fb8ff", "Violet": "#b45cff"},
    "wire": {"Copper": "#c87533", "Steel": "#8d99a6", "Brass": "#b5a642"},
    "pixel": {"Red": "#ff004d", "Yellow": "#ffec27", "Blue": "#29adff", "Green": "#00e436", "White": "#fff1e8"},
}
BOARD_STRING_NAMES = {"string": "String", "laser": "Laser", "wire": "Wire", "pixel": "Pixel"}
BOARD_DEFAULT = {"surface": "cork", "cards": "sticky", "string": "string", "color": "#c0242b"}

def board_style() -> dict:
    """The board's surface, cards, string kind and string colour, each checked, with the defaults filled in."""
    got = saved().get("board_style")
    got = got if isinstance(got, dict) else {}
    style = dict(BOARD_DEFAULT)
    if got.get("surface") in BOARD_SURFACES:
        style["surface"] = got["surface"]
    if got.get("cards") in BOARD_CARDS:
        style["cards"] = got["cards"]
    if got.get("string") in BOARD_STRINGS:
        style["string"] = got["string"]
        style["color"] = next(iter(BOARD_STRINGS[style["string"]].values()))
    if HEX.fullmatch(str(got.get("color") or "").lower()):
        style["color"] = got["color"].lower()
    return style

def set_board_style(surface: str | None = None, cards: str | None = None, string: str | None = None,
                    color: str | None = None) -> dict:
    """Change any part of the board's look. A new string kind brings its own first colour unless a colour is given
    or the current one is a human's own (in no kind's list). Raises ValueError for anything unknown."""
    style = board_style()
    if surface is not None:
        if surface not in BOARD_SURFACES:
            raise ValueError("not a board surface")
        style["surface"] = surface
    if cards is not None:
        if cards not in BOARD_CARDS:
            raise ValueError("not a card style")
        style["cards"] = cards
    if string is not None:
        if string not in BOARD_STRINGS:
            raise ValueError("not a string kind")
        presets = {c for kind in BOARD_STRINGS.values() for c in kind.values()}
        if string != style["string"] and style["color"] in presets and color is None:
            style["color"] = next(iter(BOARD_STRINGS[string].values()))
        style["string"] = string
    if color is not None:
        color = color.strip().lower() or next(iter(BOARD_STRINGS[style["string"]].values()))
        if not HEX.fullmatch(color):
            raise ValueError("not a colour")
        style["color"] = color
    changed = {k: v for k, v in style.items() if v != BOARD_DEFAULT[k]}
    if changed.get("string") and style["color"] == next(iter(BOARD_STRINGS[style["string"]].values())):
        changed.pop("color", None)                 # a kind's own first colour needs no saving
    save(board_style=changed)
    return style

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
# Each worker can also be several of the same kind (its count, 1 to MAX_COUNT): that many tasks for its role
# run at once, all on its one model. Each copy is a seat: "assistant", "assistant-2", ... (seats()).
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

MAX_COUNT = 8                              # copies of one worker at most; Ollama queues what it can't run at once

def worker_count(worker: str) -> int:
    """How many of this worker run at once (1 unless set), whether or not it is on."""
    try:
        n = int(saved().get(f"{worker}_count") or 1)
    except (TypeError, ValueError):
        return 1
    return max(1, min(MAX_COUNT, n))

def set_worker_count(worker: str, n: int) -> None:
    """Raises ValueError for an unknown worker or a count outside 1 to MAX_COUNT."""
    if worker not in WORKERS:
        raise ValueError(f"no worker {worker!r}")
    if not 1 <= n <= MAX_COUNT:
        raise ValueError(f"a worker count is 1 to {MAX_COUNT}")
    save(**{f"{worker}_count": n if n > 1 else ""})     # 1 is not stored

def seats() -> list[str]:
    """Every copy of every worker that is on, the loop's threads: "assistant", "assistant-2", ..., "pi", ..."""
    return [w if i == 1 else f"{w}-{i}" for w in WORKERS if worker_on(w) for i in range(1, worker_count(w) + 1)]

def seat_worker(seat: str) -> str:
    """The worker a seat is a copy of ("assistant-2" -> "assistant"); anything else (the desk) comes back as is."""
    return seat.split("-", 1)[0]

def seat_name(seat: str) -> str:
    """"Research Assistant", "Research Assistant 2", ...; "" for a seat that is no worker's."""
    worker, _, n = seat.partition("-")
    return f"{WORKERS[worker]} {n}".strip() if worker in WORKERS else ""

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

# ---------- web search: the Brave key the research tools need ----------
# Saved on the home page's first-run card or the Settings page into data/search.json (mode 600, never committed),
# which wins over BRAVE_API_KEY in .env. The MCP search server gets it through agents/toolbox.py.

SEARCH_FILE = Path("data/search.json")
SEARCH_ENV = "BRAVE_API_KEY"
SEARCH_API = "https://api.search.brave.com/res/v1/web/search"   # Brave's public API, used to check a key

def search_key() -> str:
    try:
        found = str(json.loads(SEARCH_FILE.read_text()).get("key") or "")
    except (OSError, ValueError, AttributeError):
        found = ""
    return found or _env(SEARCH_ENV)

def search_key_saved() -> bool:
    """Saved here (so it can be removed here), not only in .env."""
    return SEARCH_FILE.exists()

def save_search_key(key: str) -> None:
    key = key.strip()
    if not key:
        raise ValueError("paste the key")
    if len(key) > 200 or any(c.isspace() for c in key):
        raise ValueError("that doesn't look like a Brave API key")
    _write_private(SEARCH_FILE, {"key": key})

def forget_search_key() -> None:
    SEARCH_FILE.unlink(missing_ok=True)

# ---------- sharing the tools on this network (mcp_share.py) ----------
# Every tool Researcher Chat has, served as one MCP server to other apps on this network (Settings -> MCP server).
# Off unless switched on; the switch and the port are saved in data/settings.json, the token every client must send
# in data/mcp_share.json (mode 600, never committed or printed: the Settings page shows it to the owner to copy).

MCP_PORT_DEFAULT = 8780                    # clear of the UI (8770, or 8771 when that is taken)
MCP_TOKEN_FILE = Path("data/mcp_share.json")

def mcp_share() -> bool:
    return saved().get("mcp_share") == "on"

def set_mcp_share(on: bool) -> None:
    save(mcp_share="on" if on else "")

def mcp_port() -> int:
    try:
        port = int(saved().get("mcp_port") or MCP_PORT_DEFAULT)
    except (TypeError, ValueError):
        return MCP_PORT_DEFAULT
    return port if 1024 <= port <= 65535 else MCP_PORT_DEFAULT

def set_mcp_port(port: int) -> None:
    if not 1024 <= int(port) <= 65535:
        raise ValueError("a port from 1024 to 65535")
    save(mcp_port="" if int(port) == MCP_PORT_DEFAULT else int(port))

def mcp_token() -> str:
    """The token every client sends (Authorization: Bearer <token>); made the first time it is asked for."""
    try:
        found = str(json.loads(MCP_TOKEN_FILE.read_text()).get("token") or "")
    except (OSError, ValueError, AttributeError):
        found = ""
    return found or new_mcp_token()

def new_mcp_token() -> str:
    """A fresh token; clients holding the old one are refused from their next request."""
    import secrets
    token = secrets.token_urlsafe(24)
    _write_private(MCP_TOKEN_FILE, {"token": token})
    return token

# ---------- first run and personalization ----------

def setup_dismissed() -> bool:
    """The human closed the home page's first-run card; Settings → Setup brings it back."""
    return saved().get("setup_dismissed") == "yes"

def set_setup_dismissed(on: bool) -> None:
    save(setup_dismissed="yes" if on else "")

# The publication's name, tagline and About statement: shown on the published site, its About page and its feeds.
# Nothing is stored until a human changes them, so a fresh clone shows the defaults.
PUBLICATION_DEFAULT = "Mission Loop"
NAME_MAX, TAGLINE_MAX, ABOUT_MAX = 60, 160, 2000

def publication_name() -> str:
    return saved().get("publication_name") or PUBLICATION_DEFAULT

def tagline() -> str:
    return saved().get("tagline") or ""

def about_statement() -> str:
    """The human's own words for the top of the About page; "" means the built-in description."""
    return saved().get("about_statement") or ""

def set_publication(name: str, tagline_text: str, about: str) -> None:
    """Raises ValueError when a field is too long. An empty field goes back to its default."""
    name, tagline_text = " ".join(name.split()), " ".join(tagline_text.split())
    about = "\n\n".join(p.strip() for p in about.replace("\r\n", "\n").split("\n\n") if p.strip())
    for value, limit, what in ((name, NAME_MAX, "name"), (tagline_text, TAGLINE_MAX, "tagline"),
                               (about, ABOUT_MAX, "About statement")):
        if len(value) > limit:
            raise ValueError(f"the {what} can be at most {limit} characters")
    save(publication_name="" if name == PUBLICATION_DEFAULT else name, tagline=tagline_text, about_statement=about)

# ---------- outreach: asking real people a question about a mission ----------
# Off until switched on (Settings → Outreach, or MISSION_LOOP_OUTREACH=1 in .env). While on, every approved brief
# goes to the Outreach agent, which asks up to outreach_contacts() people on the channels ticked here that are set
# up, then the mission waits outreach_wait_hours() for their replies (less if everyone answers) before writing.

OUTREACH_CHANNELS = {"email": "Email", "bluesky": "Bluesky", "1f916": "1F916"}
OUTREACH_WAIT = 48                         # hours a mission waits for replies after outreach
OUTREACH_WAIT_MAX = 24 * 14
OUTREACH_CONTACTS = 3                      # people one mission may ask
OUTREACH_CONTACTS_MAX = 6

def outreach_on() -> bool:
    flag = saved().get("outreach")
    return flag == "on" if flag else _env("MISSION_LOOP_OUTREACH") == "1"

def outreach_wait_hours() -> float:
    try:
        hours = float(saved().get("outreach_wait_hours") or OUTREACH_WAIT)
    except (TypeError, ValueError):
        return OUTREACH_WAIT
    return min(max(hours, 0), OUTREACH_WAIT_MAX)

def outreach_contacts() -> int:
    try:
        n = int(saved().get("outreach_contacts") or OUTREACH_CONTACTS)
    except (TypeError, ValueError):
        return OUTREACH_CONTACTS
    return min(max(n, 1), OUTREACH_CONTACTS_MAX)

def outreach_channels() -> list[str]:
    """The channels ticked for outreach (all of them until a human unticks one). contacts.ready_channels() says
    which of these are also set up."""
    found = saved().get("outreach_channels")
    if not isinstance(found, list):
        return list(OUTREACH_CHANNELS)
    return [c for c in OUTREACH_CHANNELS if c in found]

def set_outreach(on: bool, wait_hours: float, contacts: int, channels: list[str]) -> None:
    """Raises ValueError for a wait or a count out of range. Nothing is stored at the defaults."""
    if not 0 <= wait_hours <= OUTREACH_WAIT_MAX:
        raise ValueError(f"the wait is 0 to {OUTREACH_WAIT_MAX} hours")
    if not 1 <= contacts <= OUTREACH_CONTACTS_MAX:
        raise ValueError(f"ask 1 to {OUTREACH_CONTACTS_MAX} people a mission")
    picked = [c for c in OUTREACH_CHANNELS if c in channels]
    save(outreach="on" if on else ("off" if _env("MISSION_LOOP_OUTREACH") == "1" else ""),
         outreach_wait_hours="" if wait_hours == OUTREACH_WAIT else wait_hours,
         outreach_contacts="" if contacts == OUTREACH_CONTACTS else contacts,
         outreach_channels="" if picked == list(OUTREACH_CHANNELS) else (picked or ["none"]))

# Email for outreach: an SMTP login to send and, for reading replies, an IMAP server on the same login. Saved on the
# Settings page to data/email.json (mode 600, never committed). Use an app password, never the account password.
EMAIL_FILE = Path("data/email.json")
EMAIL_FIELDS = ("smtp_host", "smtp_port", "imap_host", "username", "password", "from_name", "from_address")

def email_config() -> dict:
    """{smtp_host, smtp_port, imap_host, username, password, from_name, from_address}, or {} until saved."""
    try:
        data = json.loads(EMAIL_FILE.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not (data.get("smtp_host") and data.get("username") and data.get("password")):
        return {}
    return {k: data.get(k) or "" for k in EMAIL_FIELDS} | {"smtp_port": int(data.get("smtp_port") or 587)}

def clean_email_config(smtp_host: str, smtp_port, imap_host: str, username: str, password: str,
                       from_name: str, from_address: str) -> dict:
    """The fields tidied. Raises ValueError for anything missing or malformed. A blank password keeps the saved one."""
    host = smtp_host.strip().lower()
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host):
        raise ValueError("the SMTP server is a host name, e.g. smtp.gmail.com")
    imap = imap_host.strip().lower()
    if imap and not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", imap):
        raise ValueError("the IMAP server is a host name, e.g. imap.gmail.com")
    try:
        port = int(str(smtp_port).strip() or 587)
    except ValueError:
        raise ValueError("the SMTP port is a number, usually 587 or 465")
    if not 1 <= port <= 65535:
        raise ValueError("the SMTP port is a number, usually 587 or 465")
    username, password = username.strip(), password.strip() or email_config().get("password", "")
    sender = (from_address.strip() or username).lower()
    if not username or not password:
        raise ValueError("a username and an app password are both needed")
    if not EMAIL_ADDRESS.fullmatch(sender):
        raise ValueError("the From address isn't an email address")
    return {"smtp_host": host, "smtp_port": port, "imap_host": imap, "username": username, "password": password,
            "from_name": " ".join(from_name.split())[:60], "from_address": sender}

EMAIL_ADDRESS = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")

def save_email_config(config: dict) -> None:
    _write_private(EMAIL_FILE, config)

def forget_email_config() -> None:
    EMAIL_FILE.unlink(missing_ok=True)
