"""Switchboard: the ONLY place that decides which implementation each agent uses.

The orchestrator calls agents.theorize / tidy_board / find_missions / gather / write_brief / validate / outreach /
check_replies / write / check_article / publish, and posts to every module in CHANNELS. Swapping a stub for a real agent means editing this file and nothing else.
The UI and CLI call agents.chat, Researcher Chat, which reads the loop and the web through MCP, and the avatar
studio calls agents.edit_avatar, which edits a sprite spec (SPRITES.md) from what a person asks.
Set MISSION_LOOP_STUBS=1 to run the whole loop on stubs (no Ollama, no network).

Outreach runs only while it is switched on (Settings → Outreach) and a channel for it is set up
(outreach_channels()); otherwise missions skip it and the reply wait and go straight to writing. Stub runs never
contact anyone.

Publishing: `publish` writes the local site and its feed (always on): the foundation every channel posts from.
channels() is everything else, in order: the built-in CHANNELS, then the custom ones set up on the Settings page
(agents/custom.py). See agents/channel.py. Stub runs have no channels, so they can never post to a real site;
1F916 is also off until data/1f916.json holds a key, Bluesky until a login is saved, and GitHub Pages until
data/github.json holds a token and a linked repo.
"""
import os

from agents.publisher import publish       # local files only, so stubs and real mode share it
from agents import bluesky, custom, f916, github

if os.environ.get("MISSION_LOOP_STUBS") == "1":
    from agents.stubs import theorize, tidy_board, find_missions, gather, write_brief, validate, outreach, check_replies, write, check_article, chat, chat_models, chat_catalog, chat_web, edit_avatar, avatar_models

    def ready() -> tuple[bool, str]:
        return True, "stubs"

    def outreach_channels() -> list[str]:
        return []                          # stubs never contact anyone

    CHANNELS = []

    def channels() -> list:
        return []

    def f916_enabled() -> bool:
        return False

    def bluesky_enabled() -> bool:
        return False

    def github_enabled() -> bool:
        return False
else:
    from agents.theorist import theorize, tidy_board
    from agents.finder import find_missions
    from agents.researcher import gather, write_brief
    from checks.validator import validate
    from agents.outreach_agent import reach_out as outreach
    from contacts import check_replies, ready_channels as outreach_channels
    from agents.writer import write
    from checks.article_check import check_article
    from agents.llm import ready
    from agents.assistant import chat, models as chat_models, catalog as chat_catalog, web_search as chat_web  # Researcher Chat; reads the loop through MCP, never runs it
    from agents.avatar import edit as edit_avatar, models as avatar_models   # the avatar studio's designer; edits a spec, saves nothing
    from agents.f916 import enabled as f916_enabled
    from agents.bluesky import enabled as bluesky_enabled
    from agents.github import enabled as github_enabled
    CHANNELS = [f916, github, bluesky]     # in this order, so Bluesky links to a page already on the site, or a fresh 1F916 post

    def channels() -> list:
        """Every channel beyond the local site: the built-in ones, then the custom ones."""
        return list(CHANNELS) + custom.channels()

__all__ = ["theorize", "tidy_board", "find_missions", "gather", "write_brief", "validate", "outreach", "check_replies", "outreach_channels", "write", "check_article",
           "publish", "CHANNELS", "channels", "f916_enabled", "bluesky_enabled", "github_enabled", "ready", "chat",
           "chat_models", "chat_catalog", "chat_web", "edit_avatar", "avatar_models"]
