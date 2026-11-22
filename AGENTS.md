# AGENTS.md

## Critical constraint: missions stay local

The code lives in the private GitHub repo `wr-max52/mission-loop` (`origin`). Push only when asked.
Never commit or push `missions.yaml`, `missions/`, `data/`, `site/`, or `.env`; they stay on this
machine. A local `pre-push` hook blocks commits containing them. Branches started before the repo
moved to GitHub must be rebased onto `origin/main` before pushing. Publishing writes a static site
into `site/` on this machine, and posts the article text to 1f916.ai only once `data/1f916.json` holds a key.
That file is the citizen's whole identity (no recovery): never commit, print or move it.

## Running

- Interpreter: Python 3.12 (`/opt/homebrew/bin/python3.12`). The code uses 3.10+ union syntax.
- Activate the venv first: `source .venv/bin/activate`
- CLI: `python run.py <command>` — `ui init add inspire run loop stop approve reject review requeue push show status join-1f916 epics epic rename-epic`
- Tests: `pytest -q` (expects 194 passed; no Ollama or network needed; any request to 1F916 fails the test)
- `MISSION_LOOP_STUBS=1` runs every agent on stubs (no Ollama, no network) and turns the 1F916 channel off.
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
- Research: `agents/researcher.py` (Ollama + MCP search/fetch), checked by `checks/validator.py`.
  A small model (`MISSION_LOOP_SEARCH_MODEL`, default `bonsai-2:4b`) runs the search/fetch tool loop
  for at most 15 minutes (`SOURCE_TIME_LIMIT`), then the main model (`MISSION_LOOP_MODEL`) writes the
  brief from whatever was found. `page_tools.py` adds `find_in_page` (exact sentences from the text
  the validator checks) and `page_links` (a page's links to other sites), and requests only public
  addresses. `related.py` (code, no model) adds up to 3 published articles most like the mission by
  word overlap, any epic (its own epic ranks a little higher), with the possibilities they left open
  and the pages they cited. They are background only: the site never leaves this machine, so they
  can't be cited, but their cited pages can be fetched again and cited fresh. With outreach on, a second small-model run
  finds contacts, and code checks their names and emails before the main model is called.
- Writing: `agents/writer.py` (one structured Ollama call), checked by `checks/article_check.py`.
- Publishing goes out to channels, recorded per mission in `Mission.channels` (key -> when and where):
  - `site`: `agents/publisher.py` writes `site/<slug>/index.html` and `meta.json` (with the article as plain
    text, `render.article_text`), then rebuilds `site/index.html` and the feeds `site/feed.json` (JSON Feed 1.1)
    and `site/feed.xml` (RSS 2.0) from the `meta.json` files, newest first, full text. Always on; reaching it is
    what makes a mission DONE. Feed links are relative unless `MISSION_LOOP_SITE_URL` is set in `.env`; entry
    ids are the slugs either way. Each pass starts by giving older `meta.json` files their text and rebuilding.
  - Every other channel posts from the feed (`agents/channel.py` says what a channel module provides; add it to
    `CHANNELS` in `agents/__init__.py`). At the end of every pass `orchestrator.post_to_channels` gives each
    enabled channel its queue: feed entries it hasn't posted yet, newest first, refused ones last. A refusal
    costs nothing (the reason goes in `Mission.channel_errors` and the next entry is tried); network or key
    failures lose nothing and are retried next pass. Stub runs have no channels.
  - `1f916`: `agents/f916.py` posts the feed entry's title and text to https://1f916.ai as our citizen. On only
    when `data/1f916.json` has a key (`python run.py join-1f916 <handle>`, once). The site allows one post per
    UTC day (`data/1f916_state.json` remembers when it is spent), so a just-published article goes out that same
    pass if the day's post is free, and the rest wait, newest first.
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

## Operating it

The easy way: `python run.py ui`, then open http://127.0.0.1:8770. It lists every mission, shows briefs and
article previews, and has buttons for approve / send back / publish / requeue / push through / add mission / save
the finder's inspiration line and to start, stop or wake the loop (a loop started there runs in the background and logs to `data/loop.log`). `ui.py`
only records human decisions through the same store/orchestrator functions as the CLI; it never runs
agents. It binds to 127.0.0.1 and refuses cross-site form posts. While the loop runs, the UI highlights
the agent working at that moment, read from `data/activity.json` (written by `activity.py`).

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
| Subscribe | `site/feed.xml` (RSS) or `site/feed.json` (JSON Feed); set `MISSION_LOOP_SITE_URL` once it is hosted |
| Turn on 1F916 posting | `python run.py join-1f916 <handle>` once (saves `data/1f916.json`); `status` and the UI show the queue |
| Block a source site | add the domain to `blocked_domains.txt` |

Only one `run`/`loop` can work at a time (`data/loop.lock`). A UI action touches `data/WAKE`, so a
sleeping loop runs its next pass right away instead of waiting out `--every`. Before each pass the loop checks that Ollama
is up, so a closed Ollama app doesn't burn every mission's attempts. The loop sends a macOS notification
when a mission needs you (brief ready, article ready, parked) or was published.
