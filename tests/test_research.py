import asyncio, json
import pytest
from ollama import Message
import activity
from agents import researcher as research_mod
from agents import llm
from checks import validator
import settings
from agents.stubs import research as stub_brief
from contracts import ClaimSet, Claim, Mission, RejectedBrief, Source, SourcePick

M = Mission(slug="r", topic="Heat pumps", tools=["search"])
BRIEF_JSON = stub_brief(M).model_dump_json()
PICK_JSON = SourcePick(thesis=stub_brief(M).thesis, sources=stub_brief(M).sources).model_dump_json()
CLAIMS_JSON = ClaimSet(counterpoint=stub_brief(M).counterpoint, claims=stub_brief(M).claims).model_dump_json()

def tool_call(name, **args):
    return Message(role="assistant", content="", tool_calls=[
        Message.ToolCall(function=Message.ToolCall.Function(name=name, arguments=args))])

def text(content):
    return Message(role="assistant", content=content)

BRIEF = [text(PICK_JSON), text(CLAIMS_JSON)]   # the brief is written in two steps: sources, then claims

def by_schema(format):
    """The brief writer's reply for whichever step's schema it asked for."""
    return text(PICK_JSON if format.get("title") == "SourcePick" else CLAIMS_JSON)

class FakeToolbox:
    calls: list = []
    def __init__(self, enabled): self.tools = [{"type": "function", "function": {"name": "search"}}]
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): pass
    async def call(self, name, args):
        FakeToolbox.calls.append((name, args))
        return "result"

@pytest.fixture(autouse=True)
def reset(monkeypatch):
    FakeToolbox.calls = []
    M.brief_pick = M.brief_sources = None  # the brief's first step is saved on the mission; each test starts clean
    monkeypatch.setattr(validator, "source_problem", lambda s, blocked=None, archive=False: (None, s.url))

def script(monkeypatch, replies):
    """Make llm.chat return the given replies in order; record what it was sent."""
    seen = []
    it = iter(replies)
    async def fake_chat(messages, tools=None, format=None, model=None):
        seen.append({"tools": tools, "format": format, "model": model})
        reply = next(it)
        if isinstance(reply, Exception):   # a reply that raises, like llm.Overrun
            raise reply
        return reply
    monkeypatch.setattr(llm, "chat", fake_chat)
    return seen

async def test_tool_loop_then_brief(monkeypatch):
    seen = script(monkeypatch, [tool_call("search", query="heat pumps"), text("DONE"), *BRIEF])
    brief = await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert brief.thesis.startswith("Stub thesis")
    assert FakeToolbox.calls == [("search", {"query": "heat pumps"})]
    assert seen[-1]["format"] is not None         # final call asks for the JSON schema
    assert [c["model"] for c in seen] == [llm.model_for("assistant"), llm.model_for("assistant"), None, None]   # small model searches, main model writes (sources, then claims)

async def test_tool_budget_is_enforced(monkeypatch):
    replies = [tool_call("search", query=str(i)) for i in range(100)]
    script(monkeypatch, replies[:research_mod.MAX_TOOL_CALLS] + BRIEF)
    await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert len(FakeToolbox.calls) == research_mod.MAX_TOOL_CALLS

async def test_bad_json_gets_one_repair(monkeypatch):
    script(monkeypatch, [text("DONE"), text("{not json"), *BRIEF])
    brief = await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert len(brief.sources) == 4

async def test_bad_json_twice_raises(monkeypatch):
    script(monkeypatch, [text("DONE"), text("{nope"), text("{still nope")])
    with pytest.raises(research_mod.ResearchFailed):
        await research_mod.run_research(M, toolbox_factory=FakeToolbox)

async def test_validator_notes_reach_the_prompt(monkeypatch):
    captured = {}
    async def fake_chat(messages, tools=None, format=None, model=None):
        captured.setdefault("system", messages[0]["content"])
        return text("DONE") if format is None else by_schema(format)
    monkeypatch.setattr(llm, "chat", fake_chat)
    m = M.model_copy(update={"validator_notes": "s1: quote not found"})
    await research_mod.run_research(m, toolbox_factory=FakeToolbox)
    assert "s1: quote not found" in captured["system"]

async def test_brief_writer_is_shown_once_the_main_model_starts(monkeypatch):
    seen = []
    async def fake_chat(messages, tools=None, format=None, model=None):
        if format is None:
            return text("DONE")
        seen.append(activity.current()["agent"])
        return by_schema(format)
    monkeypatch.setattr(llm, "chat", fake_chat)
    await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert seen and set(seen) == {"brief"}     # both of the brief's steps show as writing the brief

async def test_retry_revises_the_rejected_brief(monkeypatch):
    sent = []
    async def fake_chat(messages, tools=None, format=None, model=None):
        sent.append([dict(x) if isinstance(x, dict) else x for x in messages])
        return text("DONE") if format is None else by_schema(format)
    monkeypatch.setattr(llm, "chat", fake_chat)
    notes = "s2: could not fetch https://b.example.com\nOnly 2 verified supporting domain(s); need 3"
    m = M.model_copy(update={"validator_notes": "research crashed: RuntimeError: boom",
                             "rejected_brief": RejectedBrief(brief=stub_brief(M), notes=notes)})
    await research_mod.run_research(m, toolbox_factory=FakeToolbox)
    system = sent[0][0]["content"]
    assert "Stub thesis" in system and "https://a.example.com" in system   # the brief itself is passed on
    assert "Sources that passed verification: s1, s3, s4." in system
    assert "s2: could not fetch" in system                                 # its own notes, not the crash
    assert "research crashed" not in system
    assert research_mod.REVISE_CLAIMS in sent[-1][-1]["content"]             # step 2 revises the failed claims

async def test_first_attempt_has_no_revision_prompt(monkeypatch):
    sent = []
    async def fake_chat(messages, tools=None, format=None, model=None):
        sent.append(list(messages))
        return text("DONE") if format is None else by_schema(format)
    monkeypatch.setattr(llm, "chat", fake_chat)
    await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert "passed verification" not in sent[0][0]["content"]
    assert sent[-2][-1]["content"] == research_mod.PICK
    assert research_mod.REVISE_CLAIMS not in sent[-1][-1]["content"]

async def test_prompt_separates_exploring_possibilities_and_verifying(monkeypatch):
    captured = {}
    async def fake_chat(messages, tools=None, format=None, model=None):
        captured.setdefault("system", messages[0]["content"])
        return text("DONE") if format is None else by_schema(format)
    monkeypatch.setattr(llm, "chat", fake_chat)
    await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    stages = ["Explore widely", "Rule out", "Report what is possible", "Pin down the record"]
    positions = [captured["system"].index(s) for s in stages]
    assert positions == sorted(positions)
    assert 'kind "possibility"' in captured["system"] and 'kind "fact"' in captured["system"]

async def test_prompt_is_skeptical_but_keeps_its_limits(monkeypatch):
    captured = {}
    async def fake_chat(messages, tools=None, format=None, model=None):
        captured.setdefault("system", messages[0]["content"])
        return text("DONE") if format is None else by_schema(format)
    monkeypatch.setattr(llm, "chat", fake_chat)
    await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert "Assume every source is spinning" in captured["system"]
    assert "Doubt alone proves nothing" in captured["system"]          # suspicion is not evidence
    assert "a named person committed a crime" in captured["system"]    # provocation has a limit

async def test_research_never_looks_for_contacts(monkeypatch):
    seen = script(monkeypatch, [text("DONE"), *BRIEF])
    await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert len(seen) == 3                          # one sources run, the brief's two steps; nothing else
    assert "Outreach agent does that" in research_mod.SYSTEM   # people to ask are found after the brief is approved

def test_source_finder_limit_is_15_minutes():
    assert research_mod.SOURCE_TIME_LIMIT == 15 * 60

async def test_source_finder_stops_at_its_time_limit_and_keeps_what_it_found(monkeypatch):
    monkeypatch.setattr(research_mod, "SOURCE_TIME_LIMIT", 0.05)
    sent = []
    async def fake_chat(messages, tools=None, format=None, model=None):
        if format is not None:
            sent.append(list(messages))
            return by_schema(format)
        if FakeToolbox.calls:
            await asyncio.sleep(30)            # the small model hangs on its second turn
        return tool_call("search", query="heat pumps")
    monkeypatch.setattr(llm, "chat", fake_chat)
    brief = await asyncio.wait_for(research_mod.run_research(M, toolbox_factory=FakeToolbox), 5)
    assert brief.thesis.startswith("Stub thesis")
    assert any(isinstance(x, dict) and x.get("role") == "tool" for x in sent[0])   # the search result survived

async def test_time_limit_does_not_cover_the_brief_writer(monkeypatch):
    monkeypatch.setattr(research_mod, "SOURCE_TIME_LIMIT", 0.05)
    async def fake_chat(messages, tools=None, format=None, model=None):
        if format is None:
            return text("DONE")
        await asyncio.sleep(0.1)               # longer than the source finder's limit
        return by_schema(format)
    monkeypatch.setattr(llm, "chat", fake_chat)
    assert await research_mod.run_research(M, toolbox_factory=FakeToolbox)

async def test_a_timeout_from_inside_a_call_is_still_a_failure(monkeypatch):
    async def fake_chat(messages, tools=None, format=None, model=None):
        raise TimeoutError("connection timed out")
    monkeypatch.setattr(llm, "chat", fake_chat)
    with pytest.raises(TimeoutError):
        await research_mod.run_research(M, toolbox_factory=FakeToolbox)

async def test_a_stuck_brief_writer_keeps_what_it_wrote(monkeypatch):
    script(monkeypatch, [text("DONE"), llm.Overrun("reply ran past 8192 tokens", PICK_JSON[:-1] + "\n" * 500),
                         text(CLAIMS_JSON)])
    brief = await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert brief.thesis.startswith("Stub thesis")                   # the validator checks it as usual

async def test_a_stuck_brief_writer_with_nothing_usable_fails_the_attempt(monkeypatch):
    script(monkeypatch, [text("DONE"), llm.Overrun("reply took over 10 minutes", "\n" * 500)])
    with pytest.raises(research_mod.ResearchFailed, match="got stuck"):
        await research_mod.run_research(M, toolbox_factory=FakeToolbox)

async def test_a_stuck_search_turn_ends_the_search_not_the_research(monkeypatch):
    script(monkeypatch, [tool_call("search", query="x"), llm.Overrun("tool turn ran past 2048 tokens"), *BRIEF])
    brief = await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert brief.thesis.startswith("Stub thesis") and FakeToolbox.calls == [("search", {"query": "x"})]

def test_brief_writer_sees_failed_and_repeated_tool_results_shortened():
    from agents.researcher import _for_brief
    out = _for_brief([{"role": "assistant", "content": "", "thinking": "long"},
                      {"role": "tool", "content": "ERROR: fetch failed\n" + "trace " * 500},
                      {"role": "tool", "content": "page"}, {"role": "tool", "content": "page"}])
    assert "thinking" not in out[0] and out[1]["content"] == "ERROR: fetch failed"
    assert out[2]["content"] == "page" and out[3]["content"] == "(same result as an earlier call)"

async def test_a_call_repeating_the_previous_one_is_not_run(monkeypatch):
    script(monkeypatch, [tool_call("search", query="x"), tool_call("search", query="x"),
                                tool_call("search", query="y"), text("DONE"), *BRIEF])
    await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert FakeToolbox.calls == [("search", {"query": "x"}), ("search", {"query": "y"})]

async def test_a_search_stuck_repeating_one_call_ends_and_the_brief_is_still_written(monkeypatch):
    stuck = [tool_call("search", query="x") for _ in range(research_mod.MAX_REFUSED + 1)]
    seen = script(monkeypatch, stuck + BRIEF)   # no DONE: the loop ends it, not the model
    brief = await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert brief.thesis.startswith("Stub thesis") and FakeToolbox.calls == [("search", {"query": "x"})]
    assert len(seen) == research_mod.MAX_REFUSED + 1 + len(BRIEF)

async def test_a_claim_writer_stuck_repeating_claims_keeps_the_checked_sources(monkeypatch):
    claim = json.dumps(json.loads(CLAIMS_JSON)["claims"][0])
    stuck = '{"counterpoint":"c","claims":[' + ",".join([claim] * 50)
    script(monkeypatch, [text("DONE"), *BRIEF[:1], llm.Overrun("reply ran past 8192 tokens", stuck)])
    brief = await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert len(brief.sources) == 4 and brief.counterpoint == "c" and len(brief.claims) == 1

def checker(failing: dict[str, str]):
    """A source check where the given source ids fail with the given problem, and the rest pass."""
    def check(s, blocked=None, archive=False):
        return (f"{s.id}: {failing[s.id]}" if s.id in failing else None), s.url
    return check

def recorder(monkeypatch, replies):
    """Like script, but keeps the messages each call was sent."""
    sent, it = [], iter(replies)
    async def fake_chat(messages, tools=None, format=None, model=None):
        sent.append(list(messages))
        return next(it)
    monkeypatch.setattr(llm, "chat", fake_chat)
    return sent

async def test_a_failed_source_goes_back_with_what_the_check_found_and_its_fix_is_kept(monkeypatch):
    fixed = Source(id="s2", url="https://b.example.com", title="B", quote="Real words.", stance="supports")
    sent = recorder(monkeypatch, [text("DONE"), text(PICK_JSON), text(json.dumps({"sources": [fixed.model_dump()]})),
                                  text(CLAIMS_JSON)])
    def check(s, blocked=None, archive=False):  # s2 passes once its quote is the page's real sentence
        bad = s.id == "s2" and s.quote != "Real words."
        return ('s2: quote not found. Closest sentence actually on the page: "Real words."' if bad else None), s.url
    monkeypatch.setattr(validator, "source_problem", check)
    brief = await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    feedback = sent[2][-1]["content"]
    assert "Passed, and kept exactly as they are: s1, s3, s4" in feedback and '"Real words."' in feedback
    assert next(s for s in brief.sources if s.id == "s2").quote == "Real words."
    assert "Verified sources:" in sent[3][-1]["content"] and 'quote: "Real words."' in sent[3][-1]["content"]

async def test_a_source_that_keeps_failing_is_dropped_before_claims_are_written(monkeypatch):
    monkeypatch.setattr(validator, "source_problem", checker({"s4": "could not fetch"}))
    empty = text(json.dumps({"sources": []}))
    sent = recorder(monkeypatch, [text("DONE"), text(PICK_JSON), empty, text(CLAIMS_JSON)])
    brief = await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert [s.id for s in brief.sources] == ["s1", "s2", "s3"]
    assert "s4" not in sent[-1][-1]["content"].split("Verified sources:")[1]

async def test_too_few_sources_holding_up_fails_the_attempt_with_its_draft(monkeypatch):
    monkeypatch.setattr(validator, "source_problem", checker({"s1": "could not fetch", "s2": "quote not found"}))
    empty = text(json.dumps({"sources": []}))
    script(monkeypatch, [text("DONE"), text(PICK_JSON), empty, empty])
    with pytest.raises(research_mod.BriefFailed) as failed:
        await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert "Only 1 verified supporting domain(s); need 3" in failed.value.notes
    assert failed.value.brief.thesis.startswith("Stub thesis") and failed.value.brief.claims == []

async def test_claims_are_fixed_by_code_where_safe_and_fed_back_where_not(monkeypatch):
    bad = ClaimSet(counterpoint="c", claims=[
        Claim(text="It is possible that X, based on s1.", source_ids=["s1"], kind="fact"),   # code relabels it
        Claim(text="Y happened.", source_ids=["s9"]),                                        # fed back
    ]).model_dump_json()
    sent = recorder(monkeypatch, [text("DONE"), text(PICK_JSON), text(bad), text(bad), text(bad)])
    brief = await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert "Claim 2 cites unknown source id 's9'" in sent[3][-1]["content"]
    assert "set kind to 'possibility'" not in sent[3][-1]["content"]             # already fixed by code
    assert [(c.text[:5], c.kind) for c in brief.claims] == [("It is", "possibility")]   # the last round drops claim 2

async def test_a_stop_while_writing_claims_keeps_the_checked_sources_for_next_time(monkeypatch):
    import stopping
    m = M.model_copy()
    script(monkeypatch, [text("DONE"), text(PICK_JSON), llm.Cut("")])
    with pytest.raises(stopping.Interrupted) as stop:
        await research_mod.run_research(m, toolbox_factory=FakeToolbox)
    assert stop.value.draft is None and [s.id for s in m.brief_sources.sources] == ["s1", "s2", "s3", "s4"]
    sent = recorder(monkeypatch, [text(CLAIMS_JSON)])                        # picks up at step 2: no new source pick
    from contracts import Evidence
    brief = await research_mod.run_write_brief(m, Evidence(messages=[{"role": "system", "content": "s"}]))
    assert len(sent) == 1 and "Verified sources:" in sent[0][-1]["content"] and len(brief.sources) == 4

class SiteToolbox:
    """search, fetch and wayback_versions; fetching anything on refusing.example.com fails like a 403."""
    calls: list = []
    def __init__(self, enabled):
        self.tools = [{"type": "function", "function": {"name": n}} for n in ("search", "fetch", "wayback_versions")]
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): pass
    async def call(self, name, args):
        SiteToolbox.calls.append((name, args))
        if "refusing.example.com" in str(args.get("url", "")):
            return "ERROR: Error executing tool fetch: Failed to fetch https://refusing.example.com/a - status code 403"
        return "page text"

LEADS = "Leads: https://refusing.example.com/a https://x.example.com"   # addresses the run has been shown

async def run_tools(monkeypatch, turns, budget=research_mod.MAX_TOOL_CALLS):
    SiteToolbox.calls = []
    script(monkeypatch, turns + [text("DONE")])
    async with SiteToolbox([]) as tb:
        messages = await research_mod.tool_loop(LEADS, tb, budget)
    return [m["content"] for m in messages if isinstance(m, dict) and m.get("role") == "tool"]

async def test_a_site_that_refuses_reading_is_explained_once_and_not_fetched_again(monkeypatch):
    results = await run_tools(monkeypatch, [tool_call("fetch", url="https://refusing.example.com/a"),
                                            tool_call("fetch", url="https://refusing.example.com/b")])
    assert "refuses automated reading" in results[0] and "wayback_versions" in results[0]
    assert results[1].startswith("Not run: refusing.example.com refused automated reading")
    assert len(SiteToolbox.calls) == 1

async def test_a_blocked_site_is_refused_before_it_is_fetched(monkeypatch, tmp_path):
    monkeypatch.setattr(validator, "blocked_domains", lambda path=None: {"spam.example.com"})
    results = await run_tools(monkeypatch, [tool_call("fetch", url="https://www.spam.example.com/x")])
    assert results[0].startswith("Not run: spam.example.com is on the blocked list") and SiteToolbox.calls == []

async def test_the_same_search_run_again_later_is_refused(monkeypatch):
    results = await run_tools(monkeypatch, [tool_call("search", query="a"), tool_call("fetch", url="https://x.example.com"),
                                            tool_call("search", query="a")])
    assert results[2].startswith("Not run: you already ran this exact search") and len(SiteToolbox.calls) == 2

async def test_refusals_end_the_run_only_when_they_come_in_a_row(monkeypatch):
    turns = [tool_call("search", query="a"), tool_call("search", query="a"), tool_call("search", query="b"),
             tool_call("search", query="b"), tool_call("search", query="b")]
    results = await run_tools(monkeypatch, turns)
    assert len(SiteToolbox.calls) == 2 and len(results) == 5             # a real call in between resets the count

async def test_a_low_budget_is_shown_on_results_but_not_to_the_brief_writer(monkeypatch):
    results = await run_tools(monkeypatch, [tool_call("search", query=str(i)) for i in range(3)], budget=12)
    assert "[11 tool calls left" not in results[0] and "[10 tool calls left" in results[1]
    out = research_mod._for_brief([{"role": "tool", "content": results[1]}])
    assert out[0]["content"] == "page text"

async def test_an_address_the_run_never_showed_is_refused_before_it_is_fetched(monkeypatch):
    results = await run_tools(monkeypatch, [tool_call("fetch", url="https://made-up.example.org/report.pdf")])
    assert results[0].startswith("Not run: this address wasn't in any search result or page you read")
    assert SiteToolbox.calls == []

async def test_the_same_page_written_another_way_is_fetched(monkeypatch):
    urls = ["http://www.x.example.com/", "https://x.example.com#top",
            "https://web.archive.org/web/2020/https://refusing.example.com/a"]
    results = await run_tools(monkeypatch, [tool_call("fetch", url=u) for u in urls])
    assert not any(r.startswith("Not run: this address") for r in results) and len(SiteToolbox.calls) == 3

def test_addresses_in_a_result_and_a_pages_own_links_can_be_fetched():
    g = research_mod.Guard(["fetch", "search"])
    assert not g.has_seen("https://news.example.com/2026/story")
    g.explain("search", {"query": "a"}, "Title: Story\nURL: https://news.example.com/2026/story?id=4&amp;p=2")
    assert g.has_seen("https://news.example.com/2026/story?id=4&p=2")
    g.explain("fetch", {"url": "https://news.example.com/2026/story"}, "See [the filing](/filings/2026-q1.pdf).")
    assert g.has_seen("https://news.example.com/filings/2026-q1.pdf")    # a relative link on a site it read
    assert not g.has_seen("https://news.example.com/filings/2025-q4.pdf")

def test_failed_fetches_are_read_by_their_status_not_by_digits_in_the_address():
    g = research_mod.Guard(["fetch", "wayback_versions"])
    gone = g.explain("fetch", {"url": "https://news.example.com/2026/403-days"},
                     "ERROR: Failed to fetch https://news.example.com/2026/403-days - status code 404")
    assert "no page at this address" in gone and not g.refused
    page = g.explain("fetch", {"url": "https://a.example.com/x"}, "ERROR: The sites robots.txt (https://a.example.com/robots.txt), "
                     "specifies that autonomous fetching of this page is not allowed")
    assert "doesn't allow this page" in page and not g.refused        # one page ruled out, not the whole site
    g.explain("fetch", {"url": "https://www.sec.gov/x"}, "ERROR: When fetching robots.txt (https://www.sec.gov/robots.txt), "
              "received status 403 so assuming that autonomous fetching is not allowed")
    assert "sec.gov" in g.refused

async def test_a_pick_that_uses_urls_as_ids_gets_short_ids_before_claims_are_written(monkeypatch):
    raw = json.dumps({"thesis": "t", "sources": [dict(s.model_dump(), id=s.url) for s in stub_brief(M).sources]})
    sent = recorder(monkeypatch, [text("DONE"), text(raw), text(CLAIMS_JSON)])
    brief = await research_mod.run_research(M, toolbox_factory=FakeToolbox)
    assert [s.id for s in brief.sources] == ["s1", "s2", "s3", "s4"]
    assert "- s1 (supports)" in sent[-1][-1]["content"] and "https://a.example.com (supports)" not in sent[-1][-1]["content"]
