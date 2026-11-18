# AGENTS.md

## Critical constraint: missions stay local

The code lives in the private GitHub repo `wr-max52/mission-loop` (`origin`). Push only when asked.
Never commit or push `missions.yaml`, `missions/`, `data/`, `site/`, or `.env`; they stay on this
machine. A local `pre-push` hook blocks commits containing them. Branches started before the repo
moved to GitHub must be rebased onto `origin/main` before pushing. Publishing writes a static site
into `site/` on this machine; nothing is uploaded anywhere.

## Running

- Interpreter: Python 3.12 (`/opt/homebrew/bin/python3.12`). The code uses 3.10+ union syntax.
- Activate the venv first: `source .venv/bin/activate`
- CLI: `python run.py <command>` — `ui init add run loop stop approve reject review requeue show status`
- Tests: `pytest -q` (expects 94 passed; no Ollama or network needed)
- `MISSION_LOOP_STUBS=1` runs every agent on stubs (no Ollama, no network).
- Specs for how it was built: `milestone1.md`, `milestone2.md`.

## Pipeline (v1)

```
QUEUED -> RESEARCHING -> BRIEF_READY -> AWAITING_COMMENTS -> WRITING -> ARTICLE_READY -> DONE (published)
          (validator,     (human:        (skipped while       (article     (human:
           3 attempts)     approve)       outreach is off)     check, 3     review)
                                                               attempts)
```

- Research: `agents/researcher.py` (Ollama + MCP search/fetch), checked by `checks/validator.py`.
  A small model (`MISSION_LOOP_SEARCH_MODEL`, default `bonsai-2:4b`) runs the search/fetch tool loop;
  the main model (`MISSION_LOOP_MODEL`) writes the brief. With outreach on, a second small-model run
  finds contacts, and code checks their names and emails before the main model is called.
- Writing: `agents/writer.py` (one structured Ollama call), checked by `checks/article_check.py`.
- Publishing: `agents/publisher.py` writes `site/<slug>/index.html` and rebuilds `site/index.html`.
- Outreach is OFF (milestone 4): `interview` emails nobody, so the comment wait is skipped, and
  research doesn't look for points of contact (no POC email check). The switch is
  `MISSION_LOOP_OUTREACH=1` in `settings.py`; turn it on only once real outreach exists.
- Claims have a `kind`: `fact` or `possibility`. Research explores widely, rules out the unlikely, reports
  what stays possible, and verifies facts separately. Code can't verify a possibility, so it enforces the
  label instead: possibilities must say "It is possible that", a brief needs at least one fact, and the
  article marks each one "Possibility, not verified".
- `agents/__init__.py` is still the only switchboard. Validators never call the LLM.

## Operating it

The easy way: `python run.py ui`, then open http://127.0.0.1:8770. It lists every mission, shows briefs and
article previews, and has buttons for approve / send back / publish / requeue / add mission and to start,
stop or wake the loop (a loop started there runs in the background and logs to `data/loop.log`). `ui.py`
only records human decisions through the same store/orchestrator functions as the CLI; it never runs
agents. It binds to 127.0.0.1 and refuses cross-site form posts. While the loop runs, the UI highlights
the agent working at that moment, read from `data/activity.json` (written by `activity.py`).

| You want to | Command |
|---|---|
| Open the UI | `python run.py ui` → http://127.0.0.1:8770 |
| Add a mission | edit `missions.yaml` then `python run.py init`, or `python run.py add <slug> "<topic>" --objective "..."` |
| Run once until blocked | `python run.py run` |
| Run forever | `python run.py loop --every 15` (or install `ops/com.missionloop.loop.plist`, see the comments in it) |
| Stop | `python run.py stop` (finishes the current task first) |
| Read a brief / article | `python run.py show <slug>`; the article preview is `missions/<slug>/preview.html` |
| Approve a brief | `python run.py approve <slug>` |
| Publish an article | `python run.py review <slug>`; it is published on the next pass |
| Send back with notes | `python run.py reject <slug> --notes "..."` (brief → research, article → writer) |
| Revive a parked mission | `python run.py requeue <slug>` (resumes at writing if the brief was already approved) |
| View the site | open `site/index.html`, or `python -m http.server 8765 -d site` |
| Block a source site | add the domain to `blocked_domains.txt` |

Only one `run`/`loop` can work at a time (`data/loop.lock`). A UI action touches `data/WAKE`, so a
sleeping loop runs its next pass right away instead of waiting out `--every`. Before each pass the loop checks that Ollama
is up, so a closed Ollama app doesn't burn every mission's attempts. The loop sends a macOS notification
when a mission needs you (brief ready, article ready, parked) or was published.
