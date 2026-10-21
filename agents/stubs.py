from pathlib import Path
from contracts import Mission, ResearchBrief, Source, POC, OutreachRecord

def research(m: Mission) -> ResearchBrief:
    return ResearchBrief(
        thesis=f"Stub thesis about {m.topic}",
        claims=[{"text": "Stub claim", "source_ids": ["s1", "s2"]}],
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

def check_inbox(m: Mission, records: list[OutreachRecord], now) -> list[OutreachRecord]:
    return records                         # milestone 4: poll Gmail threads here

def write(m: Mission, brief: ResearchBrief, records: list[OutreachRecord]) -> Path:
    comments = [r for rec in records for r in rec.replies if r.get("is_comment")]
    out = Path("missions") / m.slug / "article.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        f"---\ntitle: {brief.thesis}\nsources: {[s.id for s in brief.sources]}\n---\n\n"
        f"# {brief.thesis}\n\n{brief.claims[0]['text']}\n\nComments received: {len(comments)}\n")
    return out
