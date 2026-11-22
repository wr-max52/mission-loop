# AGENTS.md

## Critical constraint: missions stay local

The code lives in the private GitHub repo `wr-max52/mission-loop` (`origin`). Push only when asked.
Never commit or push `missions.yaml`, `missions/`, `data/`, `site/`, or `.env`; they stay on this
machine. A local `pre-push` hook blocks commits containing them. Branches started before the repo
moved to GitHub must be rebased onto `origin/main` before pushing. Publishing writes a static site
into `site/` on this machine, posts the article text to 1f916.ai only once `data/1f916.json` holds a key, and
announces it on Bluesky only once a login is saved (`data/bluesky.json`, from the Settings page) or `.env` has
`BLUESKY_HANDLE` and `BLUESKY_APP_PASSWORD`. `data/1f916.json` is the citizen's whole identity (no recovery) and
`data/bluesky.json` holds an app password: never commit, print or move either.

## Running

- Interpreter: Python 3.12 (`/opt/homebrew/bin/python3.12`). The code uses 3.10+ union syntax.
- Activate the venv first: `source .venv/bin/activate`
- CLI: `python run.py <command>` — `ui init add inspire run loop stop approve reject review requeue push show status ask join-1f916 check-bluesky epics epic rename-epic`
- Tests: `pytest -q` (expects 301 passed; no Ollama or network needed; any request to 1F916, Bluesky or the
  Wayback Machine fails the test, and `BLUESKY_*` variables are cleared so a real `.env` never switches Bluesky on)
- Config, not code: nothing machine-specific is written in the code. Models, the Ollama address, the 1F916 and
  Bluesky servers and the site address are looked up in `data/settings.json` (the Settings page writes it), then
  `.env` (see `.env.example`), and only a service address falls back to that service's public default (Ollama's
  local `http://127.0.0.1:11434`, `https://1f916.ai`, `https://bsky.social`). Models have no default: with none
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

- Mission finder: `agents/finder.py`, once per pass, after existing missions have run. It works from the
  human's inspiration line (`data/inspiration.txt`; no line means it is off). The small model runs at most
  5 searches from 2 randomly drawn lateral angles, then the main model proposes missions. Code keeps at
  most 3, each resting on a URL the searches returned and not too close to an existing topic. They are
  queued and researched in the same pass, and still go through human brief review. A finder failure is
  logged and never stops the pass.
- Epics: every mission can sit in one epic, a 1 to 4 word thematic title for a long-running line of
  inquiry (`epics` table; `Mission.epic` is its slug, which never changes when the title does). Humans
  name an epic when adding a mission; a new name makes a new, active epic. The finder sees each active
  epic with a few of its topics and must give every mission the best existing fit. It may also propose
  a new epic, but code keeps that only when 2+ missions from the same pass share it; the proposal then
  waits on the UI home page (approve, rename and approve, move to another epic, or reject, which puts
  each mission back in the finder's best fit, `Mission.epic_fallback`). Epics show as pills on the
  board and the published site (approved epics only); renaming redraws the published pages.
- Workers: two, set on the Settings page (`settings.WORKERS`, saved in `data/settings.json`). The
  Research Assistant (the small model: `assistant_model` in settings.json, else `MISSION_LOOP_SEARCH_MODEL`) does
  the "assistant" role: searching and reading. The Principal Investigator (the big model: `pi_model`, else
  `MISSION_LOOP_MODEL`) does the "pi" role: the brief, the article and the finder's picks. Each pass runs them on
  two threads (`orchestrator.work`): each takes the next task for its role (`orchestrator.role`), so one
  mission's search runs while another's article is written; steps with no model run between tasks on the
  main thread. A worker switched off hands its role to the other (`settings.worker_for`), which then works one
  task at a time; one always stays on. Model choices come from the LLM connection (`llm.connection`: the Ollama at
  `settings.ollama_host()`, which every Ollama call uses; set it on the Settings page or with `OLLAMA_HOST`). `data/activity.json` holds one entry per worker.
- Research: `agents/researcher.py` (Ollama + MCP search/fetch), checked by `checks/validator.py`, in two
  steps that can land in different passes. `gather` (Research Assistant) runs the search/fetch tool loop
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
  lets the model run on forever. Replies are capped at `REPLY_TOKENS` (8192; a brief is about 4k) or
  `TURN_TOKENS` (2048) for tool turns, and at `REPLY_TIME_LIMIT` (10 minutes; a brief takes about 2).
  A reply that hits either limit raises `llm.Overrun`: a stuck search turn ends the search, and a stuck
  brief, article or finder reply keeps whatever valid JSON it wrote before (checked as usual), else the
  attempt fails.
- Publishing goes out to channels, recorded per mission in `Mission.channels` (key -> when and where):
  - `site`: `agents/publisher.py` writes `site/<slug>/index.html` and `meta.json` (with the article as plain
    text, `render.article_text`), then rebuilds `site/index.html` and the feeds `site/feed.json` (JSON Feed 1.1)
    and `site/feed.xml` (RSS 2.0) from the `meta.json` files, newest first, full text. Always on; reaching it is
    what makes a mission DONE. Feed links are relative unless a site address is set (the UI's Settings page
    saves it in `data/settings.json`, which wins over `MISSION_LOOP_SITE_URL` in `.env`); entry ids are the
    slugs either way. Each pass starts by giving older `meta.json` files their text and rebuilding.
  - Every other channel posts from the feed (`agents/channel.py` says what a channel module provides; add it to
    `CHANNELS` in `agents/__init__.py`). At the end of every pass `orchestrator.post_to_channels` gives each
    enabled channel its queue: feed entries it hasn't posted yet, newest first, refused ones last. A refusal
    costs nothing (the reason goes in `Mission.channel_errors` and the next entry is tried); network or key
    failures lose nothing and are retried next pass. Stub runs have no channels.
  - `1f916`: `agents/f916.py` posts the feed entry's title and text to 1F916 as our citizen. The server to join is
    config (`settings.f916_url`: Settings page, `MISSION_LOOP_1F916_URL`, else https://1f916.ai); the key file records
    the server that issued it (`api`) and posting always goes there (`f916.api`). On only
    when `data/1f916.json` has a key (`python run.py join-1f916 <handle>`, once). The site allows one post per
    UTC day (`data/1f916_state.json` remembers when it is spent), so a just-published article goes out that same
    pass if the day's post is free, and the rest wait, newest first.
  - `bluesky`: `agents/bluesky.py` announces the feed entry on Bluesky with a short post (title and dek, at most
    300 characters; a possibility in the dek keeps its "Possibility, not verified" label, and a pushed-through
    article starts with "[Failed automated checks]"). Its link card goes to the article on the site when
    a site address is set (Settings page or `MISSION_LOOP_SITE_URL`), else to its 1F916 post (1F916 comes first in `CHANNELS`, so a fresh one
    counts), else there is no link. On only with a handle and app password (never the account password): the
    Settings page checks them with Bluesky and saves them to `data/bluesky.json` (mode 600, wins over `.env`'s
    `BLUESKY_HANDLE` and `BLUESKY_APP_PASSWORD`); `python run.py check-bluesky` logs in without posting. A login saved on
    the Settings page keeps the server it was checked against; a `.env` login uses `BLUESKY_SERVICE` (default bsky.social). After each
    post it waits `POST_GAP` (10 minutes), so a backlog goes out about one a pass. `data/bluesky_state.json`
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
  conversation in that browser tab only, so the page's refreshes don't wipe it. It is not a worker: a one-paragraph prompt, at most 5 tool calls, tool results cut
  to 2,500 characters, only the last 4 messages the page sends back, no memory and nothing saved. Its tools come
  from the `loop` MCP server (`status_tools.py`: `loop_status`, `list_missions`, `mission_status`, `agent_steps`,
  `search_missions`), plus `search` when `BRAVE_API_KEY` is set. They only read the database, `data/activity.json`,
  the trails and `data/loop.log`, so asking never touches a running agent. By default it uses the small model with
  the loop's options (`settings.chat_model`: `chat_model` in settings.json, then `MISSION_LOOP_CHAT_MODEL`, else
  the Research Assistant's model), so Ollama reuses the loaded copy instead of reloading it.
- Trails: `trail.py` keeps each mission's last 40 agent steps (model thinking and replies, tool calls, tool
  results, each cut to 400 characters) in `data/trail/<slug>.jsonl` (`_finder.jsonl` for the finder). Only the
  loop writes them: `activity.begin` starts a trail, `llm.chat` and `researcher.tool_loop` add to it. Display-only.

## Operating it

The easy way: `python run.py ui`, then open http://127.0.0.1:8770. It lists every mission, shows briefs and
article previews, and has buttons for approve / send back / publish / requeue / push through / add mission / save
the finder's inspiration line and to start, stop or wake the loop (a loop started there runs in the background and logs to `data/loop.log`).
Its Settings page (`/settings`) shows the feeds, saves the site address and joins 1F916 (the same `f916.register`
as the CLI); a fresh clone needs no settings. `ui.py`
only records human decisions through the same store/orchestrator functions as the CLI; it never runs
agents. It binds to 127.0.0.1 and refuses cross-site form posts. While the loop runs, the UI highlights
the agent working at that moment, read from `data/activity.json` (written by `activity.py`). Small pixel
researchers (`assets/researchers.js`, decorative only) walk in from the side onto the cards of missions a model
is working on right now and the working agents' pills (none while no model runs), never more than two on the
page at once. Each character is in one worker's crew (`worker` in `ROSTER`: young ones the Research Assistant's,
tenured ones the Principal Investigator's), and 80% of a card's visitors (`FAVOR`) come from the crew of the
worker running it (`data-worker`), the rest from the other crew. On the Settings page each worker card has its
whole crew standing about. Each stage has its own random first wait, a
visitor stays 15-30 s, walks off, and the stage waits 10-20 s; the longest-waiting stage goes next. Visitors
come from a shuffled cast and their lines from a shuffled deck of ~200 academic quips, so nothing repeats soon.
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
| Publish an article | `python run.py review <slug>`; it is published on the next pass |
| Send back with notes | `python run.py reject <slug> --notes "..."` (brief → research, article → writer) |
| Revive a parked mission | `python run.py requeue <slug>` (resumes at writing if the brief was already approved) |
| Override a parked mission | `python run.py push <slug>` (its last failed draft goes to your review, marked as failed) |
| View the site | open `site/index.html`, or `python -m http.server 8765 -d site` |
| Subscribe | `site/feed.xml` (RSS) or `site/feed.json` (JSON Feed); set the site address on the Settings page once it is hosted |
| Turn on Bluesky posting | add your handle and an app password on the Settings page (or `BLUESKY_HANDLE` and `BLUESKY_APP_PASSWORD` in `.env`, then `python run.py check-bluesky`) |
| Turn on 1F916 posting | Join on the Settings page, or `python run.py join-1f916 <handle>` once (saves `data/1f916.json`); `status` and the UI show the queue |
| Ask what is going on | the chat bubble at the bottom right of the UI, or `python run.py ask "where is the grid mission at?"` |
| Pick each worker's model, or switch one off | the Workers cards on the Settings page (saved in `data/settings.json`) |
| Point at another Ollama | the LLM connection address on the Settings page, or `OLLAMA_HOST` in `.env` |
| Block a source site | add the domain to `blocked_domains.txt` |

Only one `run`/`loop` can work at a time (`data/loop.lock`). A UI action touches `data/WAKE`, so a
sleeping loop runs its next pass right away instead of waiting out `--every`. Before each pass the loop checks that Ollama
is up, so a closed Ollama app doesn't burn every mission's attempts. The loop sends a macOS notification
when a mission needs you (brief ready, article ready, parked) or was published.
