"""The pipeline strip: the flow chart drawn on the UI's home page (live) and on the published site's About
page (a looping demo). Both draw it from here, so they always show the same pipeline.

Agents sit on the main track in runtime order, each code check is a vertical block right after the agent it
checks, and each human review is a sidestep below, between the check that leads to it and the agent that
follows it. assets/flow.js shrinks it to fit and relights it; assets/researchers.js puts researchers on it.
"""
from html import escape
import activity, settings
from contracts import Mission

# (key, kind, label on the strip), in runtime order
FLOW = [
    ("theorist", "agent", "Theorist"),
    ("finder", "agent", "Finder"),
    ("sources", "agent", "Sources"),
    ("contacts", "agent", "Contacts"),
    ("brief", "agent", "Brief"),
    ("source_check", "check", "check"),
    ("brief_review", "human", "Review brief"),
    ("writer", "agent", "Article"),
    ("article_check", "check", "check"),
    ("article_review", "human", "Review article"),
    ("publisher", "agent", "Publish"),     # the local site and every channel: one step on the strip
]
HUMAN_STEPS = {"brief_review": ("Brief review: you approve the brief or send it back", "review"),
               "article_review": ("Article review: you publish the article or send it back", "publish")}
CHECK_TITLE = ("{name}: code, no model. A failure goes back for another try; "
               "after {n} tries the mission is parked for you")
ICON_PERSON = ('<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="4"/>'
               '<path d="M4 21c0-4.4 3.6-7 8-7s8 2.6 8 7"/></svg>')

def flow_steps() -> list[tuple[str, str, str]]:
    """The strip's steps for this setup: contacts only while outreach is on."""
    return [s for s in FLOW if s[0] != "contacts" or settings.OUTREACH]

def track(lit: dict[str, str], waiting: dict[str, int], parked: dict[str, int], review_href: str = "") -> str:
    """The strip's track. lit: step -> the worker on it right now; waiting: missions at each human review;
    parked: missions parked at each check. review_href links the human reviews (a span without it)."""
    units: list[list[str]] = []
    for key, kind, text in flow_steps():
        on = " on" if key in lit else ""
        cur = f' aria-current="step" data-worker="{lit[key]}"' if on else ""
        if kind == "agent":
            units.append(['<span class="join" aria-hidden="true"></span>'] if units else [])
            parts = units[-1]
            parts.append(f'<span class="step{on}" data-step="{key}" title="{escape(activity.AGENTS[key])}"{cur}>{text}</span>')
        elif kind == "check":
            parts.append('<span class="join short" aria-hidden="true"></span>')
            n = parked.get(key, 0)
            title = CHECK_TITLE.format(name=activity.AGENTS[key], n=Mission.model_fields["max_attempts"].default)
            parts.append(f'<span class="check{on}" data-step="{key}" title="{escape(title)}"{cur}>'
                         f'<span class="label">{text}</span>'
                         f'<b class="parked-count" title="{n} parked here"{"" if n else " hidden"}>{n}</b></span>')
        else:
            title, tone = HUMAN_STEPS[key]
            n = waiting.get(key, 0)
            tag, href = ("a", f' href="{escape(review_href)}"') if review_href else ("span", "")
            parts.append(f'<span class="detour"><{tag} class="human {tone}{" waiting" if n else ""}"{href} '
                         f'data-step="{key}" title="{escape(title)}">{ICON_PERSON}{text}'
                         f'<b class="count"{"" if n else " hidden"}>{n}</b></{tag}></span>')
    return "".join(f'<span class="unit">{"".join(u)}</span>' for u in units)

CSS = """/* the pipeline strip: agents on the track, checks as vertical blocks, human reviews as sidesteps below.
   It never scrolls: assets/flow.js sets --s to shrink it to the panel's width, and below a readable size it
   wraps between units (an agent with its check and review), each line starting with the arrow it continues. */
.flow { flex-basis:100%; min-width:0; margin-top:.3rem; --line:color-mix(in srgb, var(--muted) 60%, transparent); }
.flow-scroll { overflow:hidden; padding-bottom:.25rem; }
.flow-track { --s:1; --u:calc(1rem * var(--s)); --row:calc(var(--u) * 1.6); --drop:var(--u); --hh:calc(var(--u) * 1.45);
              --ch:calc(var(--row) + var(--u) * 1.1);
              display:flex; align-items:flex-start; width:100%; position:relative; }
.flow-track[data-lab] { padding-top:5rem; }          /* headroom for the researchers and their speech bubbles */
.flow .unit { display:contents; }
.flow.wrap .flow-track { flex-wrap:wrap; row-gap:calc(var(--u) * .7); }
.flow.wrap .unit { display:flex; align-items:flex-start; flex:none; }
.flow.wrap .join:not(.short) { flex:none; width:calc(var(--u) * 1.5); }
.flow .step { flex:none; height:var(--row); line-height:calc(var(--row) - 2px); padding:0 calc(var(--u) * .65);
              border-radius:999px; border:1px solid var(--rule); background:var(--quote); color:var(--muted);
              font-size:calc(var(--u) * .78); font-weight:600; white-space:nowrap; }
.flow .step.on { background:#237a35; border-color:#237a35; color:#fff; animation:working 2s ease-out infinite; }
.flow .join { flex:1 0 var(--u); max-width:calc(var(--u) * 3.5); height:var(--row); position:relative; }   /* stretch to fill */
.flow .join::before { content:""; position:absolute; left:2px; right:4px; top:calc(50% - 1px); border-top:2px solid var(--line); }
.flow .join::after { content:""; position:absolute; right:1px; top:calc(50% - 4px);
                     border:4px solid transparent; border-right:0; border-left:5px solid var(--line); }
.flow .join.short { flex:none; width:calc(var(--u) * .5); } .flow .join.short::after { display:none; }
.flow .join.short::before { right:1px; }
.flow .check { flex:none; position:relative; width:var(--u); height:var(--ch); border-radius:4px; overflow:visible;
               border:1px solid var(--line); display:flex; align-items:center; justify-content:center;
               background:repeating-linear-gradient(-45deg, var(--quote) 0 4px, var(--bg) 4px 8px); }
.flow .check .label { writing-mode:vertical-rl; transform:rotate(180deg); font-size:calc(var(--u) * .56); font-weight:700;
                      letter-spacing:.08em; text-transform:uppercase; color:var(--muted); }
.flow .check.on { background:#237a35; border-color:#237a35; animation:working 2s ease-out infinite; }
.flow .check.on .label { color:#fff; }
.flow .check.on::after { content:""; position:absolute; left:1px; right:1px; height:3px; border-radius:2px;
                         background:#b2f2bb; animation:scan 1.1s ease-in-out infinite alternate; }
.flow .parked-count { position:absolute; right:calc(100% + 3px); bottom:-1px; min-width:var(--u); height:var(--u);
                      border-radius:999px; background:#c92a2a; color:#fff; font-size:calc(var(--u) * .62);
                      line-height:var(--u); text-align:center; padding:0 calc(var(--u) * .2); }
.flow .detour { flex:none; position:relative; padding:calc(var(--row) + var(--drop)) calc(var(--u) * .9) 0; }
.flow .detour::before { content:""; position:absolute; left:calc(var(--u) * -.5); width:calc(var(--u) * 1.4 - 3px); top:var(--ch);
                        height:calc(var(--row) + var(--drop) + var(--hh) / 2 - var(--ch));
                        border-left:2px solid var(--line); border-bottom:2px solid var(--line);
                        border-bottom-left-radius:7px; }
.flow .detour::after { content:""; position:absolute; right:0; width:calc(var(--u) * .9); top:calc(var(--row) / 2 - 1px);
                       height:calc(var(--row) / 2 + var(--drop) + var(--hh) / 2);
                       border-right:2px solid var(--line); border-bottom:2px solid var(--line);
                       border-bottom-right-radius:7px; }
.flow .human { position:relative; display:inline-flex; align-items:center; gap:calc(var(--u) * .3); height:var(--hh);
               padding:0 calc(var(--u) * .55); border-radius:999px; border:1px dashed var(--line); background:var(--bg);
               color:var(--muted); font-size:calc(var(--u) * .74); font-weight:600; white-space:nowrap; text-decoration:none; }
.flow .human::before { content:""; position:absolute; left:-6px; top:calc(50% - 4px);
                       border:4px solid transparent; border-right:0; border-left:5px solid var(--line); }
.flow .human svg { width:calc(var(--u) * .85); height:calc(var(--u) * .85); fill:none; stroke:currentColor; stroke-width:2.2;
                   stroke-linecap:round; stroke-linejoin:round; }
.flow .human.waiting { border-style:solid; border-color:var(--tone); background:var(--tint); color:var(--fg); }
.flow .human .count { background:var(--tone); color:#fff; border-radius:999px; font-size:calc(var(--u) * .68);
                      line-height:calc(var(--u) * 1.05); padding:0 calc(var(--u) * .38); }
.flow .human.arrived { animation:arrived 1s ease-out 3; }
.flow [hidden] { display:none; }
.flow.off .step, .flow.off .check { opacity:.7; }
.flow .now { margin-top:.45rem; }
@keyframes scan { from { top:2px; } to { top:calc(100% - 5px); } }
@keyframes arrived { from { box-shadow:0 0 0 0 var(--tone); } to { box-shadow:0 0 0 .5rem transparent; } }
@media (prefers-reduced-motion: reduce) {
  .flow .step.on, .flow .check.on, .flow .check.on::after, .flow .human.arrived { animation:none; }
}
.flow .human.review { --tone:#1f6feb; --tint:rgba(31,111,235,.12); }
.flow .human.publish { --tone:#237a35; --tint:rgba(47,158,68,.14); }
@keyframes working { from { box-shadow:0 0 0 0 rgba(47,158,68,.6); } to { box-shadow:0 0 0 .45rem rgba(47,158,68,0); } }
canvas.lab-stage { position:absolute; left:0; pointer-events:none; image-rendering:pixelated; width:100%; }
.flow-track canvas.lab-stage { top:0; height:100%; }
"""
