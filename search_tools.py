"""Local MCP server for web search (#106): web_search, news_search and wiki_search, whichever source answers.

The sources and their order are set on the Settings page (search_sources.py does the searching and the falling
through); the agents always see these three tools. The Toolbox starts this through the "search" entry in mcp.json,
so missions saved with tools ["search", "fetch"] keep working.
"""
from mcp.server.mcpserver import MCPServer
import search_sources

server = MCPServer("search")

@server.tool()
def web_search(query: str, count: int = 10, freshness: str = "") -> str:
    """Search the web. Returns titles, addresses (URL) and snippets; fetch a page to read it.

    query: the words to search for. Use the names, places, documents and terms of art you are looking for.
    count: how many results, 1 to 20.
    freshness: "day", "week", "month" or "year" to see only recent pages; leave empty for any date.
    """
    return search_sources.run_search("web", query, count, freshness)

@server.tool()
def news_search(query: str, count: int = 10, freshness: str = "") -> str:
    """Search news articles. Returns titles, addresses (URL), dates and snippets; fetch an article to read it.

    query: the words to search for.
    count: how many results, 1 to 20.
    freshness: "day", "week", "month" or "year" for recent news; leave empty for older events.
    """
    return search_sources.run_search("news", query, count, freshness)

@server.tool()
def wiki_search(query: str, count: int = 5) -> str:
    """Search Wikipedia for background on a specific topic, person, place or term.

    Wikipedia is not reliable on its own: use it to get oriented and to find the sources an article cites, then
    fetch and cite those sources, not Wikipedia, for anything that matters or is contested.
    query: the topic, name or term.
    count: how many articles, 1 to 10.
    """
    return search_sources.wiki(query, count)

if __name__ == "__main__":
    server.run()
