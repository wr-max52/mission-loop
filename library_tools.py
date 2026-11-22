"""Local MCP server for the desk's library (library.py): retrieval over everything the desk has written.

The Theorist searches it before it gives its theory, and Researcher Chat searches it to answer questions about what
the desk has found. Read-only: a search syncs the library's own index (data/library.db) with the desk and may call
the embedding model set on the Settings page, but never changes a mission. Any other MCP client can use it too.
The agents start it through the "library" entry in mcp.json.
"""
from mcp.server.mcpserver import MCPServer
import library

server = MCPServer("library")

@server.tool()
def search_library(query: str, epic: str = "", mission: str = "") -> str:
    """Search the desk's own work by meaning and words: every mission's topic and objective, each brief's thesis,
    claims (with the exact quotes they cite and their URLs), counterpoint and sources, and each article's title,
    dek and sections. Returns the best passages (how many is set on the Settings page), each with its mission's
    slug, stage and epic. query: what you want to know, in plain words (a name, a mechanism, a number, a question).
    epic: optional, only missions in this epic (its title). mission: optional, only this mission (its slug).
    Passages are the desk's own past work: reason from them, but cite only real web pages."""
    return library.ask(query, epic=epic, mission=mission)

if __name__ == "__main__":
    server.run()
