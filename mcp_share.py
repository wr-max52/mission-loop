"""Shares every tool Researcher Chat has with other MCP clients on this network, as one MCP server.

The tools are not written again here: the gateway opens the same mcp.json servers the chat assistant opens
(`assistant.servers()`: loop, library, fetch, pages, and search when a Brave key is set) through agents/toolbox.py
and serves their tools over MCP's streamable HTTP at http://<this machine>:<port>/mcp. So every tool the Theorist
or any agent has, the chat has, and so does whatever connects here (Claude Code, Claude Desktop, Cursor, LM Studio,
another machine's agent). Writes go through the same checks as the chat's; approving and publishing stay buttons.

Local network only, no cloud: it listens on every interface of this machine but lets in only addresses on this
network (private, loopback and link-local; never a public address, nor the 100.64/10 shared space VPN tunnels and
carrier NAT use), and every request must carry the token (`Authorization: Bearer <token>`, settings.mcp_token).

Off unless switched on (Settings -> MCP server, settings.mcp_share). The UI runs it on a thread beside itself
(`start`, `stop`); `python run.py mcp` runs it on its own. The tools are read when it starts, so it restarts when the
web search key changes.
"""
import asyncio, ipaddress, secrets, socket, threading, time
from contextlib import asynccontextmanager
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount
import settings

NAME = "mission-loop"
PATH = "/mcp"
BIND = "0.0.0.0"                           # every interface; Guard lets in only this network
SHARED = ipaddress.ip_network("100.64.0.0/10")   # carrier NAT and VPN tunnels: not this network
START_TIME = 60                            # seconds for the tool servers to start (npx may download the search server)

def servers() -> list[str]:
    """The mcp.json servers shared: exactly the chat assistant's."""
    from agents import assistant
    return assistant.servers()

def on_this_network(host: str) -> bool:
    """A client address on this machine or its local network: loopback, private or link-local."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if ip.version == 4 and ip in SHARED:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local

def lan_address() -> str:
    """This machine's address on the local network, as others would type it; 127.0.0.1 with no network. The UDP
    socket is only pointed at a private address to pick the interface; nothing is sent."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        except OSError:
            return "127.0.0.1"
    return ip if on_this_network(ip) else "127.0.0.1"

def url(host: str | None = None) -> str:
    return f"http://{host or lan_address()}:{settings.mcp_port()}{PATH}"

# ---------- the server ----------

class Guard:
    """Lets in only clients on this network that send the token. Reads the token each request, so a new one on
    the Settings page takes effect at once."""

    def __init__(self, app, token=None):
        self.app, self.token = app, token or settings.mcp_token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            host = (scope.get("client") or ("", 0))[0]
            if not on_this_network(host):
                return await JSONResponse({"error": "Only devices on this machine's local network may connect."},
                                          status_code=403)(scope, receive, send)
            auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
            if not secrets.compare_digest(auth.encode(), f"Bearer {self.token()}".encode()):
                return await JSONResponse({"error": "Send the token from Mission Loop's Settings page as "
                                                    "'Authorization: Bearer <token>'."},
                                          status_code=401)(scope, receive, send)
        await self.app(scope, receive, send)

def build_app(toolbox_factory=None, names: list[str] | None = None, status: dict | None = None):
    """The ASGI app: the shared servers' tools behind Guard. Opening the tool servers is the app's startup, so they
    run as long as it does. status (a dict) gets "tools": [(server, tool)] once they are open, or "error"."""
    from agents.toolbox import Toolbox
    status = {} if status is None else status
    factory = toolbox_factory or Toolbox
    names = servers() if names is None else names
    box = {}

    async def list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[
            types.Tool(name=t["function"]["name"], description=t["function"].get("description") or "",
                       input_schema=t["function"].get("parameters") or {"type": "object"})
            for t in box["tb"].tools])

    async def call_tool(ctx, params) -> types.CallToolResult:
        text = await box["tb"].call(params.name, params.arguments or {})
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)],
                                    is_error=text.startswith("ERROR: "))

    low = Server(NAME, instructions="Mission Loop's research desk: the loop's missions, epics and briefs, the "
                 "Theorist's moves, the desk's library, and web research tools. Changes go through the same checks "
                 "as in the app; approving and publishing stay with the human in the app.",
                 on_list_tools=list_tools, on_call_tool=call_tool)
    inner = low.streamable_http_app(streamable_http_path=PATH, host=BIND,
                                    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    @asynccontextmanager
    async def lifespan(app):
        try:
            async with asyncio.timeout(START_TIME):
                tb = await factory(names).__aenter__()
        except BaseException as e:
            status["error"] = f"the tool servers didn't start: {type(e).__name__}: {e}"[:300]
            raise
        box["tb"] = tb
        status.update(tools=[(tb.server.get(t["function"]["name"], ""), t["function"]["name"]) for t in tb.tools],
                      skipped=dict(getattr(tb, "skipped", {})), error="")
        try:
            async with inner.router.lifespan_context(inner):
                yield
        finally:
            await tb.__aexit__(None, None, None)

    return Guard(Starlette(routes=[Mount("/", app=inner)], lifespan=lifespan))

# ---------- running beside the UI ----------

_lock = threading.Lock()
_run: dict = {}                            # server, thread, status, port

def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((BIND, port))
            return True
        except OSError:
            return False

def running() -> bool:
    t, srv = _run.get("thread"), _run.get("server")
    return bool(t and t.is_alive() and srv and srv.started)

def status() -> dict:
    """{"running", "starting", "port", "tools": [(server, tool)], "skipped": {server: why}, "error"} for the
    Settings page."""
    st = dict(_run.get("status") or {})
    t, srv = _run.get("thread"), _run.get("server")
    return {"running": running(), "starting": bool(t and t.is_alive() and srv and not srv.started),
            "port": _run.get("port", settings.mcp_port()), "tools": st.get("tools", []),
            "skipped": st.get("skipped", {}), "error": st.get("error", "") if not running() else ""}

def start(wait: float = START_TIME + 5) -> str:
    """Start sharing on a thread; "" once it is up, else why not. Waits for the tool servers to open."""
    import uvicorn
    with _lock:
        if running():
            return ""
        port = settings.mcp_port()
        if not port_free(port):
            _run.update(status={"error": f"port {port} is in use; pick another"}, port=port)
            return _run["status"]["error"]
        st: dict = {}
        srv = uvicorn.Server(uvicorn.Config(build_app(status=st), host=BIND, port=port, log_level="warning",
                                            lifespan="on"))
        thread = threading.Thread(target=srv.run, name="mcp-share", daemon=True)
        _run.update(server=srv, thread=thread, status=st, port=port)
        thread.start()
        end = time.monotonic() + wait
        while time.monotonic() < end and thread.is_alive() and not srv.started:
            time.sleep(0.1)
        if running():
            return ""
        st.setdefault("error", "it didn't start in time" if thread.is_alive() else "it stopped while starting")
        return st["error"]

def stop() -> None:
    with _lock:
        srv, thread = _run.get("server"), _run.get("thread")
        if srv:
            srv.should_exit = True
        if thread:
            thread.join(10)
        _run.pop("server", None)
        _run.pop("thread", None)

def restart() -> str:
    """Pick up a change in which tools exist (the web search key): only while it runs."""
    if not running():
        return ""
    stop()
    return start()
