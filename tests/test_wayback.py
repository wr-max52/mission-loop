"""The Wayback Machine: snapshot addresses count as their original site, dead pages fall back to the
archive, and the research agent can list a page's archived versions. No request leaves the machine."""
import httpx
import pytest
import page_tools, render, wayback
from checks import validator
from contracts import Mission
from tests.test_validator import brief, M, PAGES

LIVE = "https://93.184.216.34/report"
COPY = f"https://web.archive.org/web/20230105120000/{LIVE}"
OLD_SENTENCE = "The agency said the program served 40,000 families in its first year."

def archive(monkeypatch, available=None, cdx=None):
    """Answer the archive's two endpoints with fixed JSON; return the requests made."""
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path == "/wayback/available":
            return httpx.Response(200, json=available or {"archived_snapshots": {}})
        if request.url.path == "/cdx/search/cdx":
            return httpx.Response(200, json=cdx or [])
        return httpx.Response(404)
    monkeypatch.setattr(wayback, "TRANSPORT", httpx.MockTransport(handler))
    return calls

def closest(timestamp="20230105120000", page=LIVE, status="200"):
    return {"archived_snapshots": {"closest": {"available": True, "status": status, "timestamp": timestamp,
                                               "url": f"http://web.archive.org/web/{timestamp}/{page}"}}}

# --- addresses ------------------------------------------------------------------------------------

@pytest.mark.parametrize("url, page", [
    (COPY, LIVE),
    (f"https://web.archive.org/web/20230105120000id_/{LIVE}", LIVE),
    ("http://web.archive.org/web/2023/example.com/a", "http://example.com/a"),
    ("https://example.com/a", None),
])
def test_snapshot_addresses_name_their_original_page(url, page):
    assert wayback.original(url) == page

def test_snapshots_are_downloaded_raw_and_shown_to_readers_with_the_banner():
    assert wayback.raw(COPY) == f"https://web.archive.org/web/20230105120000id_/{LIVE}"
    assert wayback.raw(LIVE) == LIVE
    assert wayback.day_of(COPY) == "2023-01-05" and wayback.day_of(LIVE) == ""

@pytest.mark.parametrize("text, stamp", [("2021-05-01", "20210501"), ("2021-05", "202105"), ("2021", "2021"),
                                         ("last May", ""), ("1850-01-01", ""), ("2021-5-1", "")])
def test_dates_become_archive_timestamps(text, stamp):
    assert wayback.as_timestamp(text) == stamp

# --- archive lookups ------------------------------------------------------------------------------

def test_closest_copy(monkeypatch):
    calls = archive(monkeypatch, available=closest())
    assert wayback.snapshot(LIVE, "20230101") == ("20230105120000", COPY)
    assert calls[0].url.params["timestamp"] == "20230101"

@pytest.mark.parametrize("answer", [{"archived_snapshots": {}}, closest(status="404")])
def test_no_good_copy(monkeypatch, answer):
    archive(monkeypatch, available=answer)
    assert wayback.snapshot(LIVE) is None

def test_archive_down_is_reported(monkeypatch):
    monkeypatch.setattr(wayback, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(wayback.Unavailable):
        wayback.snapshot(LIVE)

def test_history_keeps_only_changes_spread_over_time(monkeypatch):
    rows = [["timestamp", "original", "digest"]] + [[f"20{y:02d}0101000000", LIVE, "A" if y < 5 else f"D{y}"]
                                                     for y in range(30)]
    calls = archive(monkeypatch, cdx=rows)
    found = wayback.history(LIVE, limit=4)
    assert [ts[:4] for ts, _ in found] == ["2000", "2012", "2021", "2029"]   # first and last always kept
    assert found[0][1] == f"https://web.archive.org/web/20000101000000/{LIVE}"
    assert calls[0].url.params["collapse"] == "timestamp:6"

def test_history_drops_months_where_nothing_changed(monkeypatch):
    archive(monkeypatch, cdx=[["timestamp", "original", "digest"], ["20200101", LIVE, "A"],
                              ["20200201", LIVE, "A"], ["20200301", LIVE, "B"]])
    assert [ts for ts, _ in wayback.history(LIVE)] == ["20200101", "20200301"]

# --- the validator --------------------------------------------------------------------------------

@pytest.fixture
def web(monkeypatch):
    """The validator's fixed pages (see test_validator), with no archived copies."""
    monkeypatch.setattr(validator, "fetch", lambda url: PAGES.get(url))
    monkeypatch.setattr(wayback, "snapshot", lambda page, when="": None)

def test_archived_sources_count_as_their_own_sites(web):
    b = brief()
    for s in b.sources[:3]:
        url = f"https://web.archive.org/web/20220101000000/{s.url}"
        PAGES[url] = PAGES[s.url]
        s.url = url
    try:
        ok, notes = validator.validate(M, b)
    finally:
        for s in b.sources[:3]:
            PAGES.pop(s.url)
    assert ok, notes

def test_archived_copy_of_a_blocked_site_is_still_blocked(web, monkeypatch):
    monkeypatch.setattr(validator, "blocked_domains", lambda path=None: {"a.com"})
    b = brief()
    b.sources[0].url = "https://web.archive.org/web/2022/https://a.com/1"
    ok, notes = validator.validate(M, b)
    assert "s1: a.com is on the blocked list" in notes

def test_dead_source_points_to_its_archived_copy(web, monkeypatch):
    monkeypatch.setattr(wayback, "snapshot", lambda page, when="": ("20230105120000", COPY))
    b = brief()
    b.sources[1].url = LIVE
    ok, notes = validator.validate(M, b)
    assert f"s2: could not fetch {LIVE}. The Wayback Machine has a copy from 2023-01-05: cite {COPY}" in notes

def test_validator_reads_the_raw_copy(monkeypatch):
    seen = []
    monkeypatch.setattr(validator, "_cache", {})
    monkeypatch.setattr(validator.httpx, "get", lambda url, **kw: seen.append(url) or httpx.Response(200, text="<p>x</p>"))
    validator.fetch(COPY)
    assert seen == [wayback.raw(COPY)]

# --- the research agent's page tools ---------------------------------------------------------------

@pytest.fixture
def pages(monkeypatch):
    """LIVE is dead (404); its archived copy says OLD_SENTENCE. Returns the downloaded addresses."""
    monkeypatch.setattr(page_tools, "_pages", {})
    monkeypatch.setattr(validator, "blocked_domains", lambda path=None: {"bad.example"})
    downloads = []
    def download(url):
        downloads.append(url)
        if "web.archive.org" not in url:
            raise page_tools.CannotRead("the site answered with status 404")
        return '<p><a href="/about">About</a> <a href="https://www.congress.gov/bill/9">The bill</a></p>'
    monkeypatch.setattr(page_tools, "_download", download)
    monkeypatch.setattr(validator, "readable_text", lambda html: f"Some opening words here.\n{OLD_SENTENCE}")
    monkeypatch.setattr(wayback, "snapshot", lambda page, when="": ("20230105120000", COPY))
    return downloads

def test_dead_page_is_read_from_the_archive_and_says_what_to_cite(pages):
    out = page_tools.find_in_page(LIVE, "families first year")
    assert OLD_SENTENCE in out
    assert f"copy from 2023-01-05. Cite it with this url: {COPY}" in out
    assert pages == [LIVE, wayback.raw(COPY)]

def test_archived_page_links_resolve_against_the_original_site(pages):
    out = page_tools.page_links(LIVE)
    assert "The bill: https://www.congress.gov/bill/9" in out and "About" not in out
    assert "web.archive.org" in out                     # the note says which address to cite

def test_snapshot_addresses_are_read_directly(pages):
    out = page_tools.find_in_page(COPY, "families")
    assert OLD_SENTENCE in out and "could not be read" not in out
    assert pages == [wayback.raw(COPY)]

def test_no_archived_copy_reports_the_live_failure(pages, monkeypatch):
    monkeypatch.setattr(wayback, "snapshot", lambda page, when="": None)
    assert "status 404" in page_tools.find_in_page(LIVE, "families")

def test_archive_down_reports_the_live_failure(pages, monkeypatch):
    def down(page, when=""):
        raise wayback.Unavailable("ReadTimeout")
    monkeypatch.setattr(wayback, "snapshot", down)
    assert "status 404" in page_tools.find_in_page(LIVE, "families")

@pytest.mark.parametrize("url", ["http://10.0.0.1/", "https://news.bad.example/a"])
def test_private_and_blocked_pages_never_ask_the_archive(monkeypatch, url):
    monkeypatch.setattr(page_tools, "_pages", {})
    monkeypatch.setattr(validator, "blocked_domains", lambda path=None: {"bad.example"})
    calls = archive(monkeypatch)
    assert page_tools.find_in_page(url, "vote").startswith("CANNOT CITE")
    assert not calls

def test_versions_tool_lists_copies_and_the_one_closest_to_a_date(monkeypatch):
    monkeypatch.setattr(validator, "blocked_domains", lambda path=None: set())
    archive(monkeypatch, available=closest("20210430000000"),
            cdx=[["timestamp", "original", "digest"], ["20190302000000", LIVE, "A"], ["20240101000000", LIVE, "B"]])
    out = page_tools.wayback_versions(LIVE, date="2021-05-01")
    assert f"- 2019-03-02: https://web.archive.org/web/20190302000000/{LIVE}" in out
    assert f"- 2024-01-01: https://web.archive.org/web/20240101000000/{LIVE}" in out
    assert f"Closest to 2021-05-01: 2021-04-30: https://web.archive.org/web/20210430000000/{LIVE}" in out
    assert "find_in_page" in out

def test_versions_tool_accepts_a_snapshot_address(monkeypatch):
    monkeypatch.setattr(validator, "blocked_domains", lambda path=None: set())
    calls = archive(monkeypatch)
    assert "no archived copy of " + LIVE in page_tools.wayback_versions(COPY)
    assert calls[0].url.params["url"] == LIVE

def test_versions_tool_reports_trouble(monkeypatch):
    monkeypatch.setattr(validator, "blocked_domains", lambda path=None: {"bad.example"})
    assert "blocked" in page_tools.wayback_versions("https://bad.example/x")
    monkeypatch.setattr(wayback, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(503)))
    assert page_tools.wayback_versions(LIVE).startswith("ERROR: the Wayback Machine could not be reached")

# --- the published page ---------------------------------------------------------------------------

def test_published_sources_say_when_they_are_archived_copies():
    b = brief()
    b.sources[0].url = COPY
    assert "(archived copy, 2023-01-05)" in render.brief_html(b)
    assert render._archived(LIVE) == ""
