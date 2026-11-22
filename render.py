"""Human-readable output: brief.md and article.md for review, HTML for the static site."""
import json
from datetime import datetime
from email.utils import format_datetime
from html import escape
from urllib.parse import quote
from xml.etree import ElementTree as ET
import flowchart, wayback
from icons import icon
from contracts import ResearchBrief, Article, CITATION, POSSIBILITY, cited_ids

FACTS_HEADING = "Facts (quotes verified against sources)"
UNCHECKED_FACTS_HEADING = "Claimed facts (this brief failed the source check)"
POSSIBILITIES_HEADING = "Possibilities (not verified)"
POSSIBILITY_LABEL = "Possibility, not verified"

def _claims_of(b: ResearchBrief, kind: str) -> list:
    return [c for c in b.claims if c.kind == kind]

def _archived(url: str) -> str:
    """Readers should know when a source is the Wayback Machine's copy, and from when."""
    day = wayback.day_of(url)
    return f' <span class="stance">(archived copy, {day})</span>' if day else ""

def _has_possibility(a: Article) -> bool:
    return any(POSSIBILITY.search(p) for sec in a.sections for p in sec.paragraphs)

UNSOURCED = " (No source found makes this case.)"

def _unsourced(b: ResearchBrief) -> str:
    """A counterpoint with no contradicting source behind it is a skeptic's objection; say so."""
    return "" if any(s.stance == "contradicts" for s in b.sources) else UNSOURCED

def brief_markdown(slug: str, b: ResearchBrief, checked: bool = True) -> str:
    claim_lines = lambda kind: [f"- {c.text} [{', '.join(c.source_ids)}]" for c in _claims_of(b, kind)]
    heading = FACTS_HEADING if checked else UNCHECKED_FACTS_HEADING
    lines = [f"# Brief: {slug}", "", f"**Thesis:** {b.thesis}", "", f"## {heading}"]
    lines += claim_lines("fact")
    if _claims_of(b, "possibility"):
        lines += ["", f"## {POSSIBILITIES_HEADING}"] + claim_lines("possibility")
    lines += ["", f"## Counterpoint", b.counterpoint + _unsourced(b), "", "## Sources"]
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
                            --accent:#e08a8a; --quote:#1f1f23; color-scheme: dark; }
}
* { box-sizing: border-box; }
html[data-theme="dark"] { color-scheme: dark; }
html[data-theme="light"] { color-scheme: light; }
body { margin:0; background:var(--bg); color:var(--fg);
       font: 18px/1.65 Georgia, "Iowan Old Style", "Times New Roman", serif; }
main { max-width: 42rem; margin: 0 auto; padding: 2.5rem 1rem 4rem; }
a { color: var(--accent); }
header.site { font: 600 0.85rem/1 -apple-system, system-ui, sans-serif; letter-spacing: .08em;
              text-transform: uppercase; margin-bottom: 2rem; display: flex; align-items: center; gap: 1rem;
              flex-wrap: wrap; }
header.site a { color: var(--muted); text-decoration: none; }
header.site a:hover, header.site a[aria-current] { color: var(--fg); }
header.site .brand { margin-right: auto; }
.back { display: inline-block; font: 600 0.85rem/1 -apple-system, system-ui, sans-serif; color: var(--muted);
        text-decoration: none; margin-bottom: 1.2rem; }
.back:hover { color: var(--fg); }
nav.pager { display: flex; justify-content: space-between; gap: 1rem; margin-top: 2rem;
            font: 0.9rem/1.4 -apple-system, system-ui, sans-serif; }
nav.pager a { text-decoration: none; max-width: 48%; }
nav.pager .older { margin-left: auto; text-align: right; }
nav.pager small { display: block; color: var(--muted); font-size: .75rem; text-transform: uppercase; letter-spacing: .06em; }
nav.tags { display: flex; flex-wrap: wrap; gap: .4rem; margin: 0 0 .5rem; }
nav.tags a { font: 600 0.8rem/1.3 -apple-system, system-ui, sans-serif; padding: .25rem .7rem; border-radius: 999px;
             border: 1px solid var(--rule); color: var(--muted); text-decoration: none; }
nav.tags a:hover { border-color: var(--muted); color: var(--fg); }
nav.tags a.on { background: var(--fg); border-color: var(--fg); color: var(--bg); }
nav.tags span { opacity: .65; font-weight: 400; margin-left: .2rem; }
a.epic { text-decoration: none; }
a.epic:hover { border-color: var(--muted); color: var(--fg); }
h1 { font-size: 2.1rem; line-height: 1.2; margin: 0 0 .5rem; }
h2 { font-size: 1.3rem; margin: 2.2rem 0 .6rem; }
.dek { font-size: 1.2rem; color: var(--muted); margin: 0 0 .75rem; }
.meta { font: 0.85rem/1.4 -apple-system, system-ui, sans-serif; color: var(--muted);
        border-bottom: 1px solid var(--rule); padding-bottom: 1rem; margin-bottom: 1.5rem; }
sup a { text-decoration: none; font: 600 0.7rem -apple-system, system-ui, sans-serif; }
ol.sources { padding-left: 1.4rem; font-size: 0.95rem; }
ul.connected { padding-left: 1.2rem; } ul.connected li { margin-bottom: .5rem; }
ul.connected .why { color: var(--muted); font-size: .9rem; }
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
.epic { display:inline-block; font: 600 0.72rem/1.3 -apple-system, system-ui, sans-serif; white-space:nowrap;
        padding: .12rem .5rem; border-radius: 999px; border: 1px solid var(--rule); color: var(--muted);
        vertical-align: middle; }
.epic.proposed { border-style: dashed; }
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
           f"<h2>{icon('check')}{FACTS_HEADING if checked else UNCHECKED_FACTS_HEADING}</h2><ul>"]
    out += items("fact") + ["</ul>"]
    if _claims_of(b, "possibility"):
        out += [f"<h2>{icon('bulb')}{POSSIBILITIES_HEADING}</h2><ul>"] + items("possibility") + ["</ul>"]
    out += [f"<h2>{icon('scale')}Counterpoint</h2>", f"<p>{escape(b.counterpoint)}{escape(_unsourced(b))}</p>",
            f'<h2>{icon("list")}Sources</h2><ol class="sources">']
    out += [f'<li id="src-{nums[s.id]}"><span class="stance">{s.stance}</span><br>'
            f'<a href="{escape(s.url)}" target="_blank" rel="noopener">{escape(s.title)}</a>{_archived(s.url)}'
            f'<blockquote>{escape(s.quote)}</blockquote></li>' for s in b.sources]
    out.append("</ol>")
    if b.pocs:
        out.append(f"<h2>{icon('person')}Points of contact</h2><ul>")
        out += [f"<li>{escape(p.name)}, {escape(p.role)}: {escape(p.email)}</li>" for p in b.pocs]
        out.append("</ul>")
    return "\n".join(out)

def _page(title: str, body: str, home: str, head: str = "", nav: str | None = None) -> str:
    """nav: which site page this is ("articles", "about", or "" for an article); None leaves the links out,
    for an article preview that isn't on the site yet."""
    links = ""
    if nav is not None:
        cur = lambda name: ' aria-current="page"' if nav == name else ""
        links = (f'<a href="{home}"{cur("articles")}>Articles</a>'
                 f'<a href="{home}about/"{cur("about")}>About</a>')
    return (f"<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{escape(title)}</title>{head}{THEME_INIT}<style>{CSS}</style></head>\n"
            f"<body><main><header class=\"site\"><a class=\"brand\" href=\"{home}\">Mission Loop</a>{links}"
            f"{THEME_TOGGLE}</header>\n{body}\n</main></body></html>\n")

def epic_pill(title: str, proposed: bool = False, href: str = "") -> str:
    """href makes it a link: on the site, to the article list filtered by that tag."""
    if not title:
        return ""
    if proposed:
        return f'<span class="epic proposed" title="Proposed epic">{escape(title)}?</span>'
    if href:
        return f'<a class="epic" href="{escape(href)}" title="Every article tagged {escape(title)}">{escape(title)}</a>'
    return f'<span class="epic">{escape(title)}</span>'

def tag_href(home: str, tag: str) -> str:
    """The article list showing only one tag (an epic is called a tag on the site)."""
    return f"{home}?tag={quote(tag)}"

def article_html(b: ResearchBrief, a: Article, published_at: datetime | None, home: str = "../",
                 brief_checked: bool = True, article_checked: bool = True, epic: str = "", slug: str = "",
                 connected: list[tuple[str, str, str]] = ()) -> str:
    """brief_checked / article_checked are False when a human pushed that work past a failed check;
    the page must then not claim the check passed. With a slug the page is on the site: it gets the site's
    links, a way back to every article and the newer and older articles (read from the feed, so they stay
    current as more are published); without one it is a preview. connected: (slug, title, why) for the
    published articles linked to it, listed on the site page."""
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
    pill = epic_pill(epic, href=tag_href(home, epic) if slug else "")
    back = f'<a class="back" href="{home}">&larr; All articles</a>' if slug else ""
    body = [f"{back}<article><h1>{escape(a.title)}</h1>", f'<p class="dek">{escape(a.dek)}</p>',
            f'<p class="meta">{pill + " " if epic else ""}{when} · {len(b.sources)} sources{failed}</p>']
    for sec in a.sections:
        body.append(f"<h2>{escape(sec.heading)}</h2>")
        body += [f"<p>{para(p)}</p>" for p in sec.paragraphs]
    body.append('<h2>Sources</h2><ol class="sources">')
    for s in sorted(b.sources, key=lambda s: nums[s.id]):
        body.append(f'<li id="src-{nums[s.id]}" value="{nums[s.id]}"><span class="stance">{s.stance}</span><br>'
                    f'<a href="{escape(s.url)}">{escape(s.title)}</a>{_archived(s.url)}'
                    f"<blockquote>{escape(s.quote)}</blockquote></li>")
    body.append("</ol>")
    body.append(f'<p class="note">{check_note(a, brief_checked, article_checked)}</p>')
    if slug and connected:
        items = "".join(f'<li><a href="{home}{escape(s)}/">{escape(t)}</a>'
                        + (f'<br><span class="why">{escape(w)}</span>' if w else "") + "</li>" for s, t, w in connected)
        body.append(f'<h2>Connected articles</h2><ul class="connected">{items}</ul>')
    body.append("</article>")
    if slug:
        body.append(f'<nav class="pager" data-slug="{escape(slug)}" data-feed="{home}feed.json" aria-label="More articles">'
                    f'</nav>{PAGER_JS}')
    return _page(a.title, "\n".join(body), home, nav="" if slug else None)

# The newer and older article, from the feed (newest first), so a page written once still links to
# whatever was published after it. Without the feed (a page opened straight from disk) it shows nothing.
PAGER_JS = """<script>
(function () {
  var nav = document.querySelector("nav.pager");
  if (!nav || !window.fetch) return;
  fetch(nav.dataset.feed).then(function (r) { return r.json(); }).then(function (feed) {
    var items = feed.items || [], at = items.findIndex(function (e) { return e.id === nav.dataset.slug; });
    if (at < 0) return;
    function link(e, cls, label) {
      if (!e) return;
      var a = document.createElement("a"), small = document.createElement("small");
      a.className = cls; a.href = "../" + encodeURIComponent(e.id) + "/";
      small.textContent = label; a.appendChild(small); a.appendChild(document.createTextNode(e.title));
      nav.appendChild(a);
    }
    link(items[at - 1], "newer", "\u2190 Newer");
    link(items[at + 1], "older", "Older \u2192");
  }).catch(function () {});
})();
</script>"""

def check_note(a: Article, brief_checked: bool = True, article_checked: bool = True) -> str:
    """What was and wasn't checked, for the foot of every published copy of an article."""
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
    return (f"{sources_note}{article_note}{possibilities_note}"
            "A human approved the research and reviewed this article before it was published.")

def article_text(b: ResearchBrief, a: Article, brief_checked: bool = True, article_checked: bool = True) -> str:
    """The article as plain text for channels that take a text post. Citations become [1], [2] with the
    same numbers as the site, and the numbered sources follow the text. No length limit here."""
    nums = _source_numbers(b, a)

    def cite(match) -> str:
        ids = [i.strip() for i in match.group(1).split(",")]
        return "".join(f"[{nums[i]}]" for i in ids if i in nums)

    def para(text: str) -> str:
        return POSSIBILITY.sub(lambda m: f"[{POSSIBILITY_LABEL}] {m.group(0)}", CITATION.sub(cite, text))

    lines = [a.dek, ""]
    if not (brief_checked and article_checked):
        lines += ["[Failed automated checks: see the note at the end.]", ""]
    for sec in a.sections:
        lines += [f"## {sec.heading}", ""]
        for p in sec.paragraphs:
            lines += [para(p), ""]
    lines += ["## Sources", ""]
    for s in sorted(b.sources, key=lambda s: nums[s.id]):
        lines += [f"[{nums[s.id]}] ({s.stance}) {s.title}: {s.url}", f'    "{s.quote}"']
    lines += ["", check_note(a, brief_checked, article_checked)]
    return "\n".join(lines)

def index_html(entries: list[dict]) -> str:
    """entries: dicts with slug, title, dek, published_at (ISO string) and maybe epic, newest first.
    Each epic is a tag: the row of tags above the list filters it (?tag=, so an article's tag links here)."""
    items = [f'<li data-tag="{escape(e.get("epic", ""))}"><a class="title" href="{escape(e["slug"])}/">{escape(e["title"])}</a>'
             f'<p>{escape(e["dek"])}</p><p class="meta" style="border:0;padding:0;margin:.3rem 0 0">'
             f'{epic_pill(e["epic"], href=tag_href("./", e["epic"])) + " " if e.get("epic") else ""}'
             f'{datetime.fromisoformat(e["published_at"]):%B %-d, %Y}</p></li>' for e in entries]
    counts: dict[str, int] = {}
    for e in entries:
        if e.get("epic"):
            counts[e["epic"]] = counts.get(e["epic"], 0) + 1
    tags = ""
    if counts:
        chips = [f'<a href="./" data-tag="" class="on">All<span>{len(entries)}</span></a>']
        chips += [f'<a href="{escape(tag_href("./", t))}" data-tag="{escape(t)}">{escape(t)}<span>{n}</span></a>'
                  for t, n in sorted(counts.items(), key=lambda kv: kv[0].lower())]
        tags = f'<nav class="tags" aria-label="Filter by tag">{"".join(chips)}</nav>{TAGS_JS}'
    body = "<h1>Published</h1>" + tags + (f'<ul class="index">{"".join(items)}</ul>' if items
                                          else "<p>Nothing published yet.</p>")
    body += '<p class="meta">Subscribe: <a href="feed.xml">RSS</a> · <a href="feed.json">JSON Feed</a></p>'
    head = ('<link rel="alternate" type="application/rss+xml" title="Mission Loop" href="feed.xml">'
            '<link rel="alternate" type="application/feed+json" title="Mission Loop" href="feed.json">')
    return _page("Mission Loop", body, "./", head, nav="articles")

# Shows only the articles with the chosen tag, and keeps it in the address (?tag=) so it can be linked to.
TAGS_JS = """<script>
(function () {
  var nav = document.querySelector("nav.tags");
  function show(tag) {
    nav.querySelectorAll("a").forEach(function (a) { a.classList.toggle("on", a.dataset.tag === tag); });
    document.querySelectorAll("ul.index li").forEach(function (li) { li.hidden = !!tag && li.dataset.tag !== tag; });
  }
  function pick(tag) {
    try { history.replaceState(null, "", tag ? "?tag=" + encodeURIComponent(tag) : location.pathname); } catch (e) {}
    show(tag);
  }
  document.addEventListener("click", function (ev) {
    var a = ev.target.closest && ev.target.closest("nav.tags a, ul.index a.epic");
    if (!a) return;
    ev.preventDefault();
    var tag = a.dataset.tag !== undefined ? a.dataset.tag : a.textContent;
    pick(nav.querySelector("a.on") && nav.querySelector("a.on").dataset.tag === tag && tag ? "" : tag);
  });
  var tag = new URLSearchParams(location.search).get("tag") || "";
  if (![].some.call(nav.querySelectorAll("a"), function (a) { return a.dataset.tag === tag; })) tag = "";
  show(tag);
})();
</script>"""

# ---------- the About page: the pipeline, playing, and who does what ----------

ABOUT_CSS = """
.flow, .legend, .lab-floor { font-family: -apple-system, system-ui, sans-serif; }
.flow { margin: 1.5rem 0 .5rem; width: min(56rem, 100vw - 2rem); margin-left: calc(50% - min(28rem, 50vw - 1rem)); }
.flow .now { font-size: .85rem; line-height: 1.5; color: var(--muted); min-height: 4.6em; }
.flow .now strong { color: var(--fg); }
.who { font-size: .72rem; font-weight: 700; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }
.legend { font-size: .8rem; color: var(--muted); margin: 0 0 2rem; }
dl.agents dt { font-weight: 700; margin-top: 1.1rem; }
dl.agents dt small { font: 600 .72rem -apple-system, system-ui, sans-serif; text-transform: uppercase;
                     letter-spacing: .05em; color: var(--muted); margin-left: .35rem; }
dl.agents dd { margin: .2rem 0 0; }
.lab-floor { display: flex; gap: 1rem; margin-top: 1.5rem; }
.lab-floor .card { flex: 1; min-width: 0; position: relative; height: 150px;
                   background: linear-gradient(var(--rule), var(--rule)) no-repeat 0 calc(100% - 8px) / 100% 2px; }
.lab-floor canvas.lab-stage { bottom: 0; height: 150px; }
"""
ABOUT_FLOW = flowchart.CSS + ABOUT_CSS

# (name, who runs it, what it does), in the order the strip shows them
AGENT_NOTES = [
    ("Theorist", "Principal Investigator",
     "Looks across every mission and published article for patterns inside each tag and themes emerging between "
     "them, then sharpens the desk's guiding questions and connects the dots: it links missions that bear on each "
     "other or on a published article, and proposes combining missions that are really one question. Now and then "
     "it proposes a new tag or one new mission."),
    ("Mission finder", "both models",
     "Searches around those guiding questions from a couple of unexpected angles and proposes up to three new "
     "missions, each resting on a page it actually found."),
    ("Source finder", "Research Assistant",
     "A small, fast model that does the digging: it searches, reads pages, checks the Wayback Machine for pages "
     "that changed or vanished, and gathers evidence for and against the mission's thesis."),
    ("Brief writer", "Principal Investigator",
     "A larger model turns that evidence into a brief: a checkable thesis, facts that quote their sources, "
     "possibilities clearly marked as unverified, and the strongest case against."),
    ("Checks", "code, no model",
     "The source check fetches every cited page and confirms each quote is really on it; the article check "
     "confirms the article's quotes and citations match the brief. A failure goes back for another try, and "
     "after three the mission is set aside for a human."),
    ("Reviews", "a human",
     "A person approves every brief or sends it back with notes, and reads every article before it is published."),
    ("Article writer", "Principal Investigator",
     "Writes the article from the approved brief, labeling each possibility \u201cPossibility, not verified\u201d."),
    ("Publisher", "code, no model",
     "Writes the article to this site, updates the feeds and passes it on to the other places it appears."),
]

# Researchers on the About page drop by for a short visit and always say hello (assets/researchers.js).
ABOUT_PACE = '<script>window.RESEARCHERS_PACE = {life: [7, 13], gap: [4, 9], first: [1, 4], entry: [3, 7], greet: 1};</script>'

def english_track() -> str:
    """The strip as the UI draws it, but always in English: the published site isn't translated."""
    import i18n
    with i18n.english():
        return flowchart.track({}, {}, {})

def about_html() -> str:
    """site/about/index.html: the pipeline strip playing a made-up run (assets/about.js drives it through
    assets/flow.js), what each agent does, and a floor where a researcher or two drops by."""
    strip = (f'<section id="flow" class="flow" data-demo data-working="[]" aria-label="The pipeline, playing an example run">'
             f'<div class="flow-scroll"><div class="flow-track">{english_track()}</div></div>'
             f'<div class="now"></div></section>')
    agents = "".join(f"<dt>{escape(name)}<small>{escape(who)}</small></dt><dd>{escape(text)}</dd>"
                     for name, who, text in AGENT_NOTES)
    body = ("<h1>About</h1>"
            "<p>Mission Loop is a small research desk run by AI agents, with a human editor. Every article starts "
            "as a mission, a question worth answering, and moves along the line below, turning at each review. Code checks "
            "the agents' work at each step, and nothing is published until a person has approved the research "
            "and read the article.</p>"
            f"{strip}"
            '<p class="legend">Green: working now. Striped blocks: checks done by code. Dashed: a human review.</p>'
            "<h2>Who does what</h2>"
            "<p>Two models share the work. The Research Assistant, a small model, searches and reads; the "
            "Principal Investigator, a larger one, writes and decides. They run side by side, so one mission's "
            "search goes on while another's article is written.</p>"
            f'<dl class="agents">{agents}</dl>'
            '<div class="lab-floor" aria-hidden="true">'
            '<div class="card" id="lab-left" data-lab="busy"></div><div class="card" id="lab-right" data-lab="busy"></div></div>')
    head = (f"<style>{ABOUT_FLOW}</style>{ABOUT_PACE}"
            '<script src="../assets/researchers.js" defer></script>'
            '<script src="../assets/flow.js" defer></script>'
            '<script src="../assets/about.js" defer></script>')
    return _page("About · Mission Loop", body, "../", head, nav="about")

# ---------- feeds: the same entries as the index, with full text ----------

FEED_TITLE = "Mission Loop"
FEED_DESCRIPTION = "Every article Mission Loop has published, with its sources."

def _base(site_url: str) -> str:
    return site_url.rstrip("/") + "/" if site_url else ""

def _text_html(text: str) -> str:
    """Plain article text (render.article_text) as simple HTML for feed readers."""
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    return "".join(f"<h2>{escape(b[3:])}</h2>" if b.startswith("## ") else
                   f"<p>{escape(b).replace(chr(10), '<br>')}</p>" for b in blocks)

def feed_json(entries: list[dict], site_url: str = "") -> str:
    """JSON Feed 1.1. Ids are the slugs, so they stay the same when a site URL is set later.
    Without a site URL, links are relative to the feed file."""
    base = _base(site_url)
    feed = {"version": "https://jsonfeed.org/version/1.1", "title": FEED_TITLE, "description": FEED_DESCRIPTION}
    if base:
        feed |= {"home_page_url": base, "feed_url": base + "feed.json"}
    feed["items"] = [{"id": e["slug"], "url": f'{base}{e["slug"]}/', "title": e["title"], "summary": e["dek"],
                      "content_text": e.get("text") or e["dek"], "date_published": e["published_at"],
                      **({"tags": [e["epic"]]} if e.get("epic") else {})} for e in entries]
    return json.dumps(feed, indent=2, ensure_ascii=False) + "\n"

def feed_xml(entries: list[dict], site_url: str = "") -> str:
    """RSS 2.0 with the full text in content:encoded. Same ids and links as feed_json."""
    base, content = _base(site_url), "http://purl.org/rss/1.0/modules/content/"
    ET.register_namespace("content", content)
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    for tag, value in (("title", FEED_TITLE), ("link", base or "./"), ("description", FEED_DESCRIPTION)):
        ET.SubElement(channel, tag).text = value
    for e in entries:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = e["title"]
        ET.SubElement(item, "link").text = f'{base}{e["slug"]}/'
        ET.SubElement(item, "guid", isPermaLink="false").text = e["slug"]
        ET.SubElement(item, "pubDate").text = format_datetime(datetime.fromisoformat(e["published_at"]))
        ET.SubElement(item, "description").text = e["dek"]
        if e.get("epic"):
            ET.SubElement(item, "category").text = e["epic"]
        ET.SubElement(item, f"{{{content}}}encoded").text = _text_html(e.get("text") or e["dek"])
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(rss, encoding="unicode") + "\n"
