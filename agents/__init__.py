"""Switchboard: the ONLY place that decides which implementation each agent uses.

The orchestrator calls agents.research / validate / interview / check_inbox / write.
Swapping a stub for a real agent means editing this file and nothing else.
Set MISSION_LOOP_STUBS=1 to run the whole loop on stubs (no Ollama, no network).
"""
import os

from agents.stubs import interview, check_inbox, write

if os.environ.get("MISSION_LOOP_STUBS") == "1":
    from agents.stubs import research, validate
else:
    from agents.researcher import research
    from checks.validator import validate

__all__ = ["research", "validate", "interview", "check_inbox", "write"]
