"""Human-readable output: brief.md and article.md for review, HTML for the static site."""
from datetime import datetime
from html import escape
from contracts import ResearchBrief, Article, CITATION, POSSIBILITY, cited_ids

FACTS_HEADING = "Facts (quotes verified against sources)"
UNCHECKED_FACTS_HEADING = "Claimed facts (this brief failed the source check)"
POSSIBILITIES_HEADING = "Possibilities (not verified)"
POSSIBILITY_LABEL = "Possibility, not verified"

def _claims_of(b: ResearchBrief, kind: str) -> list:
    return [c for c in b.claims if c.kind == kind]

def _has_possibility(a: Article) -> bool:
    return any(POSSIBILITY.search(p) for sec in a.sections for p in sec.paragraphs)

def brief_markdown(slug: str, b: ResearchBrief, checked: bool = True) -> str:
    claim_lines = lambda kind: [f"- {c.text} [{', '.join(c.source_ids)}]" for c in _claims_of(b, kind)]
    heading = FACTS_HEADING if checked else UNCHECKED_FACTS_HEADING
    lines = [f"# Brief: {slug}", "", f"**Thesis:** {b.thesis}", "", f"## {heading}"]
    lines += claim_lines("fact")
    if _claims_of(b, "possibility"):
        lines += ["", f"## {POSSIBILITIES_HEADING}"] + claim_lines("possibility")
    lines += ["", f"## Counterpoint", b.counterpoint, "", "## Sources"]
    lines += [f"- **{s.id}** ({s.stance}) [{s.title}]({s.url})\n  > {s.quote}" for s in b.sources]
    if b.pocs:
        lines += ["", "## Points of contact"]
        lines += [f"- {p.name}, {p.role}: {p.email} ([where found]({p.contact_source_url})). {p.why_them}"
                  for p in b.pocs]
        lines += ["", f"## Question for POCs", b.question_for_pocs]
    lines += [""]
    return "\n".join(lines)

def article_markdown(slug: str, b: ResearchBrief, a: Article) -> str:
    lines = ["---", f"slug: {slug}", f"title: {a.title}", f"dek: {a.dek}", "---", "",
             f"# {a.title}", "", f"*{a.dek}*", ""]
    for sec in a.sections:
        lines += [f"## {sec.heading}", ""]
        for p in sec.paragraphs:
            lines += [POSSIBILITY.sub(lambda m: f"**[{POSSIBILITY_LABEL}]** {m.group(0)}", p), ""]
    lines += ["## Sources", ""]
    lines += [f"- **{s.id}** ({s.stance}) [{s.title}]({s.url})\n  > {s.quote}" for s in b.sources]
    return "\n".join(lines) + "\n"

def _source_numbers(b: ResearchBrief, a: Article) -> dict[str, int]:
    """Number sources in order of first citation; uncited sources go last."""
    known = {s.id for s in b.sources}
    order: list[str] = []
    for sec in a.sections:
        for p in sec.paragraphs:
            for sid in cited_ids(p):
                if sid in known and sid not in order:
                    order.append(sid)
    order += [s.id for s in b.sources if s.id not in order]
    return {sid: n for n, sid in enumerate(order, 1)}

CSS = """
:root { --bg:#fbfaf7; --fg:#1d1d1f; --muted:#6b6b70; --rule:#e3e1dc; --accent:#8a2b2b; --quote:#f1efe9; }
:root[data-theme="dark"] { --bg:#151517; --fg:#ececee; --muted:#9a9aa0; --rule:#2c2c30;
                           --accent:#e08a8a; --quote:#1f1f23; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme]) { --bg:#151517; --fg:#ececee; --muted:#9a9aa0; --rule:#2c2c30;
                            --accent:#e08a8a; --quote:#1f1f23; }
}
* { box-sizing: border-box; }
html[data-theme="dark"] { color-scheme: dark; }
body { margin:0; background:var(--bg); color:var(--fg);
       font: 18px/1.65 Georgia, "Iowan Old Style", "Times New Roman", serif; }
main { max-width: 42rem; margin: 0 auto; padding: 2.5rem 1rem 4rem; }
a { color: var(--accent); }
header.site { font: 600 0.85rem/1 -apple-system, system-ui, sans-serif; letter-spacing: .08em;
              text-transform: uppercase; margin-bottom: 2rem; }
header.site a { color: var(--muted); text-decoration: none; }
h1 { font-size: 2.1rem; line-height: 1.2; margin: 0 0 .5rem; }
h2 { font-size: 1.3rem; margin: 2.2rem 0 .6rem; }
.dek { font-size: 1.2rem; color: var(--muted); margin: 0 0 .75rem; }
.meta { font: 0.85rem/1.4 -apple-system, system-ui, sans-serif; color: var(--muted);
        border-bottom: 1px solid var(--rule); padding-bottom: 1rem; margin-bottom: 1.5rem; }
sup a { text-decoration: none; font: 600 0.7rem -apple-system, system-ui, sans-serif; }
ol.sources { padding-left: 1.4rem; font-size: 0.95rem; }
ol.sources li { margin-bottom: 1rem; }
ol.sources blockquote { margin: .35rem 0 0; padding: .5rem .75rem; background: var(--quote);
                        border-left: 3px solid var(--rule); }
.stance { font: 600 0.7rem -apple-system, system-ui, sans-serif; text-transform: uppercase;
          letter-spacing: .06em; color: var(--muted); }
.note { font: 0.85rem/1.5 -apple-system, system-ui, sans-serif; color: var(--muted);
        border-top: 1px solid var(--rule); margin-top: 2.5rem; padding-top: 1rem; }
ul.index { list-style: none; padding: 0; }
ul.index li { border-bottom: 1px solid var(--rule); padding: 1.1rem 0; }
ul.index a.title { font-size: 1.25rem; font-weight: 600; text-decoration: none; color: var(--fg); }
ul.index p { margin: .3rem 0 0; color: var(--muted); }
.theme-toggle { display:inline-flex; align-items:center; gap:.35rem; padding:.3rem .55rem;
               border:1px solid var(--rule); border-radius:999px; background:var(--bg); color:var(--fg);
               font:600 0.8rem/1 -apple-system, system-ui, sans-serif; cursor:pointer; }
.theme-toggle:hover { border-color:var(--muted); }
.theme-toggle svg { width:1rem; height:1rem; fill:none; stroke:currentColor; stroke-width:2;
                    stroke-linecap:round; stroke-linejoin:round; display:none; }
html:not([data-theme="dark"]) .theme-toggle .moon { display:block; }
html[data-theme="dark"] .theme-toggle .sun { display:block; }
"""

# Sets the theme before first paint so a saved choice never flashes the wrong one, and
# flips it on click. Falls back to the OS preference until the human chooses for the first time.
THEME_INIT = """<script>
(function () {
  try {
    var theme = localStorage.getItem("theme") ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    document.documentElement.dataset.theme = theme;
  } catch (e) { document.documentElement.dataset.theme = "light"; }
  window.toggleTheme = function () {
    try {
      var theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = theme;
      localStorage.setItem("theme", theme);
    } catch (e) {}
  };
})();
</script>"""

THEME_TOGGLE = '<button type="button" class="theme-toggle" onclick="toggleTheme()" title="Toggle light / dark" aria-label="Toggle light / dark">' \
    '<svg class="sun" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/>' \
    '<path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M19 5l-1.5 1.5M6.5 17.5L5 19"/></svg>' \
    '<svg class="moon" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>' \
    "</button>"

def brief_html(b: ResearchBrief, checked: bool = True) -> str:
    """The brief as an HTML fragment (no page shell) for the review UI."""
    nums = {s.id: n for n, s in enumerate(b.sources, 1)}
    refs = lambda ids: "".join(f'<sup><a href="#src-{nums[i]}">[{nums[i]}]</a></sup>' for i in ids if i in nums)
    items = lambda kind: [f"<li>{escape(c.text)}{refs(c.source_ids)}</li>" for c in _claims_of(b, kind)]
    out = [f'<p class="thesis"><span class="stance">Thesis</span><br>{escape(b.thesis)}</p>',
           f"<h2>{FACTS_HEADING if checked else UNCHECKED_FACTS_HEADING}</h2><ul>"]
    out += items("fact") + ["</ul>"]
    if _claims_of(b, "possibility"):
        out += [f"<h2>{POSSIBILITIES_HEADING}</h2><ul>"] + items("possibility") + ["</ul>"]
    out += ["<h2>Counterpoint</h2>", f"<p>{escape(b.counterpoint)}</p>",
            '<h2>Sources</h2><ol class="sources">']
    out += [f'<li id="src-{nums[s.id]}"><span class="stance">{s.stance}</span><br>'
            f'<a href="{escape(s.url)}" target="_blank" rel="noopener">{escape(s.title)}</a>'
            f'<blockquote>{escape(s.quote)}</blockquote></li>' for s in b.sources]
    out.append("</ol>")
    if b.pocs:
        out.append("<h2>Points of contact</h2><ul>")
        out += [f"<li>{escape(p.name)}, {escape(p.role)}: {escape(p.email)}</li>" for p in b.pocs]
        out.append("</ul>")
    return "\n".join(out)

def _page(title: str, body: str, home: str) -> str:
    return (f"<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{escape(title)}</title>{THEME_INIT}<style>{CSS}</style></head>\n"
            f"<body><main><header class=\"site\"><a href=\"{home}\">Mission Loop</a>{THEME_TOGGLE}</header>\n"
            f"{body}\n</main></body></html>\n")

def article_html(b: ResearchBrief, a: Article, published_at: datetime | None, home: str = "../",
                 brief_checked: bool = True, article_checked: bool = True) -> str:
    """brief_checked / article_checked are False when a human pushed that work past a failed check;
    the page must then not claim the check passed."""
    nums = _source_numbers(b, a)

    def cite(match) -> str:
        ids = [i.strip() for i in match.group(1).split(",")]
        return "".join(f'<sup><a href="#src-{nums[i]}">[{nums[i]}]</a></sup>' for i in ids if i in nums)

    def label(match) -> str:
        return f'<span class="stance">{POSSIBILITY_LABEL}</span> {match.group(0)}'

    def para(text: str) -> str:
        return POSSIBILITY.sub(label, CITATION.sub(cite, escape(text, quote=False)))

    when = f"Published {published_at:%B %-d, %Y}" if published_at else "Preview: not published"
    failed = "" if brief_checked and article_checked else " · Failed automated checks"
    body = [f"<article><h1>{escape(a.title)}</h1>", f'<p class="dek">{escape(a.dek)}</p>',
            f'<p class="meta">{when} · {len(b.sources)} sources{failed}</p>']
    for sec in a.sections:
        body.append(f"<h2>{escape(sec.heading)}</h2>")
        body += [f"<p>{para(p)}</p>" for p in sec.paragraphs]
    body.append('<h2>Sources</h2><ol class="sources">')
    for s in sorted(b.sources, key=lambda s: nums[s.id]):
        body.append(f'<li id="src-{nums[s.id]}" value="{nums[s.id]}"><span class="stance">{s.stance}</span><br>'
                    f'<a href="{escape(s.url)}">{escape(s.title)}</a>'
                    f"<blockquote>{escape(s.quote)}</blockquote></li>")
    body.append("</ol>")
    sources_note = ("Every source above was fetched and its quote matched against the page by code. "
                    if brief_checked else
                    "The research failed the automated source check, so sources or quotes above may be wrong. ")
    article_note = ("Direct quotes and citations in the article were checked against those sources. "
                    if article_checked else
                    "The article failed the automated check of its quotes and citations. ")
    possibilities_note = ""
    if _has_possibility(a):
        possibilities_note = ("Passages labeled as possibilities are informed inferences, not verified facts: "
                              "their sources were checked, but the possibility itself was not confirmed. "
                              if brief_checked else
                              "Passages labeled as possibilities are informed inferences, not verified facts. ")
    body.append(f'<p class="note">{sources_note}{article_note}{possibilities_note}'
                "A human approved the research and reviewed this article before it was published.</p></article>")
    return _page(a.title, "\n".join(body), home)

def index_html(entries: list[dict]) -> str:
    """entries: dicts with slug, title, dek, published_at (ISO string), newest first."""
    items = [f'<li><a class="title" href="{escape(e["slug"])}/">{escape(e["title"])}</a>'
             f'<p>{escape(e["dek"])}</p><p class="meta" style="border:0;padding:0;margin:.3rem 0 0">'
             f'{datetime.fromisoformat(e["published_at"]):%B %-d, %Y}</p></li>' for e in entries]
    body = "<h1>Published</h1>" + (f'<ul class="index">{"".join(items)}</ul>' if items
                                   else "<p>Nothing published yet.</p>")
    return _page("Mission Loop", body, "./")
