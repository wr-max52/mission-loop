"""What a publishing channel is. Every channel posts from the feed (site/feed.json, one entry per meta.json).

A channel is a module with:
  KEY                  short id stored in Mission.channels, e.g. "1f916"
  NAME                 what the UI and `status` call it, e.g. "1F916"
  enabled() -> bool    False until it is set up on this machine (no key file means off)
  wait_until() -> datetime | None   no post before this time (a daily cap); None means it can post now
  post(entry, now) -> str           posts one feed entry (slug, title, dek, published_at, text, epic) and
                                    returns where it can be read. Raises NoAllowance or Refused below;
                                    any other exception means "try again next pass".

The orchestrator does the rest for every channel the same way: the queue is feed entries the channel has not
posted yet, newest first, refused ones last; it records where each one went. Add the module to CHANNELS in
agents/__init__.py.
"""

class NoAllowance(Exception):
    """No post possible right now. daily=True: today's post is spent. False: a short rate limit, try later."""
    def __init__(self, msg: str, daily: bool = False):
        super().__init__(msg)
        self.daily = daily

class Refused(Exception):
    """The channel refused this entry. It cost nothing, so the next entry can go instead."""
