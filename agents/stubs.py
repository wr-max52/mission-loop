from contracts import Evidence, Mission, MissionIdea, ResearchBrief, Source, POC, Claim, OutreachRecord, Article, Section

def theorize(now=None):
    """Sees a pattern, changes nothing: the line stays, no epics, no mission."""
    import store
    from agents.theorist import Theory
    line = store.get_inspiration()
    return Theory(patterns=["Stub pattern"], inspiration=line, why="stub: nothing new", based_on=line)

def tidy_board(now=None):
    """A board turn that leaves the board as it is."""
    from agents.theorist import BoardTidy
    return BoardTidy(why="stub: the board reads well as it is")

def find_missions(inspiration: str, topics: list[str], now=None, epics=None) -> list[MissionIdea]:
    return [MissionIdea(topic=f"Stub lead {len(topics) + 1} from {inspiration}",
                        objective="Stub objective", source_url="https://a.example.com/lead")]

def research(m: Mission, now=None) -> ResearchBrief:
    return ResearchBrief(
        thesis=f"Stub thesis about {m.topic}",
        claims=[Claim(text="Stub claim", source_ids=["s1", "s2"])],
        counterpoint="Stub counterpoint answering s4",
        sources=[
            Source(id="s1", url="https://a.example.com", title="A", quote="stub", stance="supports"),
            Source(id="s2", url="https://b.example.com", title="B", quote="stub", stance="supports"),
            Source(id="s3", url="https://c.example.com", title="C", quote="stub", stance="supports"),
            Source(id="s4", url="https://d.example.com", title="D", quote="stub", stance="contradicts"),
        ],
        pocs=[POC(name=f"Person {i}", role="Analyst", email=f"poc{i}@example.com",
                  contact_source_url="https://a.example.com/team", why_them="stub")
              for i in range(1, 4)],
        question_for_pocs="What is your take on this thesis?",
    )

def gather(m: Mission, now=None, resume=None) -> Evidence:
    return Evidence(messages=[{"role": "system", "content": f"stub sources for {m.topic}"}])

def write_brief(m: Mission, evidence: Evidence, now=None) -> ResearchBrief:
    return research(m, now)

def validate(m: Mission, brief: ResearchBrief) -> tuple[bool, str]:
    if "FAIL" in m.topic:
        return False, "stub: forced failure"
    return True, ""

def interview(m: Mission, brief: ResearchBrief, now) -> list[OutreachRecord]:
    return [OutreachRecord(poc_email=p.email, gmail_thread_id=f"stub-{m.slug}-{i}", sent_at=now)
            for i, p in enumerate(brief.pocs)]

def no_interview(m: Mission, brief: ResearchBrief, now) -> list[OutreachRecord]:
    """Outreach is off: nobody is emailed, so the orchestrator skips the comment wait."""
    return []

def check_inbox(m: Mission, records: list[OutreachRecord], now) -> list[OutreachRecord]:
    return records                         # milestone 4: poll Gmail threads here

def write(m: Mission, brief: ResearchBrief, records: list[OutreachRecord]) -> Article:
    comments = [r for rec in records for r in rec.replies if r.get("is_comment")]
    return Article(
        title=brief.thesis,
        dek=f"Stub article about {m.topic}",
        claim=f"**{brief.claims[0].text}** [s1].",
        sections=[
            Section(heading="Finding", paragraphs=[f"{brief.claims[0].text} [s1, s2]."]),
            Section(heading="The other side", paragraphs=[f"{brief.counterpoint} [s4]. "
                                                          f"Comments received: {len(comments)} [s1]."]),
        ],
    )

def check_article(m: Mission, brief: ResearchBrief, article: Article, records=None) -> tuple[bool, str]:
    if "BADARTICLE" in m.topic:
        return False, "stub: forced article failure"
    return True, ""

def chat_models() -> list[str]:
    return ["stub"]

def chat_catalog() -> list[dict]:
    return [{"name": "stub", "family": "stub", "crew": "assistant"}]

def chat_web() -> bool:
    return False

def chat(question: str, history: list[dict] | None = None, model: str | None = None) -> str:
    """No model: asks the real loop MCP server for its status, so stub runs still go through MCP."""
    import asyncio
    from agents.assistant import CONFIG
    from agents.toolbox import Toolbox

    async def ask() -> str:
        async with Toolbox(["loop"], config_path=CONFIG) as tb:
            return await tb.call("loop_status", {})
    return f"Stub answer to: {question}\n" + asyncio.run(ask())

STUB_COLORS = {"red": "#c0392b", "blue": "#3d6a9e", "green": "#2f9e44", "pink": "#e84393", "purple": "#7b5ea7",
               "black": "#17110e", "white": "#f2f0f7", "yellow": "#f1c40f", "orange": "#e67e22", "brown": "#6e4527"}

def avatar_models() -> list[dict]:
    return [{"name": "stub", "vision": False}]

def edit_avatar(spec: dict, message: str, look: dict | None = None, model: str | None = None,
                image: str | None = None, errors: list[str] | None = None, catalog: dict | None = None) -> dict:
    """No model: the first color word named becomes the hair color, so stub runs can try the studio's chat."""
    from agents.avatar import REFUSAL, refuses
    if refuses(message):
        return {"reply": REFUSAL, "refused": True, "spec": spec, "change": {}, "model": "stub", "saw_image": False}
    words = [w for w in STUB_COLORS if w in (message or "").lower()]
    if not words:
        return {"reply": "Stub designer: name a color and I'll dye the hair.", "refused": False, "spec": spec,
                "change": {}, "model": "stub", "saw_image": False}
    change = {"hair": STUB_COLORS[words[0]]}
    return {"reply": f"Stub designer: {words[0]} hair it is.", "refused": False, "spec": dict(spec, **change),
            "change": change, "model": "stub", "saw_image": False}
