"""Switchboard: the ONLY place that decides which implementation each agent uses.

The orchestrator calls agents.find_missions / gather / write_brief / validate / interview / check_inbox / write /
check_article / publish, and posts to every module in CHANNELS. Swapping a stub for a real agent means editing this file and nothing else.
The UI and CLI call agents.chat, the human's assistant, which only reads the loop through MCP.
Set MISSION_LOOP_STUBS=1 to run the whole loop on stubs (no Ollama, no network).

Outreach is OFF for now (milestone 4): the real path uses no_interview, which emails nobody,
so missions skip the comment wait and go straight to writing.

Publishing channels: `publish` writes the local site and its feed (always on). CHANNELS post feed entries
elsewhere (see agents/channel.py). Stub runs have no channels, so they can never post to a real site;
1F916 is also off until data/1f916.json holds a key, Bluesky until a login is saved, and GitHub Pages until
data/github.json holds a token and a linked repo.
"""
import os

from agents.stubs import check_inbox
from agents.publisher import publish       # local files only, so stubs and real mode share it
from agents import bluesky, f916, github

if os.environ.get("MISSION_LOOP_STUBS") == "1":
    from agents.stubs import find_missions, gather, write_brief, validate, interview, write, check_article, chat, chat_models

    def ready() -> tuple[bool, str]:
        return True, "stubs"

    CHANNELS = []

    def f916_enabled() -> bool:
        return False

    def bluesky_enabled() -> bool:
        return False

    def github_enabled() -> bool:
        return False
else:
    from agents.finder import find_missions
    from agents.researcher import gather, write_brief
    from checks.validator import validate
    from agents.stubs import no_interview as interview
    from agents.writer import write
    from checks.article_check import check_article
    from agents.llm import ready
    from agents.assistant import chat, models as chat_models   # the human's assistant; reads the loop through MCP, never runs it
    from agents.f916 import enabled as f916_enabled
    from agents.bluesky import enabled as bluesky_enabled
    from agents.github import enabled as github_enabled
    CHANNELS = [f916, github, bluesky]     # in this order, so Bluesky links to a page already on the site, or a fresh 1F916 post

__all__ = ["find_missions", "gather", "write_brief", "validate", "interview", "check_inbox", "write", "check_article",
           "publish", "CHANNELS", "f916_enabled", "bluesky_enabled", "github_enabled", "ready", "chat", "chat_models"]
