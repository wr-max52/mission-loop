"""Sharing every tool Researcher Chat has as one MCP server on this network (mcp_share.py, Settings -> MCP server),
and the Theorist's board moves the chat (and so the share) gained."""
import json, socket, stat, threading, time
from pathlib import Path
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from starlette.testclient import TestClient
import board, mcp_share, orchestrator, settings, status_tools, store, ui
from contracts import Epic, Mission

LAN = ("192.168.1.20", 50000)

class FakeToolbox:
    def __init__(self, names):
        self.names, self.calls, self.closed = names, [], False
        self.tools = [{"type": "function", "function": {"name": "loop_status", "description": "What the loop does.",
                                                        "parameters": {"type": "object", "properties": {}}}},
                      {"type": "function", "function": {"name": "fetch", "description": "Read a page.",
                                                        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}}}}]
        self.server = {"loop_status": "loop", "fetch": "fetch"}
        self.skipped = {"search": "BRAVE_API_KEY is not set"}
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): self.closed = True
    async def call(self, name, args):
        self.calls.append((name, args))
        return "ERROR: fetch failed: no network" if name == "fetch" else "Loop: off"

# ---------- who gets in ----------

@pytest.mark.parametrize("host", ["192.168.1.20", "10.0.0.7", "172.16.4.2", "127.0.0.1", "::1", "fe80::1",
                                  "169.254.3.3", "::ffff:192.168.1.2", "fd12::5"])
def test_this_network_gets_in(host):
    assert mcp_share.on_this_network(host)

@pytest.mark.parametrize("host", ["8.8.8.8", "100.100.1.1", "2001:4860::8888", "", "testclient", "::ffff:8.8.8.8"])
def test_the_internet_and_vpn_tunnels_do_not(host):
    assert not mcp_share.on_this_network(host)

def test_token_is_made_once_kept_private_and_can_be_replaced():
    token = settings.mcp_token()
    assert len(token) >= 30 and settings.mcp_token() == token
    assert stat.S_IMODE(Path("data/mcp_share.json").stat().st_mode) == 0o600
    assert settings.new_mcp_token() != token and settings.mcp_token() != token

def test_sharing_is_off_until_switched_on_and_the_port_is_config():
    assert not settings.mcp_share() and settings.mcp_port() == settings.MCP_PORT_DEFAULT
    settings.set_mcp_share(True)
    settings.set_mcp_port(9100)
    assert settings.mcp_share() and settings.mcp_port() == 9100
    settings.set_mcp_port(settings.MCP_PORT_DEFAULT)
    assert "mcp_port" not in settings.saved()                       # nothing stored at the default
    with pytest.raises(ValueError):
        settings.set_mcp_port(80)

def test_shares_exactly_the_chat_assistants_servers(monkeypatch):
    from agents import assistant
    monkeypatch.setattr(assistant, "servers", lambda: ["loop", "library", "fetch", "pages", "search"])
    assert mcp_share.servers() == ["loop", "library", "fetch", "pages", "search"]

def test_the_guard_refuses_other_networks_and_missing_tokens():
    boxes = []
    def app():                             # an app runs once, like each start of sharing
        return mcp_share.build_app(lambda n: boxes.append(FakeToolbox(n)) or boxes[-1], names=["loop", "fetch"])
    with TestClient(app(), client=("8.8.8.8", 1)) as far:
        assert far.post("/mcp", json={}).status_code == 403
    with TestClient(app(), client=LAN) as near:
        assert near.post("/mcp", json={}).status_code == 401
        assert near.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
        ok = near.post("/mcp", headers={"Authorization": f"Bearer {settings.mcp_token()}",
                                        "Accept": "application/json, text/event-stream"},
                       json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                           "protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "test", "version": "1"}}})
        assert ok.status_code == 200 and "mission-loop" in ok.text
    assert boxes[-1].names == ["loop", "fetch"] and boxes[-1].closed    # opened with the app, closed with it

# ---------- a real client over HTTP ----------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

async def test_a_client_on_this_machine_lists_and_calls_the_tools():
    status, port = {}, _free_port()
    app = mcp_share.build_app(FakeToolbox, names=["loop", "fetch"], status=status)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if srv.started:
                break
            time.sleep(0.05)
        assert status["tools"] == [("loop", "loop_status"), ("fetch", "fetch")]
        assert status["skipped"] == {"search": "BRAVE_API_KEY is not set"}
        client = create_mcp_http_client(headers={"Authorization": f"Bearer {settings.mcp_token()}"})
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=client) as (r, w, *_):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = (await s.list_tools()).tools
                assert [t.name for t in tools] == ["loop_status", "fetch"]
                assert tools[1].input_schema["properties"]["url"]["type"] == "string"
                done = await s.call_tool("loop_status", {})
                assert done.content[0].text == "Loop: off" and not done.is_error
                assert (await s.call_tool("fetch", {"url": "x"})).is_error
    finally:
        srv.should_exit = True
        thread.join(5)

def test_status_says_why_it_is_not_running(monkeypatch):
    monkeypatch.setattr(mcp_share, "port_free", lambda port: False)
    assert "in use" in mcp_share.start()
    st = mcp_share.status()
    assert not st["running"] and "in use" in st["error"]
    assert mcp_share.restart() == ""                                  # nothing to restart while it isn't running

# ---------- Settings -> MCP server ----------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(orchestrator, "LOCK_FILE", Path("data/loop.lock"))
    return TestClient(ui.app)

def test_settings_switches_sharing_on_and_shows_how_to_connect(client, monkeypatch):
    started, stopped = [], []
    monkeypatch.setattr(mcp_share, "start", lambda: started.append(1) or "")
    monkeypatch.setattr(mcp_share, "stop", lambda: stopped.append(1))
    monkeypatch.setattr(mcp_share, "lan_address", lambda: "192.168.1.20")
    page = client.get("/settings").text
    assert 'id="mcp"' in page and "Share on this network" in page and "Bearer" not in page   # off: no token shown
    r = client.post("/settings/mcp", data={"do": "on"}, follow_redirects=False)
    assert r.status_code == 303 and "done=mcp_on" in r.headers["location"] and started == [1]
    assert settings.mcp_share()
    monkeypatch.setattr(mcp_share, "status", lambda: {"running": True, "starting": False, "port": 8780, "error": "",
                                                       "tools": [("loop", "loop_status"), ("library", "search_library")],
                                                       "skipped": {}})
    page = client.get("/settings").text
    token = settings.mcp_token()
    assert "http://192.168.1.20:8780/mcp" in page and token in page
    assert f'claude mcp add --transport http mission-loop http://192.168.1.20:8780/mcp' in page
    assert "mcp-remote" in page and "Sharing 2 tools" in page and "no web search source is on" in page
    client.post("/settings/mcp", data={"do": "token"})
    assert settings.mcp_token() != token
    assert "error=mcp_port" in client.post("/settings/mcp", data={"do": "port", "port": "80"},
                                           follow_redirects=False).headers["location"]
    client.post("/settings/mcp", data={"do": "off"})
    assert not settings.mcp_share() and stopped

def test_settings_says_when_sharing_did_not_start(client, monkeypatch):
    monkeypatch.setattr(mcp_share, "start", lambda: "port 8780 is in use; pick another")
    r = client.post("/settings/mcp", data={"do": "on"}, follow_redirects=False)
    assert "error=mcp_start" in r.headers["location"]
    monkeypatch.setattr(mcp_share, "status", lambda: {"running": False, "starting": False, "port": 8780, "tools": [],
                                                       "skipped": {}, "error": "port 8780 is in use; pick another"})
    assert "port 8780 is in use" in client.get("/settings").text

def test_settings_refuses_cross_site_posts(client):
    assert client.post("/settings/mcp", data={"do": "on"}, headers={"origin": "https://evil.example"}).status_code == 403
    assert not settings.mcp_share()

# ---------- the board moves the chat gained from the Theorist ----------

def test_chat_reads_and_arranges_the_board_like_a_board_turn():
    store.save_epic(Epic(slug="grid", title="Grid", status="active"))
    store.save_mission(Mission(slug="outage", topic="Why the grid failed", epic="grid"))
    store.save_mission(Mission(slug="dams", topic="Dam safety", epic="grid"))
    text = status_tools.board_overview()
    assert "m:outage" in text and "e:grid" in text and "Strings:" in text
    out = status_tools.arrange_board([{"id": "m:outage", "x": 4000, "y": -300, "size": 9},
                                      {"id": "m:nope", "x": 0, "y": 0}], why="cluster")
    assert "Moved 1 note" in out and "no such note" in out
    saved = store.board_notes()[("mission", "outage")]
    assert saved.by == "chat" and saved.size == board.SIZE_MAX     # held to the board like the Theorist's moves
