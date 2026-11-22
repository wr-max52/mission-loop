"""Publisher: writes a reviewed article into the local static site under site/.

site/
  index.html            list of everything published, newest first (rebuilt on every publish)
  <slug>/index.html     the article
  <slug>/meta.json      title, dek, published_at, epic and the article as plain text
  feed.json, feed.xml   JSON Feed and RSS of everything published, with full text
The index and both feeds are rebuilt from the meta.json files on every publish.

Open site/index.html in a browser, or serve it with `python -m http.server -d site`.
Nothing leaves this machine.
"""
import json
from pathlib import Path
import settings, store
from contracts import Mission, ResearchBrief, Article
from render import article_html, article_text, index_html, feed_json, feed_xml

SITE = Path("site")

def entries(site: Path | None = None) -> list[dict]:
    """Everything published, newest first: one dict per meta.json."""
    found = [json.loads(p.read_text()) for p in (site or SITE).glob("*/meta.json")]
    return sorted(found, key=lambda e: e["published_at"], reverse=True)

def rebuild_index(site: Path | None = None) -> Path:
    """site/index.html, feed.json and feed.xml, all from the same entries."""
    site = site or SITE
    found = entries(site)
    (site / "feed.json").write_text(feed_json(found, settings.site_url()))
    (site / "feed.xml").write_text(feed_xml(found, settings.site_url()))
    out = site / "index.html"
    out.write_text(index_html(found))
    return out

def missing_text(site: Path | None = None) -> set[str]:
    """Slugs published before meta.json carried the article text."""
    return {e["slug"] for e in entries(site) if "text" not in e}

def epic_label(m: Mission) -> str:
    """The epic's title for the pill, or "" while it has none or its epic is only proposed."""
    epic = store.get_epic(m.epic) if m.epic else None
    return epic.title if epic and epic.status == "active" else ""

def write_page(m: Mission, brief: ResearchBrief, article: Article, published_at) -> Path:
    """The article page and its meta.json. Also used to redraw a published page when its epic changes."""
    folder = SITE / m.slug
    folder.mkdir(parents=True, exist_ok=True)
    page = folder / "index.html"
    epic = epic_label(m)
    checks = dict(brief_checked=not m.unchecked_brief, article_checked=not m.unchecked_article)
    page.write_text(article_html(brief, article, published_at=published_at, epic=epic, **checks))
    (folder / "meta.json").write_text(json.dumps({
        "slug": m.slug, "title": article.title, "dek": article.dek, "published_at": published_at.isoformat(),
        "epic": epic, "text": article_text(brief, article, **checks)}))
    return page

def publish(m: Mission, brief: ResearchBrief, article: Article, now) -> Path:
    page = write_page(m, brief, article, now)
    rebuild_index()
    return page
