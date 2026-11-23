"""Starts the MCP servers a mission enables and exposes their tools to the LLM.

Adding a research capability = adding an entry to mcp.json. No code change.

A server this machine can't run is skipped, not fatal: its command isn't installed (npx, uvx), or a variable it
lists under "requires" is empty (the search server's Brave key). `skipped` says which and why, so research carries
on with the tools it has and the setup check (doctor.py) can say what to install.
"""
import json, os, re, shutil, sys
from contextlib import AsyncExitStack
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import settings

CONFIG = Path(__file__).resolve().parent.parent / "mcp.json"   # the project's, whatever the working folder
MAX_TOOL_CHARS = 12_000                    # keeps one big page from flooding the context
WHICH = shutil.which                       # tests swap in their own machine

def room(server: str) -> int:
    """How much of a tool's answer is kept: MAX_TOOL_CHARS, or enough for the Top N passages picked on the
    Settings page when it is the library."""
    if server == "library":
        import library
        return max(MAX_TOOL_CHARS, settings.library_top_n() * library.PASSAGE_ROOM)
    return MAX_TOOL_CHARS

def _value(name: str) -> str:
    """An environment variable, except the Brave key, which may also be saved on the Settings page."""
    return settings.search_key() if name == settings.SEARCH_ENV else os.environ.get(name, "")

def _expand(value: str) -> str:
    """Replace ${VAR} with the variable's value."""
    return re.sub(r"\$\{(\w+)\}", lambda m: _value(m.group(1)), value)

def unavailable(spec: dict) -> str:
    """Why this mcp.json server can't run on this machine, or "" if it can."""
    command = spec.get("command", "")
    if command != "python" and not WHICH(command):
        return f"{command} is not installed"
    missing = [var for var in spec.get("requires", []) if not _value(var)]
    return f"{' and '.join(missing)} is not set" if missing else ""

def server_status(names: list[str], config_path: Path | None = None) -> dict[str, str]:
    """For each named server: "" if it can run here, else why not. Starts nothing."""
    try:
        cfg = json.loads(Path(config_path or CONFIG).read_text())
    except (OSError, ValueError) as e:
        return {name: f"{config_path or CONFIG} can't be read ({type(e).__name__})" for name in names}
    return {name: unavailable(cfg[name]) if name in cfg else "not defined in mcp.json" for name in names}

class Toolbox:
    def __init__(self, enabled: list[str], config_path: Path | None = None, env: dict | None = None):
        self.enabled = enabled
        self.config_path = config_path or CONFIG
        self.extra_env = env or {}         # given to every server it starts (the outreach server's mission)
        self.tools: list[dict] = []        # Ollama tool schemas
        self._owner: dict[str, ClientSession] = {}
        self.server: dict[str, str] = {}   # tool name -> the mcp.json server it came from
        self.skipped: dict[str, str] = {}  # server -> why it can't run on this machine

    async def __aenter__(self):
        self._stack = AsyncExitStack()
        cfg = json.loads(Path(self.config_path).read_text())
        for name in self.enabled:
            if name not in cfg:
                raise KeyError(f"tool server '{name}' is not defined in {self.config_path}")
            spec = cfg[name]
            why = unavailable(spec)
            if why:
                self.skipped[name] = why
                continue
            env = {**os.environ, **self.extra_env, **{k: _expand(v) for k, v in spec.get("env", {}).items()}}
            # "python" means this interpreter: launchd's PATH doesn't include the venv
            command = sys.executable if spec["command"] == "python" else spec["command"]
            params = StdioServerParameters(command=command, args=[self._local(_expand(a)) for a in spec.get("args", [])],
                                           env=env)
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            for tool in (await session.list_tools()).tools:
                self._owner[tool.name] = session
                self.server[tool.name] = name
                self.tools.append({"type": "function", "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.input_schema,
                }})
        return self

    def _local(self, arg: str) -> str:
        """A script named relative to mcp.json (status_tools.py) works from any working directory."""
        here = Path(self.config_path).resolve().parent / arg
        return str(here) if arg.endswith(".py") and not os.path.isabs(arg) and here.is_file() else arg

    async def __aexit__(self, *exc):
        await self._stack.aclose()

    async def call(self, name: str, arguments: dict | None) -> str:
        session = self._owner.get(name)
        if session is None:
            return f"ERROR: unknown tool '{name}'. Available: {sorted(self._owner)}"
        try:
            result = await session.call_tool(name, arguments or {})
        except Exception as e:                # a bad tool call should not kill the mission
            return f"ERROR: {name} failed: {e}"
        text = "\n".join(c.text for c in getattr(result, "content", []) if getattr(c, "type", "") == "text")
        prefix = "ERROR: " if getattr(result, "is_error", False) else ""
        return prefix + text[:room(self.server.get(name, ""))]
