import re
from datetime import datetime
from enum import Enum
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator

class State(str, Enum):
    QUEUED = "QUEUED"
    RESEARCHING = "RESEARCHING"
    BRIEF_READY = "BRIEF_READY"            # waits for human approval
    OUTREACH = "OUTREACH"                  # the Outreach agent asks people a question (only while outreach is on)
    AWAITING_COMMENTS = "AWAITING_COMMENTS"  # waits for replies, up to the outreach timer
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
    wait_hours: float = 48                 # unused: the reply wait is settings.outreach_wait_hours()
    wait_until: datetime | None = None     # the reply wait ends then (or once every contact has answered)
    replies_checked_at: datetime | None = None   # when the channels were last asked for replies
    approved: bool = False
    reviewed: bool = False
    validator_notes: str = ""              # validator failures or human feedback
    rejected_brief: "RejectedBrief | None" = None  # last brief the validator failed; retries revise it
    rejected_article: "RejectedArticle | None" = None  # last article the check failed
    gathered: bool = False                 # the Research Assistant's sources are saved (artifact "evidence"); brief next
    search_paused: bool = False            # a stop cut the search short; it carries on as the same attempt
    interrupted_brief: "ResearchBrief | None" = None   # written out when stop was pressed; checked when the loop starts again
    interrupted_article: "Article | None" = None       # same for the writer
    checked_article: "Article | None" = None   # passed the article check; the Research Assistant rates it next
    article_rating: "ArticleRating | None" = None   # the rating of the article now at review (or published)
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
    by: str = "code"                       # "code", "human", "theorist" or "chat"
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

class ResearchBrief(BaseModel):
    # The model writes fields in this order. Claims come last: a model stuck repeating claims hits the reply
    # cap there, and a cut-off draft still has the sources and counterpoint its claims rely on.
    thesis: str
    sources: list[Source]
    counterpoint: str
    claims: list[Claim]

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
    """One person the Outreach agent asked (contacts.send writes it the moment the message is sent)."""
    channel: str                           # "email", "bluesky" or "1f916"
    address: str                           # their email address, Bluesky handle or 1F916 handle
    name: str
    topic: str
    question: str
    message: str                           # exactly what was sent
    sent_at: datetime
    where: str = ""                        # where it can be read: the Bluesky post, the 1F916 comment, "email"
    ref: str = ""                          # how replies are found: the post's at:// uri, "post:comment", Message-ID
    replies: list[dict] = []               # {"id", "from", "text", "at", "is_comment": True}: the contact's answers
    thanked_at: datetime | None = None     # when contacts.thank sent them THANKS, once, after their first answer
    thanks_where: str = ""                 # where that thank-you can be read: a Bluesky post, a 1F916 comment, "email"

class Section(BaseModel):
    heading: str
    paragraphs: list[str]                  # cite sources inline by id, e.g. "... in 2023 [s1, s2]."

class Article(BaseModel):
    title: str
    dek: str                               # one-sentence summary shown under the title
    sections: list[Section] = Field(min_length=2)
    claim: str = ""                        # the big idea in plain words, cited, shown in a box at the top
                                           # (empty for articles written before it existed: no box)

class PlannedSection(BaseModel):
    heading: str
    point: str                             # what the section says, in one sentence
    claims: list[int] = []                 # numbers (from 1) of the brief's claims it uses
    counterpoint: bool = False             # the section that presents the case against the thesis

class ArticlePlan(BaseModel):
    """An article's first step. Each section is then written and checked on its own."""
    title: str
    dek: str
    claim: str = ""                        # the box at the top: who did what and why it matters, cited
    sections: list[PlannedSection] = Field(min_length=2)

class ArticleDraft(BaseModel):
    """What the writer has so far: its plan, and the sections written and checked, in plan order."""
    plan: ArticlePlan
    sections: list[Section] = []

RATING_FACTORS = {                         # ArticleRating field -> what it rates, as shown to people
    "logic": "Strength of the claim and its logic",
    "intrigue": "Intrigue",
    "sources": "Credibility of the sources",
}
RATING_SCALE = 10                          # each factor is 1 to this
RATING_FAIL_AT = 20                        # a total of this or less sends the article back to the writer

class RatingReply(BaseModel):
    """What the rater writes: one score per factor (RATING_FACTORS), each after its reason, so the model reasons
    before it scores. Scores are clamped to 1..RATING_SCALE."""
    logic_why: str = ""
    logic: int = 1
    intrigue_why: str = ""
    intrigue: int = 1
    sources_why: str = ""
    sources: int = 1

    @field_validator("logic", "intrigue", "sources", mode="before")
    @classmethod
    def _clamp(cls, v):
        try:
            v = round(float(v))
        except (TypeError, ValueError):
            v = 1
        return max(1, min(RATING_SCALE, v))

class ArticleRating(RatingReply):
    """The Research Assistant's rating of an article that passed the article check. Code decides with it: a total
    of RATING_FAIL_AT or less goes back to the writer. error is set instead when the rater couldn't rate it; the
    article then goes to review unrated (warn, don't block)."""
    error: str = ""

    @property
    def total(self) -> int:
        return self.logic + self.intrigue + self.sources

    @property
    def passed(self) -> bool:
        return bool(self.error) or self.total > RATING_FAIL_AT

    def lines(self) -> list[str]:
        """One line per factor: "Intrigue 6/10: why"."""
        return [f"{name} {getattr(self, key)}/{RATING_SCALE}: {' '.join(getattr(self, key + '_why').split())}".rstrip(": ")
                for key, name in RATING_FACTORS.items()]

    def summary(self) -> str:
        if self.error:
            return f"Not rated: {self.error}"
        return f"Rated {self.total}/{RATING_SCALE * len(RATING_FACTORS)}"

    def notes(self) -> str:
        """The rating as notes: the writer's, the mission page's and article.rejected.md's."""
        if self.error:
            return self.summary()
        verdict = "passed" if self.passed else f"{RATING_FAIL_AT} or less is sent back to the writer"
        return f"{self.summary()} ({verdict}):\n" + "\n".join(f"- {l}" for l in self.lines())

class RejectedArticle(BaseModel):
    article: Article
    notes: str                             # the article check's verdict on this exact article
    rating: ArticleRating | None = None    # set when it passed the check but its rating sent it back

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

# Key words an article marks in **bold** (who did what). Shared by the writer, the checker and the renderer.
BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")

def tidy_bold(text: str) -> str:
    """Bold markers that don't pair up would show as stray asterisks: drop them all."""
    return text if "**" not in BOLD.sub("", text) else text.replace("**", "")

def unbold(text: str) -> str:
    return text.replace("**", "")

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
    (system prompt, tool calls, fetched pages). The Principal Investigator writes the brief from it, possibly in a
    later pass."""
    messages: list[dict] = []
    finished: bool = True                  # False: a stop (or a killed loop) cut the search short; it carries on
    attempt: int = 0                       # the research attempt it belongs to, so carrying on doesn't count anew
    seconds: float = 0                     # search time spent so far, out of the source finder's limit
