import re
from datetime import datetime
from enum import Enum
from typing import Literal
from pydantic import BaseModel, Field, model_validator

class State(str, Enum):
    QUEUED = "QUEUED"
    RESEARCHING = "RESEARCHING"
    BRIEF_READY = "BRIEF_READY"            # waits for human approval
    AWAITING_COMMENTS = "AWAITING_COMMENTS"
    WRITING = "WRITING"
    ARTICLE_READY = "ARTICLE_READY"        # waits for human review
    DONE = "DONE"
    PARKED = "PARKED"
    COMBINED = "COMBINED"                  # folded into a bigger mission (combined_into); out of the flow

class ChannelPost(BaseModel):
    """One place an article was published: when, and where to find it (a path or a URL)."""
    at: datetime
    where: str

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
    rejected_article: "RejectedArticle | None" = None  # last article the check failed
    gathered: bool = False                 # the Research Assistant's sources are saved (artifact "evidence"); brief next
    search_paused: bool = False            # a stop cut the search short; it carries on as the same attempt
    interrupted_brief: "ResearchBrief | None" = None   # written out when stop was pressed; checked when the loop starts again
    interrupted_article: "Article | None" = None       # same for the writer
    brief_pick: "SourcePick | None" = None      # the brief's first step as written, before its sources are checked
    brief_sources: "SourcePick | None" = None   # the brief's first step, checked: kept until the attempt ends
    article_draft: "ArticleDraft | None" = None # the article's plan and the sections written so far
    unchecked_brief: str = ""              # set when a human pushed a failed brief through: what it failed
    unchecked_article: str = ""            # same for a failed article
    published_at: datetime | None = None
    channels: dict[str, ChannelPost] = {}  # channel key ("site", "1f916") -> where it was published
    channel_errors: dict[str, str] = {}    # channel key -> why its last try refused this article
    publish_requests: dict[str, datetime] = {}   # "site" or a channel key -> when a human asked to (re)publish there
    epic: str = ""                         # slug of the epic this mission belongs to ("" = none yet)
    epic_fallback: str = ""                # finder's best existing epic, used if its proposed new epic is rejected
    combines: list[str] = []               # the missions this one was made from (a combine the human approved)
    combined_into: str = ""                # set with State.COMBINED: the bigger mission that took this one over
    rush: bool = False                     # a human asked for its research next ("Add and start loop"); cleared once picked

# Before writing starts, a mission can still be folded into a bigger one; after that it is nearly an article.
COMBINABLE = {State.QUEUED, State.RESEARCHING, State.BRIEF_READY, State.PARKED}

def is_open(m: Mission) -> bool:
    """Not published yet and still the desk's own work (not combined into another mission)."""
    return m.state not in (State.DONE, State.COMBINED)

class Link(BaseModel):
    """Two missions the Theorist (or a human) connected while keeping them separate: two open missions, or an
    open mission and a published article. Stored once per pair, a < b. A link a human removes stays as
    "removed", so the Theorist doesn't make it again."""
    a: str
    b: str
    why: str = ""
    by: str = "theorist"                   # "theorist", "chat" (the chat assistant) or "human"
    at: str = ""                           # ISO time
    status: Literal["active", "removed"] = "active"

    def other(self, slug: str) -> str:
        return self.b if slug == self.a else self.a

class Combine(BaseModel):
    """The Theorist's proposal to fold 2+ open missions into one bigger mission. It waits for the human (or auto
    mode) like a proposed epic; approving makes the mission and retires the members (State.COMBINED)."""
    slug: str                              # the new mission's slug (made fresh again if taken by then)
    topic: str
    objective: str
    members: list[str]
    epic: str = ""                         # an epic's title, or ""
    why: str = ""
    at: str = ""
    status: Literal["proposed", "approved", "rejected"] = "proposed"
    by: str = "theorist"                   # who proposed it: "theorist" or "chat" (the chat assistant)

class BoardNote(BaseModel):
    """Where a sticky note sits on the cork board (board.py). Only notes code placed or someone moved are stored;
    a note's size stays None until the Theorist or a human sets one, and the board works it out from the stage."""
    kind: Literal["mission", "epic"]
    id: str                                # the mission's or epic's slug ("_unsorted" for missions in no epic)
    x: float
    y: float
    size: float | None = None              # 1.0 = a normal note; None = from its stage and strings
    by: str = "code"                       # "code", "human" or "theorist"
    at: str = ""

class Mention(BaseModel):
    """Two missions code found mentioning each other, stored once per pair and kind, a < b: "source" (both cite
    the same page), "background" (research was shown one as a related published article) or "name" (a brief or
    article names the other mission). In its own table, like links, so no worker saves over one."""
    a: str
    b: str
    kind: Literal["source", "background", "name"]
    detail: str = ""                       # the shared page, the words that matched, or whose research saw the other

class Epic(BaseModel):
    """A long-running line of inquiry: a short thematic title, not a goal. The missions in it describe it,
    so its meaning can drift. The slug never changes; the title can."""
    slug: str
    title: str
    status: Literal["active", "proposed"] = "active"   # proposed = suggested by the finder, awaiting the human

MAX_EPIC_WORDS = 4

def epic_title(text: str) -> str:
    """The title cleaned to one line, or "" if it isn't a usable epic title (1 to MAX_EPIC_WORDS words)."""
    title = " ".join(text.split()).strip(" .,:;-")
    return title if title and len(title.split()) <= MAX_EPIC_WORDS and len(title) <= 48 else ""

class MissionIdea(BaseModel):
    topic: str
    objective: str
    source_url: str                        # a search result showing the claim is really being made
    epic: str = ""                         # title of the existing epic it fits best
    new_epic: str = ""                     # a proposed new epic; kept only if 2+ ideas share it

class Claim(BaseModel):
    text: str
    source_ids: list[str]
    kind: Literal["fact", "possibility"] = "fact"   # possibilities are labeled, never presented as verified

SOURCE_ID = re.compile(r"[A-Za-z0-9_.-]+")  # what an article can cite as [id]; a URL is not one

class Source(BaseModel):
    id: str                                # short label, e.g. "s1"; ResearchBrief replaces any other
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
    # The model writes fields in this order. Claims come last: a model stuck repeating claims hits the reply
    # cap there, and a cut-off draft still has the sources and counterpoint its claims rely on.
    thesis: str
    sources: list[Source]
    counterpoint: str
    claims: list[Claim]
    pocs: list[POC] = []                   # validator requires 3+ only when outreach is on
    question_for_pocs: str = ""

    @model_validator(mode="after")
    def _short_ids(self):
        """A model sometimes uses a source's URL as its id. An article can't cite that ([https://...] reads as
        an outside link), so code gives such a source the next free "s<n>" and points its claims there; a
        claim that cites a source by its URL is pointed at that source's id too. Runs on every load, so a
        brief saved with URL ids is fixed when it is read back."""
        renamed = _shorten(self.sources)
        taken = {s.id for s in self.sources}
        by_url = {_bare(s.url): s.id for s in self.sources}
        for c in self.claims:
            c.source_ids = [renamed.get(i) or (i if i in taken else by_url.get(_bare(i), i)) for i in c.source_ids]
        return self

def _shorten(sources: list) -> dict[str, str]:
    """Give every source whose id isn't a short label (a URL, say) the next free "s<n>"; returns old -> new."""
    taken = {s.id for s in sources}
    renamed, n = {}, 0
    for s in sources:
        if SOURCE_ID.fullmatch(s.id):
            continue
        n += 1
        while f"s{n}" in taken:
            n += 1
        renamed[s.id] = s.id = f"s{n}"
        taken.add(s.id)
    return renamed

class SourcePick(BaseModel):
    """A brief's first step: the thesis and the sources it rests on. Code checks every source against its
    page before any claim is written, so claims only ever cite sources that passed."""
    thesis: str
    sources: list[Source]

    @model_validator(mode="after")
    def _short_ids(self):
        """Short ids from the start, so the claims written next cite what an article can cite."""
        _shorten(self.sources)
        return self

class SourceFix(BaseModel):
    """Replacements for the sources that failed the check; a failed source left out is dropped."""
    sources: list[Source]

class ClaimSet(BaseModel):
    """A brief's second step, citing only the sources that passed."""
    counterpoint: str
    claims: list[Claim]

class RejectedBrief(BaseModel):
    brief: ResearchBrief
    notes: str                             # the validator's verdict on this exact brief

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

class PlannedSection(BaseModel):
    heading: str
    point: str                             # what the section says, in one sentence
    claims: list[int] = []                 # numbers (from 1) of the brief's claims it uses
    counterpoint: bool = False             # the section that presents the case against the thesis

class ArticlePlan(BaseModel):
    """An article's first step. Each section is then written and checked on its own."""
    title: str
    dek: str
    sections: list[PlannedSection] = Field(min_length=2)

class ArticleDraft(BaseModel):
    """What the writer has so far: its plan, and the sections written and checked, in plan order."""
    plan: ArticlePlan
    sections: list[Section] = []

class RejectedArticle(BaseModel):
    article: Article
    notes: str                             # the article check's verdict on this exact article

Mission.model_rebuild()

# Inline citation in article text: [s1] or [s1, s2]. Shared by the checker and the renderer.
CITATION = re.compile(r"\[([A-Za-z0-9_.-]+(?:\s*,\s*[A-Za-z0-9_.-]+)*)\](?!\()")
URL_CITATION = re.compile(r"\[\s*(https?://[^\s,\]]+(?:\s*,\s*(?:https?://)?[^\s,\]]+)*)\s*\](?!\()")

def _bare(url: str) -> str:
    return url.strip().rstrip("/")

def cite_by_id(text: str, brief: "ResearchBrief") -> str:
    """Rewrite [https://a, https://b] to [s1, s2] when every entry is a brief source's URL or id. Anything
    else stays, so a link to a page outside the brief still fails the article check."""
    ids = {s.id for s in brief.sources}
    by_url = {_bare(s.url): s.id for s in brief.sources}
    def swap(m: re.Match) -> str:
        parts = [p.strip() for p in m.group(1).split(",")]
        new = [p if p in ids else by_url.get(_bare(p)) for p in parts]
        return f"[{', '.join(new)}]" if all(new) else m.group(0)
    return URL_CITATION.sub(swap, text)

# The required wording of a possibility. Shared by the checkers and the renderer.
POSSIBILITY = re.compile(r"\bit is possible that\b", re.I)

SITE_FOLDERS = {"about", "assets"}       # the published site's own folders: no mission may use them as its slug

def slugify(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words[:5])[:60].strip("-")

def words(text: str) -> set[str]:
    """Meaningful words (longer than 3 letters), for cheap topic overlap checks."""
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 3}

def cited_ids(text: str) -> list[str]:
    return [i.strip() for group in CITATION.findall(text) for i in group.split(",")]

class Evidence(BaseModel):
    """What the Research Assistant gathered for one research attempt: its whole search-and-read transcript
    (system prompt, tool calls, fetched pages) and, with outreach on, its code-checked contacts. The Principal
    Investigator writes the brief from it, possibly in a later pass."""
    messages: list[dict] = []
    pocs: list[POC] | None = None
    finished: bool = True                  # False: a stop (or a killed loop) cut the search short; it carries on
    attempt: int = 0                       # the research attempt it belongs to, so carrying on doesn't count anew
    seconds: float = 0                     # search time spent so far, out of the source finder's limit
