#!/usr/bin/env bash
# First launch, and every launch after: makes .venv and installs the packages the first time (and again whenever
# requirements.txt changes), says what research still needs, then opens the UI in your browser.
# Anything after ./start.sh goes to `python run.py ui`, e.g. ./start.sh --port 8771
set -euo pipefail
cd "$(dirname "$0")"

PY=""
for candidate in python3.12 python3.13 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Mission Loop needs Python 3.10 or newer. On a Mac: brew install python@3.12, then run ./start.sh again."
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Making a virtual environment in .venv with $PY..."
  "$PY" -m venv .venv || { echo "Couldn't make .venv with $PY."; exit 1; }
fi

STAMP=.venv/.mission-loop-requirements
if [ ! -f "$STAMP" ] || ! cmp -s requirements.txt "$STAMP"; then
  echo "Installing packages (the first time takes a minute)..."
  if ! .venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements.txt; then
    echo "Couldn't install the packages. Check your internet connection, then run ./start.sh again."
    exit 1
  fi
  cp requirements.txt "$STAMP"
fi

command -v npx >/dev/null 2>&1 || echo "Note: npx isn't installed, so research can't search the web. Install it: brew install node"
command -v uvx >/dev/null 2>&1 || echo "Note: uvx isn't installed, so research reads pages with fewer tools. Install it: brew install uv"
command -v ollama >/dev/null 2>&1 || echo "Note: Ollama isn't installed. Get it at https://ollama.com/download, or use a cloud model's API key in the app."

exec .venv/bin/python run.py ui "$@"
