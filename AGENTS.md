# AGENTS.md

## Critical constraint: missions stay local

The code lives in the private GitHub repo `wr-max52/mission-loop` (`origin`). Push only when asked.
Never commit or push `missions.yaml`, `missions/`, `data/`, `site/`, or `.env`; they stay on this
machine. A local `pre-push` hook blocks commits containing them. Branches started before the repo
moved to GitHub must be rebased onto `origin/main` before pushing. Publishing writes a static site
into `site/` on this machine, posts the article text to 1f916.ai only once `data/1f916.json` holds a key, pushes
`site/` to a separate public GitHub Pages repo only once GitHub is connected and a repo linked (`data/github.json`,
from the Settings page), and announces it on Bluesky only once a login is saved (`data/bluesky.json`, from the
Settings page) or `.env` has `BLUESKY_HANDLE` and `BLUESKY_APP_PASSWORD`. `data/1f916.json` is the citizen's whole
identity (no recovery), `data/github.json` holds a GitHub token and `data/bluesky.json` an app password: never
commit, print or move any of them. The Pages repo is the one place `site/` is meant to go.

## Running

- Interpreter: Python 3.12 (`/opt/homebrew/bin/python3.12`). The code uses 3.10+ union syntax.
- Activate the venv first: `source .venv/bin/activate`
- CLI: `python run.py <command>` — `ui init add inspire run loop stop approve reject review requeue push show status ask join-1f916 check-bluesky epics epic rename-epic`
- Tests: `pytest -q` (expects 357 passed; no Ollama or network needed; any request to 1F916, GitHub, Bluesky, a
  custom webhook or the Wayback Machine fails the test, and `BLUESKY_*` and `MISSION_LOOP_GITHUB_*` variables are
  cleared so a real `.env` never switches a channel on)
- Config, not code: nothing machine-specific is written in the code. Models, the Ollama address, the 1F916 and
  Bluesky servers, the GitHub server and OAuth app, and the site address are looked up in `data/settings.json` (the
  Settings page writes it), then `.env` (see `.env.example`), and only a service address falls back to that
  service's public default (Ollama's local `http://127.0.0.1:11434`, `https://1f916.ai`, `https://bsky.social`,
  `https://github.com`; the site address falls back to the linked GitHub Pages address). Models have no default: with none
  chosen the loop won't start and says so. All lookups live in `settings.py`; don't add a model name or server
  address anywhere else. Tests set their own fake models in `tests/conftest.py`.
- `MISSION_LOOP_STUBS=1` runs every agent on stubs (no Ollama, no network) and turns the 1F916 and Bluesky channels off.
- Specs for how it was built: `milestone1.md`, `milestone2.md`.

## Pipeline (v1)

```
QUEUED -> RESEARCHING -> BRIEF_READY -> AWAITING_COMMENTS -> WRITING -> ARTICLE_READY -> DONE (published)
          (validator,     (human:        (skipped while       (article     (human:
           3 attempts)     approve)       outreach is off)     check, 3     review)
                                                               attempts)
```

- Order of work (`orchestrator.work`): there are no fixed passes. The loop works until nothing is left it
  can do on its own, reading missions fresh every few seconds, always in this order:
  1. Every code step, farthest along first (`in_flow_order`): publish a reviewed article, move an approved
     brief on, end the comment wait, start a queued mission. No model is called. Right after the first sweep,
     and whenever a sweep publishes something, the channels post.
  2. Each idle worker takes the farthest-along mission for its role that hasn't had a turn this round
     (`_pick`), so writing goes before a brief, a brief before a new search, and a brief a human approves
     mid-task is next in line. A failed attempt shows as before and the mission waits until the others with
     work for that worker have had a turn; then a new round starts from the farthest along.
  When the Research Assistant's role has nothing left, the mission finder runs once (what it finds is worked on
  straight away); when no worker has anything left, the loop rests (`--every` minutes, or until the UI wakes
  it). A step that raises is logged on its mission and skipped until the loop starts again; the others go on.
  `run.py loop` sends its notifications after every model task.
- Mission finder: `agents/finder.py`, once each time the loop runs out of model work for existing missions. It works from the
  human's inspiration line (`data/inspiration.txt`; no line means it is off). The small model runs at most
  5 searches from 2 randomly drawn lateral angles, then the main model proposes missions. Code keeps at
  most 3, each resting on a URL the searches returned and not too close to an existing topic. They are
  queued and researched straight away, and still go through human brief review. A finder failure is
  logged and never stops the loop.
- Epics: every mission can sit in one epic, a 1 to 4 word thematic title for a long-running line of
  inquiry (`epics` table; `Mission.epic` is its slug, which never changes when the title does). Humans
  name an epic when adding a mission; a new name makes a new, active epic. The finder sees each active
  epic with a few of its topics and must give every mission the best existing fit. It may also propose
  a new epic, but code keeps that only when 2+ missions from the same search share it; the proposal then
  waits on the UI home page (approve, rename and approve, move to another epic, or reject, which puts
  each mission back in the finder's best fit, `Mission.epic_fallback`). Epics show as pills on the
  board and the published site (approved epics only); renaming redraws the published pages.
- Workers: two, set on the Settings page (`settings.WORKERS`, saved in `data/settings.json`). The
  Research Assistant (the small model: `assistant_model` in settings.json, else `MISSION_LOOP_SEARCH_MODEL`) does
  the "assistant" role: searching and reading. The Principal Investigator (the big model: `pi_model`, else
  `MISSION_LOOP_MODEL`) does the "pi" role: the brief, the article and the finder's picks. The loop runs them on
  two threads (`orchestrator.work`): each takes the next task for its role (`orchestrator.role`), so one
  mission's search runs while another's article is written; steps with no model run between tasks on the
  main thread. A worker switched off hands its role to the other (`settings.worker_for`), which then works one
  task at a time; one always stays on. Model choices come from the LLM connection (`llm.connection`: the Ollama at
  `settings.ollama_host()`, which every Ollama call uses; set it on the Settings page or with `OLLAMA_HOST`). `data/activity.json` holds one entry per worker.
- Research: `agents/researcher.py` (Ollama + MCP search/fetch), checked by `checks/validator.py`, in two
  steps that can be done at different times. `gather` (Research Assistant) runs the search/fetch tool loop
  for at most 15 minutes (`SOURCE_TIME_LIMIT`) and saves the transcript as the mission's `evidence` artifact
  (`Mission.gathered`); a research attempt counts from here. `write_brief` (Principal Investigator) writes the
  brief from it, then the evidence is dropped. A stop during the search keeps the evidence. `page_tools.py` adds `find_in_page` (exact sentences from the text
  the validator checks), `page_links` (a page's links to other sites) and `wayback_versions` (the Wayback
  Machine's copies of a page: the ones that changed, spread over time, and the one nearest a date), and
  requests only public addresses. `wayback.py` (code, no model) makes an archived copy work like a live page:
  a snapshot address counts as its original site for domain counts and the block list, and is read as the
  raw copy, without the archive's toolbar. When `find_in_page` or `page_links` can't read a live page, they
  read its latest Wayback copy and say which address to cite; when a cited page can't be fetched, the
  validator's note names its archived copy. The prompt sends the agent to the archive for sites that are
  gone and to check whether a page changed what it says. Published pages label archived sources with
  their date. `related.py` (code, no model) adds up to 3 published articles most like the mission by
  word overlap, any epic (its own epic ranks a little higher), with the possibilities they left open
  and the pages they cited. They are background only: the site never leaves this machine, so they
  can't be cited, but their cited pages can be fetched again and cited fresh. With outreach on, a second small-model run
  finds contacts, and code checks their names and emails before the main model is called.
- Writing: `agents/writer.py` (one structured Ollama call), checked by `checks/article_check.py`.
- Every model call goes through `agents/llm.py`, which keeps it bounded. Before the call, tool results
  are cut to fit the 32k context (the longest pages lose their ends first; instructions and the final
  request stay whole), because Ollama otherwise drops the start of the prompt or, on the MLX runner,
  lets the model run on forever. The search loop cuts deeper (`llm.compact`, to 70% of the room) and keeps
  its transcript cut, so the next turns only append and Ollama reuses its prompt cache instead of rereading
  ~25k tokens every turn; results the model hasn't read yet stay whole. The brief writer gets the transcript
  without the small model's thinking, failed tool calls cut to one line and repeated results dropped. Replies are capped at `REPLY_TOKENS` (8192; a brief is about 4k) or
  `TURN_TOKENS` (2048) for tool turns, and at `REPLY_TIME_LIMIT` (10 minutes; a brief takes about 2).
  A reply that hits either limit raises `llm.Overrun`: a stuck search turn ends the search, and a stuck
  brief, article or finder reply keeps whatever valid JSON it wrote before (checked as usual), else the
  attempt fails. A writing retry after a failed article check sees its failed draft and revises it.
- Publishing has two parts: the local site, the foundation every article is written to first, and channels,
  everywhere else it goes, each posting from the local site's feed. Where each article went is recorded per
  mission in `Mission.channels` (key -> when and where):
  - `site`: `agents/publisher.py` writes `site/<slug>/index.html` and `meta.json` (with the article as plain
    text, `render.article_text`), then rebuilds `site/index.html` and the feeds `site/feed.json` (JSON Feed 1.1)
    and `site/feed.xml` (RSS 2.0) from the `meta.json` files, newest first, full text. Always on; reaching it is
    what makes a mission DONE. Feed links are relative unless a site address is set (the UI's Settings page
    saves it in `data/settings.json`, which wins over `MISSION_LOOP_SITE_URL` in `.env`); entry ids are the
    slugs either way. Each time the loop starts working it gives older `meta.json` files their text and rebuilds.
  - Every other channel posts from the feed (`agents/channel.py` says what a channel module provides; add it to
    `CHANNELS` in `agents/__init__.py`; `agents.channels()` is those plus the custom ones). When the loop starts,
    whenever it publishes and whenever a human asks to publish an article again, `orchestrator.post_to_channels`
    runs a publishing pass, and during long model work at least every `PASS_EVERY` (5 minutes): pages asked to be
    rewritten, then each enabled channel works through its whole backlog: entries a human asked for first (even ones
    it already posted), then feed entries it hasn't posted yet, newest first, refused ones last, until its allowance
    runs out. So a new channel with no cap (GitHub Pages, a folder, a webhook) gets every past article on its first pass, while
    1F916 takes one a day and Bluesky one per 10 minutes until they catch up. A pass gives up on a channel after
    `MAX_REFUSALS` (3) refusals.
    A refusal costs nothing (the reason goes in `Mission.channel_errors` and the next entry is tried); network or key
    failures lose nothing and are retried the next time something is published or the loop starts again. Stub runs have no channels.
  - Publishing again: the Published page (or a published mission's page) ticks the local site and any channels
    and records them in `Mission.publish_requests` (`orchestrator.request_publish`). The loop's next sweep sees
    the new request and runs a pass, within seconds even mid-task; "site" rewrites the page and feeds (the
    article keeps its first `published_at`). A channel's own politeness gap is skipped for a request
    (`MANUAL_SKIPS_WAIT`, Bluesky) but a real cap is not (1F916's daily post); a request that fails stays and
    goes first on the next pass. Requests for a channel since switched off are dropped.
  - custom: `agents/custom.py`, any number, added on the Settings page and saved in `data/settings.json`
    (`settings.custom_channels`): a name, a webhook (POST) or a folder (one file per article), and a format
    (a JSON Feed 1.1 item, Markdown or plain text). Keys are `custom-<id>`; no cap. A webhook's address is never
    recorded as where an article went (it often holds a token): a JSON reply's `url`, else its host.
  - `1f916`: `agents/f916.py` posts the feed entry's title and text to 1F916 as our citizen. The server to join is
    config (`settings.f916_url`: Settings page, `MISSION_LOOP_1F916_URL`, else https://1f916.ai); the key file records
    the server that issued it (`api`) and posting always goes there (`f916.api`). On only
    when `data/1f916.json` has a key (`python run.py join-1f916 <handle>`, once). The site allows one post per
    UTC day (`data/1f916_state.json` remembers when it is spent), so a just-published article goes out right after
    publishing if the day's post is free, and the rest wait, newest first.
  - `github`: `agents/github.py` keeps a separate public GitHub repo identical to `site/`, so GitHub Pages serves
    the site. Set up once on the Settings page: Connect GitHub (GitHub's device flow: the page shows a code to
    enter at github.com/login/device; scope `public_repo` only; token saved to `data/github.json`, mode 600), then
    create the site's repo (default `<login>.github.io`, which GitHub serves with nothing to switch on) or paste
    the link of one. Linking turns Pages on where the API allows it, else names the repo settings page to do it in,
    and refuses private repos, repos it can't push to and repos holding other files (the site replaces everything
    except `KEEP`: CNAME, README, licence). The channel's `sync` runs before its queue on every channel turn (an
    optional hook in `agents/channel.py`) and pushes whatever changed as one commit through the Git Data API (only
    changed files are uploaded; no git needed; `data/github_state.json` remembers the last push, so an unchanged
    site costs no request). Each entry's post records its page there. While no site address is typed in, the
    Pages address is the site address (`settings.site_url`), so the feeds use it and Bluesky links to it, and
    Bluesky holds an entry until GitHub has it. The sign-in uses mission-loop's OAuth app
    (`settings.GITHUB_CLIENT_ID`; a client ID is public, there is no secret); a copy without one asks for one on
    the Settings page (`MISSION_LOOP_GITHUB_CLIENT_ID` in `.env` also works).
  - `bluesky`: `agents/bluesky.py` announces the feed entry on Bluesky with a short post (title and dek, at most
    300 characters; a possibility in the dek keeps its "Possibility, not verified" label, and a pushed-through
    article starts with "[Failed automated checks]"). Its link card goes to the article on the site when
    a site address is set (Settings page or `MISSION_LOOP_SITE_URL`), else to its 1F916 post (1F916 comes first in `CHANNELS`, so a fresh one
    counts), else there is no link. On only with a handle and app password (never the account password): the
    Settings page checks them with Bluesky and saves them to `data/bluesky.json` (mode 600, wins over `.env`'s
    `BLUESKY_HANDLE` and `BLUESKY_APP_PASSWORD`); `python run.py check-bluesky` logs in without posting. A login saved on
    the Settings page keeps the server it was checked against; a `.env` login uses `BLUESKY_SERVICE` (default bsky.social). After each
    post it waits `POST_GAP` (10 minutes), so a backlog goes out at most one every 10 minutes. `data/bluesky_state.json`
    holds that wait and the last failure, which the UI and `status` show.
- Outreach is OFF (milestone 4): `interview` emails nobody, so the comment wait is skipped, and
  research doesn't look for points of contact (no POC email check). The switch is
  `MISSION_LOOP_OUTREACH=1` in `settings.py`; turn it on only once real outreach exists.
- Claims have a `kind`: `fact` or `possibility`. The prompts treat every source as part truth, part spin.
  Research explores widely, drops only what specific evidence contradicts, and leads with pointed
  possibilities that each rest on a cited detail. A `fact` means only what the record directly shows (what
  a document says, what someone said). Code can't verify a possibility, so it enforces the label instead:
  possibilities must say "It is possible that", a brief needs at least one fact, and the article marks
  each one "Possibility, not verified".
- The thesis must be checkable (who, what, when, how much). Research must look for the case against it,
  but a contradicting source is optional, since a broad thesis often has none: any it cites is verified
  like every source, and the brief always needs a counterpoint. Without a contradicting source the
  counterpoint is the skeptic's objection, marked "No source found makes this case" in the brief, and the
  article says so. The hard gate is the claims: cited, quotes found on the page, 3 supporting websites.
- A parked mission can be pushed through: its last failed brief or article moves to the human review
  that follows, and stays marked as failing its check (`unchecked_brief` / `unchecked_article`), including
  on the published page. This needs a failed draft; if every attempt crashed, only requeue works.
- `agents/__init__.py` is still the only switchboard. Validators never call the LLM.
- Tools are MCP servers, always: a new agent tool goes in a small server module registered in `mcp.json`
  (like `page_tools.py`, `status_tools.py`) and is reached through `agents/toolbox.py`, never imported directly,
  so other clients can use it later. Script paths in `mcp.json` are relative to `mcp.json`.
- Chat assistant: `agents/assistant.py` answers the human's questions about the loop: the chat bubble at the
  bottom right of every UI page opens it in a panel over the left third, or `python run.py ask "..." [--model]`.
  It needs Ollama but not the loop, so it works with the loop stopped. The panel has a model picker (installed
  Ollama models that can call tools; the default is shared with the loop, any other is loaded beside it), and keeps the
  conversation in that browser tab only, so the page's refreshes don't wipe it. It is not a worker: a short prompt that explains the app, at most 5 tool calls, tool results cut
  to 2,500 characters, only the last 4 messages the page sends back, no memory and nothing saved. Its tools come
  from the `loop` MCP server (`status_tools.py`: `loop_status`, `list_missions`, `mission_status`, `agent_steps`,
  `search_missions`, `publication_status`, `list_epics`, `add_mission`, `set_mission_epic`), plus `search` when
  `BRAVE_API_KEY` is set. Mission listings name each mission's epic. All but `add_mission` and `set_mission_epic`
  only read the database, `data/activity.json`, the channels' state files, the trails and
  `data/loop.log`, so asking never touches a running agent. `add_mission` queues a new mission like the UI's Add
  mission box (it still needs brief approval); `set_mission_epic` puts any mission, published ones included, in an
  epic like the Epic field on its page (`orchestrator.assign_epic`). Neither changes a mission's stage or work. The
  panel renders a small, escaped Markdown subset in replies (bold, italic, code, links, lists). By default it uses the small model with
  the loop's options (`settings.chat_model`: `chat_model` in settings.json, then `MISSION_LOOP_CHAT_MODEL`, else
  the Research Assistant's model), so Ollama reuses the loaded copy instead of reloading it.
- Trails: `trail.py` keeps each mission's last 40 agent steps (model thinking and replies, tool calls, tool
  results, each cut to 400 characters) in `data/trail/<slug>.jsonl` (`_finder.jsonl` for the finder). Only the
  loop writes them: `activity.begin` starts a trail, `llm.chat` and `researcher.tool_loop` add to it. Display-only.

## Operating it

The easy way: `python run.py ui`, then open http://127.0.0.1:8770. It lists every mission, shows briefs and
article previews, and has buttons for approve / send back / publish / requeue / push through / add mission / save
the finder's inspiration line and to start, stop or wake the loop (a loop started there runs in the background and logs to `data/loop.log`).
The Missions page reads like the flow chart from its end: what needs you at the top, then everything in
progress closest to published first (publishing, writing, briefs, searches) and new missions last; published
articles are not listed there, only a link to the Published page. The Published site button in the top bar
opens that page (`/published`, with a link back): the local site, every channel, and each article with where it
went and boxes to publish it again. Its Settings page (`/settings`) has the workers, the LLM connection and
Publishing: the local site (always on: feeds, the hosted address), the channels that are set up, and Add a
channel (1F916, the same `f916.register` as the CLI; Bluesky; custom); a fresh clone needs no settings. `ui.py`
only records human decisions through the same store/orchestrator functions as the CLI; it never runs
agents. It binds to 127.0.0.1 and refuses cross-site form posts. The home page's pipeline strip is always drawn,
unlit while the loop is off: agents on a track in runtime order (publishing is one step, whatever the channels), each code check a vertical block right after the
agent it checks (with a red count of missions parked there), and each human review a sidestep below between that
check and the next agent (with a count of missions waiting on you). The steps working right now are lit, one per
busy worker, read from `data/activity.json` (written by `activity.py`). `assets/flow.js` polls `/api/flow` every 2
s and relights the strip in place, and the mission cards below with it (green outline, "working on this now",
researcher visitors), one per busy worker, so they never wait on the page refresh, which holds off while you type or
chat; the rest of the page refreshes by swapping in fresh HTML around the live strip, never by reloading. Small pixel researchers (`assets/researchers.js`, decorative only) live on the cards of
missions a model is working on right now (none while no model runs) and on the strip, never more than four on the
page at once (one on each busy worker's lit step, one on each busy card). Each character is in one worker's crew (`worker` in
`ROSTER`: young ones the Research Assistant's, tenured ones the Principal Investigator's), and 80% of a card's
visitors (`FAVOR`) come from the crew of the worker running it (`data-worker`), the rest from the other crew; on the Settings page each worker card
has its whole crew standing about. A card's visitor keeps a timer: a random first wait, 15-30 s on stage, walks
off, then 10-20 s before the next; the longest-waiting card goes next, and a card nobody works on any more sees its
visitor walk off. Every arrival, the strip's included, comes one at a time, a random 12-20 s apart, so a full page
fills over about a minute; a lit step's newcomer goes ahead of the cards, and walks in at least 10 s after the one
it replaces walked off. Each busy worker's lit step
gets a researcher from that worker's crew, who follows the work instead: it stays on the lit step doing that
step's kind of work, runs after its mission when the mission moves on (sources, brief, check), and when the work
ends, moves to another mission or passes to the other worker it says a closing line about where the work went
(your review, parked, published, another try) and walks off, and a new one walks in for whatever started. Visitors
come from a shuffled cast and their lines from a shuffled deck of 500+ academic quips and memes, so nothing
repeats soon. They work through about 40 bits weighted by mood (the lit step on the strip): reading, digging and
typing, plus lab life like a chemistry poof, a model rocket, a rubber duck, a reviewer's stamp, a bird landing on
a head or the office cat; naps are rare.
The Settings page's Animations switch (on by default, saved in `data/settings.json` only while off) removes them.

| You want to | Command |
|---|---|
| Open the UI | `python run.py ui` → http://127.0.0.1:8770 |
| Add a mission | edit `missions.yaml` then `python run.py init`, or `python run.py add <slug> "<topic>" --objective "..."` |
| Put a mission in an epic | the Epic field on its page, or `python run.py epic <slug> "<title>"` (`""` clears) |
| See / rename epics | `python run.py epics`, `python run.py rename-epic "<old>" "<new>"`, or Epics on the UI home page |
| Approve a proposed epic | the "New epic proposed" panel on the UI home page |
| Set the mission finder's inspiration | the box on the UI home page, or `python run.py inspire "<line>"` (`""` turns it off) |
| Run once until blocked | `python run.py run` |
| Run forever | `python run.py loop --every 15` (or install `ops/com.missionloop.loop.plist`, see the comments in it) |
| Stop | `python run.py stop` (finishes the current task first) |
| Read a brief / article | `python run.py show <slug>`; the article preview is `missions/<slug>/preview.html` |
| Approve a brief | `python run.py approve <slug>` |
| Publish an article | `python run.py review <slug>`; while the loop runs it is published within seconds, even mid-task |
| Send back with notes | `python run.py reject <slug> --notes "..."` (brief → research, article → writer) |
| Revive a parked mission | `python run.py requeue <slug>` (resumes at writing if the brief was already approved) |
| Override a parked mission | `python run.py push <slug>` (its last failed draft goes to your review, marked as failed) |
| View the site | Published site in the UI's top bar, open `site/index.html`, or `python -m http.server 8765 -d site` |
| Publish an article again | tick where on the UI's Published page and press Publish (the loop does it within seconds) |
| Add a custom channel | Settings → Publishing → Add a channel → Custom channel (a webhook or a folder, and a format) |
| Subscribe | `site/feed.xml` (RSS) or `site/feed.json` (JSON Feed); set the hosted address on the Settings page once it is hosted |
| Turn on Bluesky posting | add your handle and an app password on the Settings page (or `BLUESKY_HANDLE` and `BLUESKY_APP_PASSWORD` in `.env`, then `python run.py check-bluesky`) |
| Put the site online (GitHub Pages) | Connect GitHub on the Settings page, then create or paste the site's repo |
| Turn on 1F916 posting | Join on the Settings page, or `python run.py join-1f916 <handle>` once (saves `data/1f916.json`); `status` and the UI show the queue |
| Ask what is going on | the chat bubble at the bottom right of the UI, or `python run.py ask "where is the grid mission at?"` |
| Pick each worker's model, or switch one off | the Workers cards on the Settings page (saved in `data/settings.json`) |
| Point at another Ollama | the LLM connection address on the Settings page, or `OLLAMA_HOST` in `.env` |
| Block a source site | add the domain to `blocked_domains.txt` |

Only one `run`/`loop` can work at a time (`data/loop.lock`). A UI action touches `data/WAKE`, so a
resting loop looks for work right away instead of waiting out `--every`. Before it starts working the loop checks that Ollama
is up, so a closed Ollama app doesn't burn every mission's attempts. The loop sends a macOS notification
when a mission needs you (brief ready, article ready, parked) or was published.
