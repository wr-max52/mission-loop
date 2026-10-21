"""Human-readable brief, written next to the data so you can review before approving."""
from contracts import ResearchBrief

def brief_markdown(slug: str, b: ResearchBrief) -> str:
    lines = [f"# Brief: {slug}", "", f"**Thesis:** {b.thesis}", "", "## Claims"]
    lines += [f"- {c.text} [{', '.join(c.source_ids)}]" for c in b.claims]
    lines += ["", f"## Counterpoint", b.counterpoint, "", "## Sources"]
    lines += [f"- **{s.id}** ({s.stance}) [{s.title}]({s.url})\n  > {s.quote}" for s in b.sources]
    lines += ["", "## Points of contact"]
    lines += [f"- {p.name}, {p.role}: {p.email} ([where found]({p.contact_source_url})). {p.why_them}"
              for p in b.pocs]
    lines += ["", f"## Question for POCs", b.question_for_pocs, ""]
    return "\n".join(lines)
