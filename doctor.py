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
import search_sources, search_status, settings, store
from i18n import _, _n
from agents import llm, toolbox

WAITING = Path("data/waiting.json")
OLLAMA_DOWNLOAD = "https://ollama.com/download"

@dataclass
class Check:
    key: str            # models, connection, search, search_up, npx, uvx, work, outreach
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

def _tools(probe: bool = False) -> list[Check]:
    status = toolbox.server_status(["search", "fetch"])
    out = []
    sources = settings.search_sources_on("web")
    names = ", ".join(settings.SEARCH_SOURCES[s]["name"] for s in sources)
    out.append(Check("search", _("Web search"), bool(sources),
                     _("Searches with {names}.").format(names=names) if sources
                     else _("No web search source is on. Research can't search the web."),
                     "" if sources else _("Switch DuckDuckGo on (free, no key) or add a key under Web search."),
                     "" if sources else "/settings#research"))
    if sources and probe:
        probe_search()
    down = search_status.current() if sources else None
    if down:                               # sources are on but searches fail: one check says why
        out.append(Check("search_up", _("Web search works"), False,
                         _("Web search is down: {reason}.").format(reason=down["reason"]),
                         _(down.get("fix") or search_status.FIX["unreachable"]), "/settings#research"))
    missing = "not installed" in status.get("fetch", "")
    out.append(Check("uvx", _("{command} for {what}").format(command="uvx", what=_("reading pages")), not missing,
                     _("{command} not installed: {what} is off.").format(command="uvx", what=_("reading pages"))
                     if missing else _("{command} installed.").format(command="uvx"),
                     _("Install it: brew install {package}").format(package="uv") if missing else ""))
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

def checks(deep: bool = False, conn: dict | None = None, probe: bool = False) -> list[Check]:
    """Everything, in setup order. Never raises. probe (and deep) also runs one real test search, at most every
    PROBE_EVERY seconds, so a saved key that ran out of credit shows up."""
    if stubs():                            # MISSION_LOOP_STUBS=1 needs no model, so play already works
        return [Check("models", _("A model for each worker"), True, _("Running on stubs (MISSION_LOOP_STUBS=1), no model needed"))] \
            + _tools() + _work()
    conn = conn or llm.connection()
    return _models(conn) + _connection(conn, deep) + _tools(probe or deep) + _work() + _outreach()

def problems(found: list[Check] | None = None) -> list[Check]:
    return [c for c in (found if found is not None else checks()) if not c.ok]

def done(found: list[Check] | None = None) -> bool:
    return not problems(found)

def as_dicts(found: list[Check]) -> list[dict]:
    return [asdict(c) for c in found]

# ---------- checking a search source's key before it is saved ----------

def check_search_key(key: str, source: str = "brave") -> tuple[bool, str]:
    """(True, "") if the source takes the key with one test search; (False, why) if it refuses it. Anything else
    (out of credit, rate limited, unreachable) counts as unknown, not refused: (True, note), so a key can still be
    saved (warn, don't block)."""
    name = settings.SEARCH_SOURCES[source]["name"]
    try:
        search_sources.RUN[source]("web", "test", 1, "", key.strip())
    except search_sources.SourceDown as e:
        if e.outage.kind == "key":
            return False, f"{name} refused that key"
        return True, f"{e.outage.reason}; saved anyway."
    search_sources.wake(source)            # a key that works: try the source again at once
    search_status.clear()                  # and a recorded outage is over
    return True, ""

PROBE_EVERY = 10 * 60                      # seconds between real test searches (each one costs a query)
_probed = 0.0

def probe_search() -> search_status.Outage | None:
    """One real test search through the sources in order, at most every PROBE_EVERY seconds: records an outage
    (every source down) or clears one. Returns the outage it found (None when it works, or when it didn't ask)."""
    global _probed
    if time.monotonic() - _probed < PROBE_EVERY and _probed:
        return None
    _probed = time.monotonic()
    try:
        search_sources.search("web", "news today", 1)
    except search_sources.AllDown as e:
        outage = search_status.Outage("all", "", str(e))
        search_status.record(outage)
        return outage
    search_status.clear()
    return None

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
