# AGENTS.md

## Critical constraint: local only

Everything in this project stays on this machine. Do NOT add a git remote and do NOT push.
All commits (including `m1`) and tags are local only.

## Running

- Interpreter: Python 3.12 (`/opt/homebrew/bin/python3.12`). The code uses 3.10+ union syntax.
- Activate the venv first: `source .venv/bin/activate`
- CLI: `python run.py <command>` — `init add run stop approve review status`
- Tests: `pytest -q` (expects 4 passed)
- Manual walkthrough: see `milestone1.md` section 5.
