"""The UI's languages. Every string the UI shows is written in English in the code and wrapped in `_()`, which
looks it up in the language picked on the Settings page (`settings.language()`): `locales/<code>.json` maps each
English string to its translation, and anything missing falls back to the English. Placeholders like `{n}` are
filled after the lookup (`_("{n} queued").format(n=3)`), so a translation may put them in any order.

The published site and the agents' prompts stay English; `english()` forces English for a block of code (the
published About page draws the same flow chart as the UI). The pixel researchers' lines live in
`assets/lines/<code>.js`, written in each language rather than translated, and page() loads the one for the UI's
language before `assets/researchers.js`.
"""
import contextvars, json
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

LOCALES = Path(__file__).parent / "locales"
LINES = Path(__file__).parent / "assets" / "lines"
_forced = contextvars.ContextVar("i18n_forced", default="")

@lru_cache(maxsize=None)
def table(code: str) -> dict[str, str]:
    path = LOCALES / f"{code}.json"
    return json.loads(path.read_text()) if path.exists() else {}

def current() -> str:
    import settings
    return _forced.get() or settings.language()

def _(text: str) -> str:
    """The text in the UI's language, or the English when there is no translation."""
    code = current()
    return text if code == "en" else table(code).get(text) or text

def _n(one: str, many: str, n: int) -> str:
    """A count's words: `one` when n is 1, else `many`. Both may hold {n}, which is filled in here."""
    return _(one if n == 1 else many).format(n=n)

@contextmanager
def english():
    token = _forced.set("en")
    try:
        yield
    finally:
        _forced.reset(token)

def lines_script() -> str:
    """The <script> that gives the pixel researchers their lines in the UI's language (none for English)."""
    code = current()
    return f'<script src="/assets/lines/{code}.js"></script>' if code != "en" and (LINES / f"{code}.js").exists() else ""
