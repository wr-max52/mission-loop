from contracts import Mission, MissionIdea, ResearchBrief, Source, POC, Claim, OutreachRecord, Article, Section

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
