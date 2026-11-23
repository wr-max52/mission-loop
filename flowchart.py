"""The pipeline strip: the flow chart drawn on the UI's home page (live) and on the published site's About
page (a looping demo). Both draw it from here, so they always show the same pipeline.

It is a snake. The steps, in runtime order, are cut into lanes at each human review: lane 0 runs left to right
from the top left (the Theorist) to the first review, a square end on the right; lane 1 comes back right to left
one level lower to the next review on the left; and so on, each review spanning the two lanes it joins, so the
snake turns through it. Agents are tiles with an icon, each code check a narrow tile right after the agent it
checks. In auto mode (the UI only) the two reviews it takes over are drawn as automatic. assets/flow.js shrinks it
to fit and relights it; assets/researchers.js puts researchers on it, each on its own step's row.
"""
from html import escape
import activity, settings
from i18n import _
from contracts import Mission
from icons import icon, CSS as ICON_CSS

# (key, kind, label on the strip), in runtime order
FLOW = [
    ("theorist", "agent", "Theorist"),
    ("theorist_check", "check", "check"),  # theorist.settle
    ("finder", "agent", "Finder"),
    ("finder_check", "check", "check"),    # finder._keep and the 2+ missions rule for a new epic
    ("epic_review", "human", "Review epics"),   # the epics both the Theorist and the finder propose, and its combines
    ("sources", "agent", "Sources"),
    ("brief", "agent", "Brief"),
    ("source_check", "check", "check"),
    ("brief_review", "human", "Review brief"),
    ("outreach", "agent", "Outreach"),     # only while outreach is on (OUTREACH_STEPS)
    ("outreach_check", "check", "check"),  # contacts.send's checks on each message
    ("replies", "agent", "Replies"),       # the reply wait
    ("writer", "agent", "Article"),
    ("article_check", "check", "check"),
    ("article_rating", "agent", "Rating"), # the Research Assistant rates the checked article (agents/rater.py)
    ("rating_check", "check", "check"),    # code: a total of 20 or less goes back to the writer
    ("article_review", "human", "Review article"),
    ("publisher", "agent", "Publish"),     # the local site and every channel: one step on the strip
]
TILES = {   # key -> (icon, the line under its name on the strip)
    "theorist": ("bulb", "connects the dots"), "finder": ("compass", "finds missions"),
    "sources": ("book", "searches and reads"), "outreach": ("send", "asks people"),
    "replies": ("clock", "waits for answers"),
    "brief": ("write", "writes the brief"), "writer": ("newspaper", "writes the article"),
    "article_rating": ("scale", "rates the article"),
    "publisher": ("broadcast", "site and channels"),
}
REVIEW_SUB = {"epic_review": "you approve", "brief_review": "you approve", "article_review": "you publish"}
HUMAN_STEPS = {"epic_review": ("Epic review: approve, rename, move or reject proposed epics; approve or reject "
                               "combines", "propose"),   # the purple of the New epic card
               "brief_review": ("Brief review: approve or send back", "review"),
               "article_review": ("Article review: publish or send back", "publish")}
# Auto mode takes over exactly these two reviews (key -> its label and title then); publishing is never automatic.
AUTO_STEPS = {"epic_review": ("Auto-approve epics", "Auto mode: proposed epics and combines are approved"),
              "brief_review": ("Auto-approve brief", "Auto mode: briefs that pass the check are approved")}
RETRY = "Failures retry; parked after {n}."
CHECKS = {   # every code check on the strip: what it holds the step before it to
    "theorist_check": "Theorist check (code): questions only, keeps half its words; new epic needs 2+ missions and "
                      "a new name; new mission must be new; a combine takes 2-4 missions not yet in writing; a link "
                      "needs an open mission, a reason and a new pair. Failures are dropped.",
    "finder_check": "Finder check (code): up to 3 missions, each backed by a found page and unlike existing topics; "
                    "new epic needs 2+ missions. Failures are dropped.",
    "source_check": "Source check (code): every claim cited, quotes on the page, 3 sites, 1+ fact, a counterpoint.",
    "outreach_check": "Outreach check (code): a set-up channel, a real address, a first name, a short topic, one "
                      "question; nobody asked twice or twice a week. Failures aren't sent; the agent is told why.",
    "article_check": "Article check (code): every paragraph cited to the brief's sources, exact quotes, no outside "
                     "links, opposing view cited.",
    "rating_check": "Rating check (code): the Research Assistant scores the claim and logic, intrigue and sources "
                    "1-10 each; 20 or less goes back to the writer with the reasons.",
}
PARKS = {"source_check", "article_check", "rating_check"}   # the checks that retry and then park; the rest drop

OUTREACH_STEPS = ("outreach", "outreach_check", "replies")

def flow_steps(auto: bool = False) -> list[tuple[str, str, str]]:
    """The strip's steps for this setup: outreach, its check and the reply wait only while outreach is on, and with
    auto (the UI's switch) the two reviews it takes over drawn as automatic ("auto")."""
    return [(k, "auto", _(AUTO_STEPS[k][0])) if auto and k in AUTO_STEPS else (k, kind, _(text))
            for k, kind, text in FLOW if k not in OUTREACH_STEPS or settings.outreach_on()]

def lanes(steps: list[tuple[str, str, str]]) -> tuple[list[list[tuple[str, str, str]]], list[tuple[str, str, str]]]:
    """Cut the steps into lanes at each human review: lane 0, review 0, lane 1, review 1, ... Each review is the end
    of the lane before it and the start of the one after. The last lane may be empty."""
    out, ends = [[]], []
    for step in steps:
        if step[1] in ("human", "auto"):
            ends.append(step)
            out.append([])
        else:
            out[-1].append(step)
    return out, ends

def track(lit: dict[str, str], waiting: dict[str, int], parked: dict[str, int], review_href: str = "",
          auto: bool = False) -> str:
    """The strip's track: its lanes and the reviews between them. lit: step -> the worker on it right now; waiting:
    missions at each human review; parked: missions parked at each check. review_href links the human reviews (a
    span without it). auto draws the reviews auto mode takes over as automatic."""
    steps = flow_steps(auto)
    runs, ends = lanes(steps)
    parked_n = Mission.model_fields["max_attempts"].default

    def tile(key: str, kind: str, text: str) -> str:
        on = " on" if key in lit else ""
        cur = f' aria-current="step" data-worker="{lit[key]}"' if on else ""
        if kind == "check":
            n = parked.get(key, 0)
            title = escape(_(CHECKS[key]) + (" " + _(RETRY).format(n=parked_n) if key in PARKS else ""))
            return (f'<span class="check{on}" data-step="{key}" title="{title}"{cur}>'
                    f'{icon("shield")}<span class="label">{text}</span>'
                    f'<b class="parked-count" title="{escape(_("{n} parked here").format(n=n))}"{"" if n else " hidden"}>{n}</b></span>')
        name, sub = TILES[key]
        return (f'<span class="step{on}" data-step="{key}" title="{escape(_(activity.AGENTS[key]))}"{cur}>{icon(name)}'
                f'<span class="text"><span class="name">{text}</span><span class="sub">{_(sub)}</span></span></span>')

    def review(key: str, kind: str, text: str, place: str) -> str:
        if kind == "auto":
            return (f'<span class="human auto" data-step="{key}" title="{escape(_(AUTO_STEPS[key][1]))}" style="{place}">'
                    f'{icon("bolt")}<span class="text"><span class="name">{text}</span>'
                    f'<span class="sub">{_("auto mode")}</span></span></span>')
        title, tone = HUMAN_STEPS[key]
        title = _(title)
        n = waiting.get(key, 0)
        tag, href = ("a", f' href="{escape(review_href)}"') if review_href else ("span", "")
        return (f'<{tag} class="human {tone}{" waiting" if n else ""}"{href} data-step="{key}" '
                f'title="{escape(title)}" style="{place}">{icon("person")}<span class="text"><span class="name">{text}</span>'
                f'<span class="sub">{_(REVIEW_SUB[key])}</span></span><b class="count"{"" if n else " hidden"}>{n}</b></{tag}>')

    out = []
    for i, run in enumerate(runs):
        rtl, last = i % 2 == 1, i == len(runs) - 1
        if not run and last:
            break
        # Each agent with the arrow into it and its checks is one unit, so a stacked lane wraps between units.
        units: list[list[str]] = []
        for key, kind, text in run:
            if kind == "check":
                units[-1] += ['<span class="join short" aria-hidden="true"></span>', tile(key, kind, text)]
                continue
            lead = ('<span class="join" aria-hidden="true"></span>' if units else
                    '<span class="join edge" aria-hidden="true"></span>' if i else "")   # out of the review before
            units.append([lead, tile(key, kind, text)])
        parts = [f'<span class="unit">{"".join(u)}</span>' for u in units]
        if i < len(ends):
            parts.append('<span class="join edge" aria-hidden="true"></span>')     # into the review that ends it
        # A lane reaches into an end column no review holds: the first lane's start, the last lane's end.
        start_free, end_free = i == 0, i >= len(ends)
        left_free, right_free = (end_free, start_free) if rtl else (start_free, end_free)
        cols = f"{1 if left_free else 2} / {4 if right_free else 3}"
        out.append(f'<span class="lane{" rtl" if rtl else ""}" style="grid-row:{i + 1}; grid-column:{cols}">'
                   + "".join(parts) + "</span>")
        if i < len(ends):
            key, kind, text = ends[i]
            span = 2 if i + 1 < len(runs) and (runs[i + 1] or i + 1 < len(ends)) else 1
            out.append(review(key, kind, text, f"grid-row:{i + 1} / span {span}; grid-column:{1 if rtl else 3}"))
    return "".join(out)

CSS = ICON_CSS + """/* the pipeline strip: a snake. Each lane runs between two human reviews, the reviews are square ends at the lanes'
   ends, and the lanes turn at each one: left to right, then right to left one level lower, and so on (ui.flow_strip).
   Agents are tiles with an icon, each code check a narrow tile right after the agent it checks. It never scrolls:
   assets/flow.js sets --s to shrink it to the panel's width, and below a readable size it stacks (.stack): one
   column in flow order, lanes wrapping, every arrow pointing on. */
.flow { flex-basis:100%; min-width:0; margin-top:.3rem; --line:color-mix(in srgb, var(--muted) 60%, transparent); }
.flow-scroll { overflow:hidden; padding-bottom:.25rem; }
.flow-track { --s:1; --u:calc(1rem * var(--s)); --tile:calc(var(--u) * 3.1); --cap:calc(var(--u) * 7.6);
              display:grid; grid-template-columns:var(--cap) minmax(min-content, 1fr) var(--cap);
              row-gap:calc(var(--u) * 1.3); width:100%; position:relative; }
.flow-track[data-lab] { padding-top:5rem; row-gap:max(3.4rem, calc(var(--u) * 1.3)); }   /* room for the researchers */
.flow .lane { display:flex; align-items:center; min-width:0; }
.flow .lane.rtl { flex-direction:row-reverse; }
.flow .unit { display:contents; }
.flow .step { flex:none; display:flex; align-items:center; gap:calc(var(--u) * .55); box-sizing:border-box;
              height:var(--tile); min-width:calc(var(--u) * 8.4); padding:0 calc(var(--u) * .8);
              border-radius:calc(var(--u) * .55); border:1px solid var(--rule); background:var(--quote); color:var(--muted); }
.flow .step .ico { width:calc(var(--u) * 1.35); height:calc(var(--u) * 1.35); }
.flow .step .name { display:block; font-size:calc(var(--u) * .86); font-weight:700; line-height:1.15; white-space:nowrap; }
.flow .step .sub { display:block; font-size:calc(var(--u) * .68); line-height:1.2; white-space:nowrap; opacity:.85; }
.flow .step.on { background:#237a35; border-color:#237a35; color:#fff; animation:working 2s ease-out infinite; }
.flow .join { flex:1 0 calc(var(--u) * 1.6); height:var(--tile); position:relative; }   /* stretch to fill the lane */
.flow .join::before { content:""; position:absolute; left:2px; right:4px; top:calc(50% - 1px); border-top:2px solid var(--line); }
.flow .join::after { content:""; position:absolute; right:1px; top:calc(50% - 4px);
                     border:4px solid transparent; border-right:0; border-left:5px solid var(--line); }
.flow .lane.rtl .join::before { left:4px; right:2px; }
.flow .lane.rtl .join::after { right:auto; left:1px; border-left:0; border-right:5px solid var(--line); }
.flow .join.short { flex:none; width:calc(var(--u) * .6); }
.flow .join.short::after { display:none; }
.flow .join.short::before, .flow .lane.rtl .join.short::before { left:1px; right:1px; }
.flow .check { flex:none; position:relative; box-sizing:border-box; width:calc(var(--u) * 2.5); height:var(--tile);
               border-radius:calc(var(--u) * .4); border:1px solid var(--line); color:var(--muted);
               display:flex; flex-direction:column; align-items:center; justify-content:center; gap:calc(var(--u) * .15);
               background:repeating-linear-gradient(-45deg, var(--quote) 0 4px, var(--bg) 4px 8px); }
.flow .check .ico { width:calc(var(--u) * 1.1); height:calc(var(--u) * 1.1); }
.flow .check .label { font-size:calc(var(--u) * .54); font-weight:700; letter-spacing:.08em; text-transform:uppercase; }
.flow .check.on { background:#237a35; border-color:#237a35; color:#fff; animation:working 2s ease-out infinite; }
.flow .check.on::after { content:""; position:absolute; left:1px; right:1px; height:3px; border-radius:2px;
                         background:#b2f2bb; animation:scan 1.1s ease-in-out infinite alternate; }
.flow .parked-count { position:absolute; top:calc(var(--u) * -.5); right:calc(var(--u) * -.5); min-width:var(--u);
                      height:var(--u); border-radius:999px; background:#c92a2a; color:#fff; font-size:calc(var(--u) * .62);
                      line-height:var(--u); text-align:center; padding:0 calc(var(--u) * .2); }
/* a human review: the square end of a lane, where the snake turns. In auto mode the two it takes over are green. */
.flow .human { position:relative; align-self:stretch; box-sizing:border-box; min-height:var(--tile);
               display:flex; flex-direction:column; align-items:center; justify-content:center; gap:calc(var(--u) * .3);
               padding:calc(var(--u) * .5); border-radius:calc(var(--u) * .55); border:2px dashed var(--line);
               background:var(--bg); color:var(--muted); text-align:center; text-decoration:none; }
.flow .human .ico { width:calc(var(--u) * 1.7); height:calc(var(--u) * 1.7); }
.flow .human .name { display:block; font-size:calc(var(--u) * .86); font-weight:700; line-height:1.15; }
.flow .human .sub { display:block; font-size:calc(var(--u) * .68); line-height:1.2; }
.flow .human.waiting { border-style:solid; border-color:var(--tone); background:var(--tint); color:var(--fg); }
.flow .human .count { position:absolute; top:calc(var(--u) * .4); right:calc(var(--u) * .4); background:var(--tone);
                      color:#fff; border-radius:999px; font-size:calc(var(--u) * .7); line-height:calc(var(--u) * 1.15);
                      min-width:calc(var(--u) * 1.15); padding:0 calc(var(--u) * .3); box-sizing:border-box; }
.flow .human.arrived { animation:arrived 1s ease-out 3; }
.flow .human.auto { border-style:solid; border-color:#237a35; color:var(--fg);
                    background:color-mix(in srgb, #237a35 12%, var(--bg)); }
.flow .human.auto .ico { color:#237a35; }
.flow .human.auto .ico svg { fill:currentColor; stroke:none; }
.flow.stack .flow-track { display:flex; flex-direction:column; align-items:stretch; }
.flow.stack .lane, .flow.stack .lane.rtl { flex-direction:row; flex-wrap:wrap; row-gap:calc(var(--u) * .6); }
.flow.stack .unit { display:flex; align-items:center; flex:none; }
.flow.stack .step { min-width:0; }
.flow.stack .flow-track[data-lab] .lane { row-gap:3.4rem; }
.flow.stack .join { flex:none; width:calc(var(--u) * 1.4); }
.flow.stack .join.short { width:calc(var(--u) * .6); }
.flow.stack .join.edge { display:none; }
.flow.stack .lane.rtl .join::before { left:2px; right:4px; }
.flow.stack .lane.rtl .join::after { left:auto; right:1px; border-right:0; border-left:5px solid var(--line); }
.flow.stack .human { flex-direction:row; justify-content:flex-start; text-align:left; padding:calc(var(--u) * .5) calc(var(--u) * .8); }
.flow.stack .human .count { position:static; margin-left:auto; }
.flow .text { display:block; }
.flow [hidden] { display:none; }
.flow.off .step, .flow.off .check { opacity:.7; }
.flow .now { margin-top:.45rem; }
@keyframes scan { from { top:2px; } to { top:calc(100% - 5px); } }
@keyframes arrived { from { box-shadow:0 0 0 0 var(--tone); } to { box-shadow:0 0 0 .5rem transparent; } }
@media (prefers-reduced-motion: reduce) {
  .flow .step.on, .flow .check.on, .flow .check.on::after, .flow .human.arrived { animation:none; }
}
.flow .human.review { --tone:#1f6feb; --tint:rgba(31,111,235,.12); }
.flow .human.propose { --tone:#7048e8; --tint:rgba(112,72,232,.12); }
.flow .human.publish { --tone:#237a35; --tint:rgba(47,158,68,.14); }
@keyframes working { from { box-shadow:0 0 0 0 rgba(47,158,68,.6); } to { box-shadow:0 0 0 .45rem rgba(47,158,68,0); } }
canvas.lab-stage { position:absolute; left:0; pointer-events:none; image-rendering:pixelated; width:100%; }
.flow-track canvas.lab-stage { top:0; height:100%; }
"""
