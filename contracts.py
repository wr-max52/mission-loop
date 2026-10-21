from datetime import datetime
from enum import Enum
from typing import Literal
from pydantic import BaseModel, Field

class State(str, Enum):
    QUEUED = "QUEUED"
    RESEARCHING = "RESEARCHING"
    BRIEF_READY = "BRIEF_READY"            # waits for human approval
    AWAITING_COMMENTS = "AWAITING_COMMENTS"
    WRITING = "WRITING"
    ARTICLE_READY = "ARTICLE_READY"        # waits for human review
    DONE = "DONE"
    PARKED = "PARKED"

class Mission(BaseModel):
    slug: str
    topic: str
    objective: str = ""
    tools: list[str] = ["search", "fetch"]
    state: State = State.QUEUED
    attempts: int = 0
    max_attempts: int = 3
    wait_hours: float = 48                 # fixed; comments never end it early
    wait_until: datetime | None = None
    approved: bool = False
    reviewed: bool = False
    validator_notes: str = ""

class Source(BaseModel):
    id: str
    url: str
    title: str
    quote: str
    stance: Literal["supports", "contradicts"]

class POC(BaseModel):
    name: str
    role: str
    email: str
    contact_source_url: str
    why_them: str

class ResearchBrief(BaseModel):
    thesis: str
    claims: list[dict]                     # {"text": ..., "source_ids": [...]}
    counterpoint: str
    sources: list[Source]
    pocs: list[POC] = Field(min_length=3)  # 3 or more POCs
    question_for_pocs: str

class OutreachRecord(BaseModel):
    poc_email: str
    gmail_thread_id: str
    sent_at: datetime
    replies: list[dict] = []               # {"text", "received_at", "is_comment", "ok_to_quote"}
