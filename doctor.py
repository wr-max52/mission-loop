"""The setup check: what this machine still needs before the loop can do its work, each with how to fix it.

One list, used everywhere: `python run.py doctor`, the home page's first-run card, Settings → Setup, play
refusing to start, and Researcher Chat's `setup_status` tool. It reads config and asks Ollama (already a quick,
never-raising call); `deep=True` also asks each cloud provider and is what the loop itself checks (llm.ready).

The loop writes why it is waiting (data/waiting.json) when it can't work, so the UI can say "Waiting: Ollama isn't
answering" instead of "Running". Nothing here starts or stops anything.
"""
import json, os, time
from dataclasses import dataclass, asdict
from pathlib import Path
import httpx
import settings, store
from i18n import _, _n
from agents import llm, toolbox

WAITING = Path("data/waiting.json")
TRANSPORT: httpx.BaseTransport | None = None   # tests swap in a mock; None is the real network
OLLAMA_DOWNLOAD = "https://ollama.com/download"
BRAVE_KEYS = "https://brave.com/search/api/"

@dataclass
class Check:
    key: str            # models, connection, search, npx, uvx, work, outreach
    title: str          # what is being checked, in a few words
    ok: bool
    detail: str         # what was found
    fix: str = ""       # what to do about it, empty when ok
    link: str = ""      # where to do it: a UI path or a web page
    blocks_loop: bool = False   # True: the loop can't work at all until this is fixed

def _models(conn: dict) -> list[Check]:
    missing = settings.missing_models()
    if missing:
        who = _(" and ").join(_(settings.WORKERS[w]) for w in missing)
        if conn["ok"] and conn["models"]:
            fix = _("Press Connect Ollama, or pick models under Settings → Workers.")
        elif conn["ok"]:
            fix = _("Ollama has no models. Run ollama pull <model>, or add a cloud provider's key.")
        else:
            fix = _("Install and open Ollama, or add a cloud provider's API key in Settings.")
        return [Check("models", _("A model for each worker"), False, _("No model for: {who}.").format(who=who), fix,
                      "/settings#workers", True)]
    names = ", ".join(f"{_(settings.WORKERS[w])}: {settings.worker_model(w)}" for w in settings.WORKERS
                      if settings.worker_on(w))
    return [Check("models", _("A model for each worker"), True, names)]

def _connection(conn: dict, deep: bool) -> list[Check]:
    if settings.missing_models():
        return []
    if deep:
        ok, why = llm.ready()
        return [Check("connection", _("Models answer"), ok, why if ok else f"{why}.",
                      "" if ok else _("Fix it in Settings → Model providers, then press play."),
                      "" if ok else "/settings#providers", not ok)]
    installed = {m["name"] for m in conn["models"]}
    for w in settings.WORKERS:
        if not settings.worker_on(w):
            continue
        provider, name = settings.split_model(settings.worker_model(w))
        who = _(settings.WORKERS[w])
        if provider == settings.LOCAL and not conn["ok"]:
            return [Check("connection", _("Models answer"), False,
                          _("Ollama not answering at {host} ({who} uses it).").format(host=conn["host"], who=who),
                          _("Open Ollama, or fix its address in Settings."), "/settings#providers", True)]
        if provider == settings.LOCAL and name not in installed:
            return [Check("connection", _("Models answer"), False,
                          _("{model} ({who}) not installed in Ollama.").format(model=name, who=who),
                          _("Run: ollama pull {model}").format(model=name), "/settings#workers", True)]
        if provider != settings.LOCAL and not settings.provider_on(provider):
            return [Check("connection", _("Models answer"), False,
                          _("{who} uses {provider}, which has no API key.").format(who=who, provider=provider),
                          _("Add its key in Settings → Model providers."), "/settings#providers", True)]
    return [Check("connection", _("Models answer"), True, _("Every model's provider is connected."))]

def _tools() -> list[Check]:
    status = toolbox.server_status(["search", "fetch"])
    out = []
    key = settings.search_key()
    out.append(Check("search", _("Web search key"), bool(key),
                     _("Brave Search key set.") if key else _("No Brave Search key. Research can't search the web."),
                     "" if key else _("Get a free key from Brave and paste it under Web search."),
                     "" if key else BRAVE_KEYS))
    for command, server, what in (("npx", "search", _("web search")), ("uvx", "fetch", _("reading pages"))):
        missing = "not installed" in status.get(server, "")
        out.append(Check(command, _("{command} for {what}").format(command=command, what=what), not missing,
                         _("{command} not installed: {what} is off.").format(command=command, what=what) if missing
                         else _("{command} installed.").format(command=command),
                         _("Install it: brew install {package}").format(package="node" if command == "npx" else "uv")
                         if missing else ""))
    return out

def _work() -> list[Check]:
    line = store.get_inspiration()
    missions = len(store.all_missions())
    ok = bool(line or missions)
    detail = ((_n("{n} mission", "{n} missions", missions) + (_(", and an inspiration line") if line else ""))
              if missions else _("Inspiration line set.") if line else _("No missions and no inspiration line."))
    return [Check("work", _("Something to work on"), ok, detail,
                  "" if ok else _("Add a mission, or give the mission finder an inspiration line."))]

def _outreach() -> list[Check]:
    """Only while outreach is switched on: a channel it can use. Without one, missions just skip outreach."""
    if not settings.outreach_on():
        return []
    import contacts                        # late: contacts reads the channels' key files
    ready = contacts.ready_channels()
    names = ", ".join(settings.OUTREACH_CHANNELS[c] for c in ready)
    return [Check("outreach", _("An outreach channel"), bool(ready),
                  _("Outreach uses {names}.").format(names=names) if ready else _("Outreach is on, but no channel is set up."),
                  "" if ready else _("Set up email in Settings → Outreach, or add Bluesky or 1F916 under Publishing."),
                  "" if ready else "/settings#outreach")]

def stubs() -> bool:
    return os.environ.get("MISSION_LOOP_STUBS") == "1"

def checks(deep: bool = False, conn: dict | None = None) -> list[Check]:
    """Everything, in setup order. Never raises."""
    if stubs():                            # MISSION_LOOP_STUBS=1 needs no model, so play already works
        return [Check("models", _("A model for each worker"), True, _("Running on stubs (MISSION_LOOP_STUBS=1), no model needed"))] \
            + _tools() + _work()
    conn = conn or llm.connection()
    return _models(conn) + _connection(conn, deep) + _tools() + _work() + _outreach()

def problems(found: list[Check] | None = None) -> list[Check]:
    return [c for c in (found if found is not None else checks()) if not c.ok]

def done(found: list[Check] | None = None) -> bool:
    return not problems(found)

def as_dicts(found: list[Check]) -> list[dict]:
    return [asdict(c) for c in found]

# ---------- checking a Brave key before it is saved ----------

def check_search_key(key: str) -> tuple[bool, str]:
    """(True, "") if Brave takes the key; (False, why) if it refuses it. A network failure counts as unknown, not
    refused: (True, note), so a key can still be saved offline (warn, don't block)."""
    try:
        with httpx.Client(transport=TRANSPORT, timeout=10) as client:
            r = client.get(settings.SEARCH_API, params={"q": "test", "count": 1},
                           headers={"X-Subscription-Token": key.strip(), "Accept": "application/json"})
    except httpx.HTTPError as e:
        return True, f"Couldn't reach Brave to check it ({type(e).__name__}); saved anyway."
    if r.status_code in (401, 403, 422):
        return False, "Brave refused that key"
    if r.status_code == 429:
        return True, "Brave says the key is over its rate limit right now; saved anyway."
    if r.status_code >= 400:
        return True, f"Brave answered {r.status_code}; saved anyway."
    return True, ""

# ---------- why the loop is waiting ----------

def set_waiting(reason: str) -> bool:
    """Record why the loop can't work. True when the reason is new, so the log says it once, not every retry."""
    old = waiting()
    WAITING.parent.mkdir(parents=True, exist_ok=True)
    WAITING.write_text(json.dumps({"reason": reason, "since": old["since"] if old and old["reason"] == reason
                                   else time.time()}))
    return not old or old["reason"] != reason

def clear_waiting() -> None:
    WAITING.unlink(missing_ok=True)

def waiting() -> dict | None:
    """{"reason", "since"} while the loop (or the last press of play) found it can't work, else None."""
    try:
        data = json.loads(WAITING.read_text())
        return data if isinstance(data, dict) and data.get("reason") else None
    except (OSError, ValueError):
        return None

def report(found: list[Check]) -> str:
    """The plain-text checklist `run.py doctor` prints."""
    lines = []
    for c in found:
        mark = "ok" if c.ok else "!!" if c.blocks_loop else "--"
        lines.append(f"{mark} {c.title}: {c.detail}")
        if c.fix:
            lines.append(f"   fix: {c.fix}" + (f" ({c.link})" if c.link.startswith("http") else ""))
    lines.append("All set: press play in the UI, or run python run.py loop." if done(found) else
                 "The loop can't start until the items marked !! are fixed." if any(c.blocks_loop and not c.ok for c in found)
                 else "The loop can start; fixing the items marked -- lets it do its job.")
    return "\n".join(lines)
