"""Starts the MCP servers a mission enables and exposes their tools to the LLM.

Adding a research capability = adding an entry to mcp.json. No code change.
"""
import json, os, re, sys
from contextlib import AsyncExitStack
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CONFIG = Path("mcp.json")
MAX_TOOL_CHARS = 12_000                    # keeps one big page from flooding the context

def _expand(value: str) -> str:
    """Replace ${VAR} with the environment variable's value."""
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)

class Toolbox:
    def __init__(self, enabled: list[str], config_path: Path | None = None):
        self.enabled = enabled
        self.config_path = config_path or CONFIG
        self.tools: list[dict] = []        # Ollama tool schemas
        self._owner: dict[str, ClientSession] = {}
        self.server: dict[str, str] = {}   # tool name -> the mcp.json server it came from

    async def __aenter__(self):
        self._stack = AsyncExitStack()
        cfg = json.loads(Path(self.config_path).read_text())
        for name in self.enabled:
            if name not in cfg:
                raise KeyError(f"tool server '{name}' is not defined in {self.config_path}")
            spec = cfg[name]
            env = {**os.environ, **{k: _expand(v) for k, v in spec.get("env", {}).items()}}
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
        return prefix + text[:MAX_TOOL_CHARS]
