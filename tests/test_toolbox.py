import json, sys
from pathlib import Path
from agents.toolbox import Toolbox

FIXTURE = Path(__file__).parent / "fixtures" / "echo_server.py"

async def test_toolbox_round_trip(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"echo": {"command": sys.executable, "args": [str(FIXTURE)]}}))
    async with Toolbox(["echo"], config_path=cfg) as tb:
        assert [t["function"]["name"] for t in tb.tools] == ["echo"]
        assert "echo: hi" in await tb.call("echo", {"text": "hi"})
        assert (await tb.call("nope", {})).startswith("ERROR: unknown tool")

async def test_unknown_server_name_is_a_clear_error(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{}")
    try:
        async with Toolbox(["search"], config_path=cfg):
            pass
    except KeyError as e:
        assert "search" in str(e)
    else:
        raise AssertionError("expected KeyError")
