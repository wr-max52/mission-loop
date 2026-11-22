"""Publisher: writes a reviewed article into the local static site under site/.

site/
  index.html            list of everything published, newest first, filtered by tag (rebuilt on every publish)
  about/index.html      the pipeline playing an example run, and what each agent does (rebuilt with the index)
  assets/               the scripts the About page plays (copied from assets/ with the index)
  <slug>/index.html     the article, with links back to the list and to the newer and older article
  <slug>/meta.json      title, dek, published_at, epic and the article as plain text
  feed.json, feed.xml   JSON Feed and RSS of everything published, with full text
The index and both feeds are rebuilt from the meta.json files on every publish.

Open site/index.html in a browser, or serve it with `python -m http.server -d site`.
Nothing leaves this machine.
"""
import json
from pathlib import Path
import settings, store
from contracts import Mission, State, ResearchBrief, Article
from render import article_html, article_text, index_html, about_html, feed_json, feed_xml

SITE = Path("site")
ASSETS = Path(__file__).resolve().parent.parent / "assets"
ABOUT_ASSETS = ("researchers.js", "flow.js", "about.js")   # what the About page plays

def entries(site: Path | None = None) -> list[dict]:
    """Everything published, newest first: one dict per meta.json."""
    found = [json.loads(p.read_text()) for p in (site or SITE).glob("*/meta.json")]
    return sorted(found, key=lambda e: e["published_at"], reverse=True)

def rebuild_index(site: Path | None = None) -> Path:
    """site/index.html, feed.json and feed.xml, all from the same entries."""
    site = site or SITE
    site.mkdir(parents=True, exist_ok=True)   # a fresh clone has no site/ yet
    found = entries(site)
    (site / "feed.json").write_text(feed_json(found, settings.site_url()))
    (site / "feed.xml").write_text(feed_xml(found, settings.site_url()))
    out = site / "index.html"
    out.write_text(index_html(found))
    write_about(site)
    return out

def write_about(site: Path) -> None:
    """The About page and the scripts it plays, so the site works on its own wherever it is hosted."""
    (site / "about").mkdir(parents=True, exist_ok=True)
    (site / "about" / "index.html").write_text(about_html())
    refresh_assets(site)

def refresh_assets(site: Path | None = None) -> list[str]:
    """Copy the About page's scripts into site/assets/ where they differ from the code's; returns the ones copied.
    The site keeps its own copies (it is served on its own), so without this an updated script, the researchers'
    drawing say, stays old there until the next article rebuilds the index."""
    site = site or SITE
    (site / "assets").mkdir(parents=True, exist_ok=True)
    copied = []
    for name in ABOUT_ASSETS:
        fresh, there = (ASSETS / name).read_bytes(), site / "assets" / name
        if not there.exists() or there.read_bytes() != fresh:
            there.write_bytes(fresh)
            copied.append(name)
    return copied

def missing_text(site: Path | None = None) -> set[str]:
    """Slugs published before meta.json carried the article text."""
    return {e["slug"] for e in entries(site) if "text" not in e}

def epic_label(m: Mission) -> str:
    """The epic's title for the pill, or "" while it has none or its epic is only proposed."""
    epic = store.get_epic(m.epic) if m.epic else None
    return epic.title if epic and epic.status == "active" else ""

def connected(m: Mission, publishing: str = "") -> list[tuple[str, str, str]]:
    """(slug, title, why) for each published article linked to this one: what its page points to. publishing:
    the article being published right now, which counts as published already."""
    found = []
    for k in store.links_of(m.slug):
        other = store.get_mission(k.other(m.slug))
        out = other and (other.state == State.DONE or other.slug == publishing)
        data = store.load_artifact(other.slug, "article") if out else None
        if data:
            found.append((other.slug, Article(**data).title, k.why))
    return found

def write_page(m: Mission, brief: ResearchBrief, article: Article, published_at, publishing: str = "") -> Path:
    """The article page and its meta.json. Also used to redraw a published page when its epic or links change."""
    folder = SITE / m.slug
    folder.mkdir(parents=True, exist_ok=True)
    page = folder / "index.html"
    epic = epic_label(m)
    checks = dict(brief_checked=not m.unchecked_brief, article_checked=not m.unchecked_article)
    page.write_text(article_html(brief, article, published_at=published_at, epic=epic, slug=m.slug,
                                 connected=connected(m, publishing), **checks))
    (folder / "meta.json").write_text(json.dumps({
        "slug": m.slug, "title": article.title, "dek": article.dek, "published_at": published_at.isoformat(),
        "epic": epic, "text": article_text(brief, article, **checks)}))
    return page

def publish(m: Mission, brief: ResearchBrief, article: Article, now) -> Path:
    """The article's page, then the pages of the published articles linked to it, so they point back."""
    page = write_page(m, brief, article, now)
    for slug, _, _ in connected(m):
        other = store.get_mission(slug)
        b = store.load_artifact(slug, "brief")
        if other and b and other.published_at:
            write_page(other, ResearchBrief(**b), Article(**store.load_artifact(slug, "article")), other.published_at,
                       publishing=m.slug)
    rebuild_index()
    return page
