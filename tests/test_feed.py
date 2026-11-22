import json
from datetime import timedelta
from pathlib import Path
from xml.etree import ElementTree as ET
import pytest
import settings, store
from contracts import Mission, State
from test_publish import T0, run_at, flag, get, to_article_ready

pytestmark = pytest.mark.usefixtures("stub_agents")
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"

def publish(slug, at):
    to_article_ready(slug)
    flag(slug, reviewed=True)
    run_at(at)
    assert get(slug).state == State.DONE

def feeds():
    return json.loads(Path("site/feed.json").read_text()), ET.parse("site/feed.xml").getroot()

def test_publishing_writes_both_feeds_newest_first_with_full_text():
    publish("older", T0 + timedelta(hours=49))
    publish("newer", T0 + timedelta(hours=50))
    jf, rss = feeds()
    assert jf["version"] == "https://jsonfeed.org/version/1.1" and "feed_url" not in jf
    assert [i["id"] for i in jf["items"]] == ["newer", "older"]
    item = jf["items"][0]
    assert item["url"] == "newer/" and "Stub claim [1]" in item["content_text"] and "## Sources" in item["content_text"]
    items = rss.findall("channel/item")
    assert [i.findtext("guid") for i in items] == ["newer", "older"]
    assert items[0].findtext("link") == "newer/" and "<h2>Sources</h2>" in items[0].findtext(CONTENT)
    index = Path("site/index.html").read_text()
    assert 'type="application/feed+json"' in index and 'href="feed.xml"' in index

def test_site_url_makes_links_full_and_keeps_ids(monkeypatch):
    publish("p", T0 + timedelta(hours=49))
    monkeypatch.setattr(settings, "SITE_URL", "https://example.com/news")
    run_at(T0 + timedelta(hours=50))                   # the next pass rebuilds with the new address
    jf, rss = feeds()
    assert jf["feed_url"] == "https://example.com/news/feed.json"
    assert jf["items"][0]["id"] == "p" and jf["items"][0]["url"] == "https://example.com/news/p/"
    assert rss.find("channel/item/link").text == "https://example.com/news/p/"

def test_articles_published_before_the_feed_get_their_text_on_the_next_pass():
    publish("old", T0 + timedelta(hours=49))
    meta = Path("site/old/meta.json")
    data = json.loads(meta.read_text())
    del data["text"]                                   # how meta.json looked before this change
    meta.write_text(json.dumps(data))
    Path("site/feed.json").unlink()
    run_at(T0 + timedelta(hours=50))
    assert "Stub claim" in json.loads(meta.read_text())["text"]
    assert "Stub claim" in feeds()[0]["items"][0]["content_text"]

def test_feed_text_is_escaped_and_failed_checks_are_marked():
    from render import feed_json, feed_xml
    entry = {"slug": "x", "title": "Fish & <chips>", "dek": "d", "published_at": T0.isoformat(),
             "text": "d\n\n[Failed automated checks: see the note at the end.]\n\n## A <b>\n\nOne & two."}
    rss = ET.fromstring(feed_xml([entry]))
    assert rss.findtext("channel/item/title") == "Fish & <chips>"
    assert "<h2>A &lt;b&gt;</h2>" in rss.findtext(f"channel/item/{CONTENT}")
    assert "Failed automated checks" in json.loads(feed_json([entry]))["items"][0]["content_text"]

def test_no_feed_until_something_is_published():
    store.save_mission(Mission(slug="q", topic="x"))
    run_at(T0)
    assert not Path("site/feed.json").exists()
