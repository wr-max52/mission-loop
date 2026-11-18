"""Publisher: writes a reviewed article into the local static site under site/.

site/
  index.html            list of everything published, newest first (rebuilt on every publish)
  <slug>/index.html     the article
  <slug>/meta.json      title, dek, published_at; the index is rebuilt from these

Open site/index.html in a browser, or serve it with `python -m http.server -d site`.
Nothing leaves this machine.
"""
import json
from pathlib import Path
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

def publish(m: Mission, brief: ResearchBrief, article: Article, now) -> Path:
    folder = SITE / m.slug
    folder.mkdir(parents=True, exist_ok=True)
    page = folder / "index.html"
    page.write_text(article_html(brief, article, published_at=now, brief_checked=not m.unchecked_brief,
                                 article_checked=not m.unchecked_article))
    (folder / "meta.json").write_text(json.dumps({
        "slug": m.slug, "title": article.title, "dek": article.dek, "published_at": now.isoformat()}))
    rebuild_index()
    return page
