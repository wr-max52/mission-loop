"""Project-wide switches read from the environment (.env is loaded by run.py first)."""
import os

# Outreach (milestone 4) emails points of contact and waits for their comments. Until it is built
# this stays off: research doesn't look for contacts and the validator doesn't check their emails.
OUTREACH = os.environ.get("MISSION_LOOP_OUTREACH") == "1"

# Where the published site lives on the web, e.g. https://example.com/news/. Optional: without it the feeds in
# site/ use links relative to the feed file, which work wherever the folder is served.
SITE_URL = os.environ.get("MISSION_LOOP_SITE_URL", "").strip()
