"""Publisher: writes a reviewed article into the local static site under site/.

site/
  index.html            list of everything published, newest first (rebuilt on every publish)
  <slug>/index.html     the article
  <slug>/meta.json      title, dek, published_at, epic; the index is rebuilt from these

Open site/index.html in a browser, or serve it with `python -m http.server -d site`.
Nothing leaves this machine.
"""
import json
from pathlib import Path
import store
from contracts import Mission, ResearchBrief, Article
from render import article_html, index_html

SITE = Path("site")

def rebuild_index(site: Path | None = None) -> Path:
    site = site or SITE
    entries = [json.loads(p.read_text()) for p in site.glob("*/meta.json")]
    entries.sort(key=lambda e: e["published_at"], reverse=True)
    out = site / "index.html"
    out.write_text(index_html(entries))
    return out

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
    page.write_text(article_html(brief, article, published_at=published_at, brief_checked=not m.unchecked_brief,
                                 article_checked=not m.unchecked_article, epic=epic))
    (folder / "meta.json").write_text(json.dumps({
        "slug": m.slug, "title": article.title, "dek": article.dek, "published_at": published_at.isoformat(),
        "epic": epic}))
    return page

def publish(m: Mission, brief: ResearchBrief, article: Article, now) -> Path:
    page = write_page(m, brief, article, now)
    rebuild_index()
    return page
