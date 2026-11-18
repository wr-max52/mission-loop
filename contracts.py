import re
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
    attempts: int = 0                      # research attempts
    write_attempts: int = 0                # writer attempts
    max_attempts: int = 3
    wait_hours: float = 48                 # fixed; comments never end it early
    wait_until: datetime | None = None
    approved: bool = False
    reviewed: bool = False
    validator_notes: str = ""              # validator failures or human feedback
    rejected_brief: "RejectedBrief | None" = None  # last brief the validator failed; retries revise it
    published_at: datetime | None = None

class Claim(BaseModel):
    text: str
    source_ids: list[str]

class Source(BaseModel):
    id: str
    url: str
    title: str
    quote: str                             # exact sentence copied from the page
    stance: Literal["supports", "contradicts"]

class POC(BaseModel):
    name: str
    role: str
    email: str
    contact_source_url: str                # public page where the email appears
    why_them: str

class ResearchBrief(BaseModel):
    thesis: str
    claims: list[Claim]
    counterpoint: str
    sources: list[Source]
    pocs: list[POC] = []                   # validator requires 3+ only when outreach is on
    question_for_pocs: str = ""

class RejectedBrief(BaseModel):
    brief: ResearchBrief
    notes: str                             # the validator's verdict on this exact brief

Mission.model_rebuild()

class OutreachRecord(BaseModel):
    poc_email: str
    gmail_thread_id: str
    sent_at: datetime
    replies: list[dict] = []

class Section(BaseModel):
    heading: str
    paragraphs: list[str]                  # cite sources inline by id, e.g. "... in 2023 [s1, s2]."

class Article(BaseModel):
    title: str
    dek: str                               # one-sentence summary shown under the title
    sections: list[Section] = Field(min_length=2)

# Inline citation in article text: [s1] or [s1, s2]. Shared by the checker and the renderer.
CITATION = re.compile(r"\[([A-Za-z0-9_.-]+(?:\s*,\s*[A-Za-z0-9_.-]+)*)\](?!\()")

def cited_ids(text: str) -> list[str]:
    return [i.strip() for group in CITATION.findall(text) for i in group.split(",")]
