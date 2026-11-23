import json
from contextlib import contextmanager
from pathlib import Path
import httpx
import pytest
import page_tools, wayback
from agents.researcher import _tool_servers
from agents.toolbox import Toolbox
from checks import validator
from contracts import Mission

ROOT = Path(__file__).parent.parent
PUBLIC = "https://93.184.216.34"           # a literal address, so no DNS lookup is needed
PAGE = f"{PUBLIC}/story"
SENTENCE = "The Senate passed the tariff bill by a vote of 51 to 49 on Tuesday."
HTML = """<html><body><nav><a href="https://menu.example.org/">Menu link</a></nav><article>
<a href="https://www.congress.gov/bill/1#text">Full bill text</a>
<a href="https://www.congress.gov/bill/1">Same bill again</a>
<a href="https://sub.93.184.216.34/x">Own site</a>
<a href="/local">Local page</a>
<a href="https://twitter.com/share">Share this</a>
<a href="https://bad.example/x">Blocked site</a>
<a href="mailto:a@b.org">Email us</a>
<a href="https://critics.org/rebuttal"><img src="x.png"></a>
<a href="https://critics.org/reply">A rebuttal from critics</a>
</article></body></html>"""

@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(page_tools, "_pages", {})
    monkeypatch.setattr(validator, "blocked_domains", lambda path=None: {"bad.example"})
    monkeypatch.setattr(wayback, "snapshot", lambda page, when="": None)   # no archived copies unless a test adds one

@pytest.fixture
def web(monkeypatch):
    """Serve one fixed page; return the list of requested URLs."""
    calls = []
    monkeypatch.setattr(page_tools, "_download", lambda url: calls.append(url) or HTML)
    monkeypatch.setattr(validator, "readable_text",
                        lambda html: f"Opening words that say very little here.\n{SENTENCE} Short one. "
                                     "Farmers worried about the price of soybeans all season.")
    return calls

def responses(monkeypatch, *replies):
    """Make httpx.stream play back responses in order; return the requested URLs."""
    calls, it = [], iter(replies)
    @contextmanager
    def fake_stream(method, url, **kwargs):
        calls.append(url)
        assert kwargs["follow_redirects"] is False
        yield next(it)
    monkeypatch.setattr(page_tools.httpx, "stream", fake_stream)
    return calls

def test_find_returns_exact_sentences_from_the_whole_page(web):
    out = page_tools.find_in_page(PAGE, "senate tariffs vote")
    assert SENTENCE in out and "soybeans" not in out and "Short one" not in out

def test_find_ranks_best_match_first(web, monkeypatch):
    monkeypatch.setattr(validator, "readable_text", lambda html: "Alpha beta words sit in the first line.\n"
                                                                 "Alpha beta gamma words sit in the second line.")
    out = page_tools.find_in_page(PAGE, "alpha beta gamma")
    assert out.index("second line") < out.index("first line")

def test_find_says_when_nothing_matches(web):
    assert "no sentence matches" in page_tools.find_in_page(PAGE, "submarine")

def test_find_needs_a_real_query(web):
    assert page_tools.find_in_page(PAGE, "the of").startswith("ERROR")

def test_page_is_downloaded_once(web):
    page_tools.find_in_page(PAGE, "vote")
    page_tools.page_links(PAGE)
    assert web == [PAGE]

def test_blocked_domain_cannot_be_cited(web):
    out = page_tools.find_in_page("https://news.bad.example/a", "vote")
    assert out.startswith("CANNOT CITE") and "blocked" in out and not web

def test_page_without_text_cannot_be_cited(web, monkeypatch):
    monkeypatch.setattr(validator, "readable_text", lambda html: "")
    assert page_tools.find_in_page(PAGE, "vote").startswith("CANNOT CITE")

def test_links_keep_only_useful_outside_pages(web):
    out = page_tools.page_links(PAGE)
    assert "- Full bill text: https://www.congress.gov/bill/1\n" in out
    assert "- A rebuttal from critics: https://critics.org/reply" in out
    assert out.count("congress.gov") == 1
    for unwanted in ("Menu link", "Local page", "Share this", "Blocked site", "Email us", "Own site", "/rebuttal"):
        assert unwanted not in out

def test_links_can_be_narrowed(web):
    out = page_tools.page_links(PAGE, about="rebuttal")
    assert "critics.org/reply" in out and "congress.gov" not in out
    assert "No matching links" in page_tools.page_links(PAGE, about="submarine")

@pytest.mark.parametrize("url", ["http://127.0.0.1:8770/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
                                 "http://10.0.0.1/", "http://169.254.169.254/latest/", "file:///etc/passwd",
                                 "http://224.0.0.1/", "http://93.184.216.34:notaport/"])
def test_private_addresses_are_never_requested(monkeypatch, url):
    calls = responses(monkeypatch)
    assert page_tools.find_in_page(url, "vote").startswith("CANNOT CITE")
    assert not calls

def test_redirect_to_private_address_is_refused(monkeypatch):
    calls = responses(monkeypatch, httpx.Response(302, headers={"location": "http://127.0.0.1:8770/"}))
    assert "public web address" in page_tools.find_in_page(PAGE, "vote")
    assert calls == [PAGE]

def test_public_redirect_is_followed(monkeypatch):
    calls = responses(monkeypatch, httpx.Response(301, headers={"location": "/moved"}),
                      httpx.Response(200, text="<p>hello</p>"))
    assert page_tools._download(PAGE) == "<p>hello</p>"
    assert calls == [PAGE, f"{PUBLIC}/moved"]

def test_error_status_and_huge_pages_are_reported(monkeypatch):
    responses(monkeypatch, httpx.Response(403))
    assert "status 403" in page_tools.find_in_page(PAGE, "vote")
    responses(monkeypatch, httpx.Response(200, content=b"x" * (page_tools.MAX_BYTES + 1)))
    assert "too large" in page_tools.find_in_page(PAGE, "vote")

def test_network_failure_is_reported(monkeypatch):
    @contextmanager
    def broken(method, url, **kwargs):
        raise httpx.ConnectError("down")
        yield
    monkeypatch.setattr(page_tools.httpx, "stream", broken)
    assert "ConnectError" in page_tools.find_in_page(PAGE, "vote")

def test_missions_that_fetch_get_the_page_tools():
    assert _tool_servers(Mission(slug="a", topic="t")) == ["search", "fetch", "pages"]
    assert _tool_servers(Mission(slug="a", topic="t", tools=["search"])) == ["search"]
    assert _tool_servers(Mission(slug="a", topic="t", tools=["fetch", "pages"])) == ["fetch", "pages"]

async def test_search_server_runs_through_the_toolbox(monkeypatch):
    monkeypatch.chdir(ROOT)
    async with Toolbox(["search"]) as tb:
        assert sorted(t["function"]["name"] for t in tb.tools) == ["news_search", "web_search", "wiki_search"]
        assert not tb.skipped              # no key and no npx needed
        assert await tb.call("web_search", {"query": "  "}) == "Give a query: the words to search for."

async def test_pages_server_runs_through_the_toolbox(monkeypatch):
    monkeypatch.chdir(ROOT)                # the app runs from the project folder
    async with Toolbox(["pages"]) as tb:
        assert sorted(t["function"]["name"] for t in tb.tools) == ["find_in_page", "page_links", "wayback_versions"]
        out = await tb.call("find_in_page", {"url": "http://127.0.0.1:1/", "query": "vote"})
        assert out.startswith("CANNOT CITE")
