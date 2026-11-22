"""Switchboard: the ONLY place that decides which implementation each agent uses.

The orchestrator calls agents.find_missions / research / validate / interview / check_inbox / write /
check_article / publish / post_1f916. Swapping a stub for a real agent means editing this file and nothing else.
Set MISSION_LOOP_STUBS=1 to run the whole loop on stubs (no Ollama, no network).

Outreach is OFF for now (milestone 4): the real path uses no_interview, which emails nobody,
so missions skip the comment wait and go straight to writing.

Publishing channels: `publish` writes the local site (always on). `post_1f916` posts to 1f916.ai, and is on only
when data/1f916.json holds a key and stubs are off, so a stub run can never post to the real forum.
"""
import os

from agents.stubs import check_inbox
from agents.publisher import publish       # local files only, so stubs and real mode share it
from agents.f916 import post as post_1f916

if os.environ.get("MISSION_LOOP_STUBS") == "1":
    from agents.stubs import find_missions, research, validate, interview, write, check_article

    def ready() -> tuple[bool, str]:
        return True, "stubs"

    def f916_enabled() -> bool:
        return False
else:
    from agents.finder import find_missions
    from agents.researcher import research
    from checks.validator import validate
    from agents.stubs import no_interview as interview
    from agents.writer import write
    from checks.article_check import check_article
    from agents.llm import ready
    from agents.f916 import enabled as f916_enabled

__all__ = ["find_missions", "research", "validate", "interview", "check_inbox", "write", "check_article",
           "publish", "post_1f916", "f916_enabled", "ready"]
