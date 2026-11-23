"""What a publishing channel is. Every channel posts from the feed (site/feed.json, one entry per meta.json).

A channel is a module with:
  KEY                  short id stored in Mission.channels, e.g. "1f916"
  NAME                 what the UI and `status` call it, e.g. "1F916"
  enabled() -> bool    False until it is set up on this machine (no key file means off)
  wait_until() -> datetime | None   no post before this time (a daily cap); None means it can post now
  post(entry, now) -> str           posts one feed entry (slug, title, dek, published_at, text, epic) and
                                    returns where it can be read. Raises NoAllowance or Refused below;
                                    any other exception means "try again next pass".
  sync(now)            optional, for a channel that mirrors the whole site (GitHub Pages): called before the
                       queue on every turn, so a page redrawn after it was posted (an epic renamed) goes out too.
                       Its exceptions count like post's.
  MANUAL_SKIPS_WAIT    optional, default False: True when wait_until is only our own politeness gap, so an
                       article a human asks to publish now may go before it (Bluesky); False for a real cap
                       the server enforces (1F916's one post a day).
  PACE                 optional timedelta: how far apart its posts go once it is capped (1F916 a day, Bluesky
                       its POST_GAP). With it the UI and `status` say how long its queue takes (backlog_time,
                       estimate; #119), counted from wait_until. No PACE means no cap and no estimate.

The orchestrator does the rest for every channel the same way: the queue is feed entries the channel has not
posted yet, newest first, refused ones last, with any a human asked to publish (Mission.publish_requests) ahead
of all of them; it records where each one went. Add the module to CHANNELS in agents/__init__.py, or set up a
custom channel (agents/custom.py) on the Settings page.
"""

from datetime import datetime, timedelta
import clock

class NoAllowance(Exception):
    """No post possible right now. daily=True: today's post is spent. False: a short rate limit, try later."""
    def __init__(self, msg: str, daily: bool = False):
        super().__init__(msg)
        self.daily = daily

class Refused(Exception):
    """The channel refused this entry. It cost nothing, so the next entry can go instead."""

def backlog_time(channel, n: int, now: datetime) -> timedelta | None:
    """How long until the last of n queued entries goes out: the channel's wait_until, then PACE between each.
    None for a channel with no PACE (no cap) or nothing queued."""
    pace = getattr(channel, "PACE", None)
    if not pace or n < 1:
        return None
    until = channel.wait_until()
    return max(timedelta(0), until - now if until else timedelta(0)) + (n - 1) * pace

def estimate(channel, n: int, now: datetime) -> str:
    """backlog_time in words with the pace, "about 3 weeks (one a day)", or "" with no cap (clock.backlog)."""
    return clock.backlog(backlog_time(channel, n, now), getattr(channel, "PACE", None))
