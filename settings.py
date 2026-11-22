"""Project-wide switches: read from the environment (.env is loaded by run.py first), and the few the UI's
Settings page saves in data/settings.json on this machine. A fresh clone needs neither."""
import json, os
from pathlib import Path
from urllib.parse import urlparse

# Outreach (milestone 4) emails points of contact and waits for their comments. Until it is built
# this stays off: research doesn't look for contacts and the validator doesn't check their emails.
OUTREACH = os.environ.get("MISSION_LOOP_OUTREACH") == "1"

SETTINGS_FILE = Path("data/settings.json")

# Where the published site lives on the web, e.g. https://example.com/news/. Optional: without it the feeds in
# site/ use links relative to the feed file, which work wherever the folder is served. The UI's saved value
# wins over MISSION_LOOP_SITE_URL in .env.
SITE_URL = os.environ.get("MISSION_LOOP_SITE_URL", "").strip()

def saved() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        return {}

def save(**values) -> None:
    """Set values in data/settings.json; an empty value removes the setting."""
    data = saved() | values
    data = {k: v for k, v in data.items() if v}
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, indent=2) + "\n")

def site_url() -> str:
    return saved().get("site_url") or SITE_URL

def set_site_url(url: str) -> str:
    """Save the site address from the UI ("" clears it). Raises ValueError unless it is an http(s) address."""
    url = url.strip()
    if url:
        parts = urlparse(url)
        if parts.scheme not in ("http", "https") or not parts.netloc or any(c.isspace() for c in url):
            raise ValueError("not an http(s) address")
    save(site_url=url)
    return url

def animations() -> bool:
    """The little pixel researchers on the Missions page. On unless turned off on the Settings page."""
    return saved().get("animations") != "off"

def set_animations(on: bool) -> None:
    save(animations="" if on else "off")   # nothing stored while on, so a fresh install has them
