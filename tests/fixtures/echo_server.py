"""Tiny local MCP server used to test the Toolbox plumbing without any network."""
from mcp.server.mcpserver import MCPServer

server = MCPServer("echo")

@server.tool()
def echo(text: str) -> str:
    """Return the text you send."""
    return f"echo: {text}"

if __name__ == "__main__":
    server.run()
