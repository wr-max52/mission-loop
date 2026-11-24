"""Local MCP server for outreach (contacts.py): the Outreach agent's way to ask a real person a question about the
mission it is working on, and to find people on Bluesky and 1F916.

send_outreach never takes a message: code fills the template with the fields given and sends it, and answers with
what was sent or why it wasn't (and what to fix). The mission comes from MISSION_LOOP_MISSION, which the agent's
toolbox sets when it starts this server, so a model can't send for another mission.
The agents start it through the "outreach" entry in mcp.json.
"""
import os
from mcp.server.mcpserver import MCPServer
import contacts

server = MCPServer("outreach")

@server.tool()
def send_outreach(channel: str, address: str, name: str, topic: str, question: str) -> str:
    """Ask one person one question about this mission. Code writes and sends the message:
    "Hi {name}, I'm an AI Agent on a research mission about {topic} and I need your help answering this:
    {question}
    Appreciate your help! You can learn more about my missions here <our site>
    From, <our publication>, AI Agent" (the question on its own line, bold in email).
    channel: "email", "bluesky" or "1f916" (only the ones set up work). address: their email address, Bluesky handle
    (name.bsky.social) or 1F916 handle. name: their first name only. topic: a few words naming what the mission is
    about. question: one short, very specific question about a claim, ending in "?" (did X happen, was Y done, who
    was involved). Answers "Sent ..." or "Not sent: <why>"; fix what it says and try again, or ask someone else."""
    slug = os.environ.get("MISSION_LOOP_MISSION", "")
    return contacts.send(slug, channel, address, name, topic, question)

@server.tool()
def find_on_bluesky(query: str) -> str:
    """Search Bluesky for people by name or words (e.g. "Jane Smith energy reporter"). Returns handles, display names
    and bios. A handle from here is one you can send_outreach to on "bluesky"."""
    return contacts.find_on_bluesky(query)

@server.tool()
def list_1f916_commenters() -> str:
    """Everyone who commented on one of our 1F916 posts, with what they said. On "1f916" you can only ask these people
    (the message is a reply to their comment)."""
    return contacts.commenters_text()

if __name__ == "__main__":
    server.run()
