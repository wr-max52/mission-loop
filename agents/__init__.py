"""Switchboard: the ONLY place that decides which implementation each agent uses.

The orchestrator calls agents.research / validate / interview / check_inbox / write /
check_article / publish. Swapping a stub for a real agent means editing this file and nothing else.
Set MISSION_LOOP_STUBS=1 to run the whole loop on stubs (no Ollama, no network).

Outreach is OFF for now (milestone 4): the real path uses no_interview, which emails nobody,
so missions skip the comment wait and go straight to writing.
"""
import os

from agents.stubs import check_inbox
from agents.publisher import publish       # local files only, so stubs and real mode share it

if os.environ.get("MISSION_LOOP_STUBS") == "1":
    from agents.stubs import research, validate, interview, write, check_article

    def ready() -> tuple[bool, str]:
        return True, "stubs"
else:
    from agents.researcher import research
    from checks.validator import validate
    from agents.stubs import no_interview as interview
    from agents.writer import write
    from checks.article_check import check_article
    from agents.llm import ready

__all__ = ["research", "validate", "interview", "check_inbox", "write", "check_article", "publish", "ready"]
