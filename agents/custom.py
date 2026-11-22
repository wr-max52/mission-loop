"""Custom channels: anything else a published article should reach, set up on the Settings page.

Each one is {id, name, kind, target, format} in data/settings.json (settings.custom_channels) and behaves like
any other channel (agents/channel.py): it posts feed entries, newest first, and has no daily cap.

kind     webhook: POST the article to `target`, an http(s) address. 2xx is success; a JSON reply with a "url"
                  is recorded as where it can be read. 429 means "later", other 4xx refuse this article,
                  anything else is retried next pass.
         folder:  write the article to `target`/<slug>.<ext>, a full path on this machine (made if missing).
format   json:     one JSON Feed 1.1 item (id, url, title, summary, content_text, date_published, tags),
                   the same as that article's entry in site/feed.json. Content-Type application/json.
         markdown: "# title", the dek in italics, the link (when a site address is set), then the full text.
         text:     the same as plain text.
"""
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
import httpx
import settings
from agents.channel import NoAllowance, Refused

TRANSPORT = None                           # tests swap in an httpx.MockTransport
EXT = {"json": "json", "markdown": "md", "text": "txt"}
TYPES = {"json": "application/json", "markdown": "text/markdown; charset=utf-8", "text": "text/plain; charset=utf-8"}

def link(entry: dict) -> str:
    """The article's address on the hosted site, or "" while no site address is set."""
    base = settings.site_url()
    return f'{base.rstrip("/")}/{entry["slug"]}/' if base else ""

def render(entry: dict, fmt: str) -> str:
    text = entry.get("text") or entry.get("dek", "")
    if fmt == "json":
        item = {"id": entry["slug"], "url": link(entry) or f'{entry["slug"]}/', "title": entry["title"],
                "summary": entry.get("dek", ""), "content_text": text, "date_published": entry["published_at"],
                **({"tags": [entry["epic"]]} if entry.get("epic") else {})}
        return json.dumps(item, indent=2, ensure_ascii=False) + "\n"
    url = link(entry)
    if fmt == "markdown":
        head = [f'# {entry["title"]}', f'*{entry["dek"]}*' if entry.get("dek") else "", f"<{url}>" if url else ""]
    else:
        head = [entry["title"], entry.get("dek", ""), url]
    return "\n\n".join([h for h in head if h] + [text]) + "\n"

class Channel:
    """One custom channel, built from its settings entry."""
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.KEY, self.NAME = f'custom-{cfg["id"]}', cfg["name"]

    def enabled(self) -> bool:
        return True

    def wait_until(self) -> datetime | None:
        return None

    def post(self, entry: dict, now: datetime) -> str:
        fmt, target = self.cfg["format"], self.cfg["target"]
        body = render(entry, fmt)
        if self.cfg["kind"] == "folder":
            folder = Path(target)
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f'{entry["slug"]}.{EXT[fmt]}'
            path.write_text(body)
            return str(path)
        with httpx.Client(timeout=30, transport=TRANSPORT) as c:
            r = c.post(target, content=body.encode(), headers={"Content-Type": TYPES[fmt], "User-Agent": "mission-loop"})
        if r.status_code == 429:
            raise NoAllowance(f"{self.NAME} asked us to slow down")
        if 400 <= r.status_code < 500 and r.status_code not in (401, 403, 404):
            raise Refused(f"HTTP {r.status_code}: {r.text[:200]}")
        r.raise_for_status()               # 401/403/404 and 5xx: the setup or the server, so try again next pass
        try:
            where = r.json().get("url", "")
        except (ValueError, AttributeError):
            where = ""
        # never record the webhook address itself: it often carries a secret token
        return where if str(where).startswith(("https://", "http://")) else f"sent to {urlparse(target).netloc}"

def channels() -> list[Channel]:
    return [Channel(c) for c in settings.custom_channels()]
