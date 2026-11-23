"""Dead leads and claim tags (#132): a lead research shows is refuted, unanswerable or untestable stops; the Theorist or
Researcher Chat may propose one for the human (or auto mode) to approve; a human may mark or revive one. Every brief
and article carries a claim tag (confirmed or unconfirmed) that only our UI and the Theorist see."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from ollama import Message
from starlette.testclient import TestClient
import agents, claim_tags, orchestrator, settings, status_tools, store, ui
from agents import llm, stubs, theorist
from agents import researcher as research_mod
from agents.theorist import NewClaimTag, NewDeadLead, Theory
from checks import validator
from contracts import (Article, ArticleDraft, ArticlePlan, ArticleRating, ClaimSet, Evidence, Mission, PlannedSection,
                       ResearchBrief, Section, Source, SourcePick, State)

pytestmark = pytest.mark.usefixtures("stub_agents")
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)

def run_at(t): orchestrator.run_until_idle(lambda: t)

def text(content): return Message(role="assistant", content=content)

def src(i, stance="supports", site=None):
    return Source(id=f"s{i}", url=f"https://{site or chr(96 + i)}.example.com/p", title=f"T{i}", quote="Words.", stance=stance)

def replies(monkeypatch, *items):
    it = iter(items)
    async def fake_chat(messages, tools=None, format=None, model=None):
        return text(next(it))
    monkeypatch.setattr(llm, "chat", fake_chat)

# ---------- research can end a lead ----------

async def test_a_refuted_pick_with_a_verified_contradicting_record_is_a_dead_lead(monkeypatch):
    monkeypatch.setattr(validator, "source_problem", lambda s, blocked=None, archive=False: (None, s.url))
    pick = SourcePick(thesis="It is possible that the mayor was arrested in 2019", sources=[src(1, "contradicts")],
                      dead_lead="refuted", dead_lead_why="The county booking log shows no arrest.")
    replies(monkeypatch, pick.model_dump_json())
    m = Mission(slug="mayor", topic="Was the mayor arrested in 2019?")
    with pytest.raises(research_mod.DeadLeadFound) as dead:
        await research_mod.run_write_brief(m, Evidence(messages=[{"role": "system", "content": "s"}]))
    assert dead.value.verdict == "refuted" and "booking log" in dead.value.why
    assert dead.value.sources == ['https://a.example.com/p: "Words."']

async def test_a_refuted_pick_whose_record_fails_its_check_is_an_ordinary_failed_attempt(monkeypatch):
    monkeypatch.setattr(validator, "source_problem", lambda s, blocked=None, archive=False: ("quote not found", s.url))
    pick = SourcePick(thesis="x", sources=[src(1, "contradicts")], dead_lead="refuted", dead_lead_why="It's wrong.")
    replies(monkeypatch, pick.model_dump_json(), json.dumps({"sources": []}), json.dumps({"sources": []}))
    with pytest.raises(research_mod.BriefFailed):
        await research_mod.run_write_brief(Mission(slug="x", topic="X"), Evidence(messages=[{"role": "system", "content": "s"}]))

async def test_no_record_with_three_good_sites_is_ignored_and_the_brief_carries_on(monkeypatch):
    monkeypatch.setattr(validator, "source_problem", lambda s, blocked=None, archive=False: (None, s.url))
    pick = SourcePick(thesis="The board met in secret in 2021", sources=[src(1), src(2), src(3)], dead_lead="no_record",
                      dead_lead_why="Nothing found.")
    claims = ClaimSet(counterpoint="The board denies it [s1].", claims=[{"text": "The minutes show it.", "source_ids": ["s1"]}])
    replies(monkeypatch, pick.model_dump_json(), claims.model_dump_json())
    brief = await research_mod.run_write_brief(Mission(slug="b", topic="Board"), Evidence(messages=[{"role": "system", "content": "s"}]))
    assert brief.thesis == "The board met in secret in 2021"

@pytest.mark.parametrize("down, dead", [("", True), ("Brave Search is out of prepaid credit", False)])
async def test_no_record_counts_only_when_search_worked_and_the_sources_are_thin(monkeypatch, down, dead):
    monkeypatch.setattr(validator, "source_problem", lambda s, blocked=None, archive=False: (None, s.url))
    pick = SourcePick(thesis="x", sources=[src(1)], dead_lead="no_record", dead_lead_why="No filing exists.")
    replies(monkeypatch, pick.model_dump_json())
    evidence = Evidence(messages=[{"role": "system", "content": "s"}], search_down=down)
    with pytest.raises(research_mod.DeadLeadFound if dead else research_mod.BriefFailed):
        await research_mod.run_write_brief(Mission(slug="x", topic="X"), evidence)

def test_the_research_prompts_ask_for_records_and_allow_a_dead_lead():
    assert "court heard it?" in research_mod.SYSTEM and "A dead end is a finding." in research_mod.SYSTEM
    assert '"refuted"' in research_mod.PICK and '"untestable"' in research_mod.PICK

def test_the_loop_stops_a_dead_lead_after_one_attempt(monkeypatch):
    def dead(m, evidence, now=None):
        raise research_mod.DeadLeadFound("refuted", "The court dismissed the claim.", ["https://court.example/r: \"Dismissed.\""])
    monkeypatch.setattr(agents, "write_brief", dead)
    store.save_mission(Mission(slug="suit", topic="Did the suit succeed?"))
    run_at(T0)
    m = store.get_mission("suit")
    assert m.state == State.DEAD_LEAD and m.attempts == 1
    d = store.get_dead_lead("suit")
    assert (d.verdict, d.by, d.status) == ("refuted", "research", "dead") and d.sources
    assert "dead lead (refuted): The court dismissed the claim." in store.events("suit")[-1]["note"]
    run_at(T0 + timedelta(hours=1))
    assert store.get_mission("suit").state == State.DEAD_LEAD   # out of the flow: nothing picks it up

def test_requeue_revives_a_dead_lead_and_it_is_never_proposed_again():
    store.save_mission(Mission(slug="gone", topic="A lead", state=State.DEAD_LEAD, attempts=1))
    store.save_dead_lead(store.DeadLead(slug="gone", verdict="no_record", why="none", at=T0.isoformat()))
    orchestrator.requeue(store.get_mission("gone"), T0)
    assert store.get_mission("gone").state == State.QUEUED and store.get_dead_lead("gone").status == "revived"
    with pytest.raises(ValueError, match="kept it going"):
        orchestrator.propose_dead_lead("gone", "untestable", "still a framework", T0)

# ---------- the Theorist (and chat) propose, the human or auto mode decides ----------

def desk():
    store.save_mission(Mission(slug="frame", topic="Does the coordination framework explain exclusion?"))
    store.save_mission(Mission(slug="writing", topic="Why did the grid fail?", state=State.WRITING))
    store.save_mission(Mission(slug="brief", topic="Who funded the measure?", state=State.BRIEF_READY))
    store.save_artifact("brief", "brief", stubs.research(store.get_mission("brief")))

def test_dead_lead_rules():
    desk()
    dropped = []
    kept = theorist.settle_dead_leads([
        NewDeadLead(mission="frame", verdict="untestable", why="It rests on a label the desk made up."),
        NewDeadLead(mission="writing", verdict="refuted", why="x"),
        NewDeadLead(mission="brief", verdict="maybe", why="x"),
        NewDeadLead(mission="nope", verdict="refuted", why="x"),
    ], store.all_missions(), None, dropped)
    assert [d.mission for d in kept] == ["frame"]
    assert any("not yet being written" in d for d in dropped) and any("verdict is one of" in d for d in dropped)

def test_auto_mode_approves_a_proposed_dead_lead_and_the_human_can_reject_one(monkeypatch):
    desk()
    theory = Theory(patterns=["p"], inspiration="", dead_leads=[
        NewDeadLead(mission="frame", verdict="untestable", why="It rests on a label the desk made up.")])
    orchestrator.apply_theory(theory, T0)
    assert store.get_dead_lead("frame").status == "proposed" and store.get_mission("frame").state == State.QUEUED
    assert orchestrator.auto_approve(T0) == 0           # auto mode off: it waits
    monkeypatch.setattr(settings, "auto_mode", lambda: True)
    assert store.get_mission("frame").state == State.QUEUED
    orchestrator.auto_approve(T0, busy={"frame"})       # a worker is busy with it: later
    assert store.get_mission("frame").state == State.QUEUED
    orchestrator.auto_approve(T0)
    assert store.get_mission("frame").state == State.DEAD_LEAD
    d = store.get_dead_lead("frame")
    assert (d.status, d.by, d.decided_by) == ("dead", "theorist", "auto mode")

def test_a_rejected_dead_lead_is_kept_going_and_not_proposed_again():
    desk()
    orchestrator.propose_dead_lead("brief", "no_record", "Nothing on file.", T0, by="chat")
    orchestrator.reject_dead_lead("brief", T0)
    assert store.get_mission("brief").state == State.BRIEF_READY
    dropped = []
    assert not theorist.settle_dead_leads([NewDeadLead(mission="brief", verdict="no_record", why="again")],
                                          store.all_missions(), None, dropped)
    assert "the editor kept it going" in dropped[0]

def test_the_theorist_is_told_to_test_the_world_and_reads_tags_and_dead_leads():
    desk()
    store.save_mission(Mission(slug="dead", topic="An old framework", state=State.DEAD_LEAD))
    store.save_dead_lead(store.DeadLead(slug="dead", verdict="untestable", why="a label, not events"))
    claim_tags.tag_brief(store.get_mission("brief"), stubs.research(store.get_mission("brief")), T0)
    text_ = theorist.prompt("Who funds ballot measures?", [], store.all_missions())
    assert "Test the desk against the world" in text_ and "a document can answer" in text_
    assert "dead lead (untestable: a label, not events)" in text_
    assert "brief | brief in review | claim: confirmed |" in text_
    assert "dead_leads:" in text_ and "claim_tags:" in text_

def test_the_finder_hunts_records():
    from agents import finder
    assert any(a.startswith("Read the fringe") for a in finder.ANGLES)
    assert all(any(w in a.lower() for w in ("record", "court", "testimony", "filing", "data", "trail"))
               for a in finder.ANGLES)
    assert "the open question" in finder.PROPOSE and "fringe source is fine" in finder.PROPOSE and "Explore." in finder.SEARCH

# ---------- claim tags ----------

def test_code_tags_a_brief_from_its_thesis():
    m = Mission(slug="t", topic="T", state=State.BRIEF_READY)
    store.save_mission(m)
    brief = stubs.research(m).model_copy(update={"thesis": "It is possible that the fund paid twice"})
    store.save_artifact("t", "brief", brief)
    assert claim_tags.tag_brief(m, brief, T0).value == "unconfirmed"

def test_a_passed_brief_gets_code_tag_in_the_loop():
    store.save_mission(Mission(slug="loop", topic="Who paid?"))
    run_at(T0)
    t = store.get_claim_tag("loop")
    assert (t.value, t.by) == ("confirmed", "code")   # the stub thesis states it as fact

def article(claim):
    return Article(title="T", dek="D", claim=claim, sections=[Section(heading="A", paragraphs=["x [s1]."]),
                                                              Section(heading="B", paragraphs=["y [s1]."])])

def plan(status, why="w"):
    return ArticlePlan(title="T", dek="D", sections=[PlannedSection(heading="A", point="p"),
                                                     PlannedSection(heading="B", point="p")],
                       claim_status=status, claim_status_why=why)

def tagged_mission():
    m = Mission(slug="w", topic="W", state=State.WRITING)
    store.save_mission(m)
    store.save_artifact("w", "brief", stubs.research(m))
    return m

def test_the_writer_cannot_call_a_possibility_confirmed_and_the_rater_can_change_it_without_the_score():
    m = tagged_mission()
    m.article_draft = ArticleDraft(plan=plan("confirmed", "The claim matches the thesis."))
    orchestrator._writer_tag(m, article("It is possible that **the fund paid twice** [s1]."), T0)
    t = store.get_claim_tag("w")
    assert (t.value, t.by) == ("unconfirmed", "writer") and "code kept it unconfirmed" in t.why
    m.article_draft = ArticleDraft(plan=plan("confirmed"))
    good = article("**The fund paid twice** [s1].")
    orchestrator._writer_tag(m, good, T0)
    assert (store.get_claim_tag("w").value, store.get_claim_tag("w").by) == ("confirmed", "writer")
    rating = ArticleRating(logic=8, intrigue=8, sources=8, claim="unconfirmed", claim_why="Only one source shows it.")
    orchestrator._rater_tag(m, rating, good, T0)
    t = store.get_claim_tag("w")
    assert (t.value, t.by, t.history[-1]["by"]) == ("unconfirmed", "rater", "writer")
    assert rating.total == 24 and rating.passed

def test_a_human_tag_stands_and_the_theorist_can_change_an_agent_tag():
    m = tagged_mission()
    claim_tags.set_tag(m, "unconfirmed", "writer", "w", T0)
    orchestrator.apply_theory(Theory(patterns=["p"], inspiration="", claim_tags=[
        NewClaimTag(mission="w", tag="confirmed", why="The filing shows it.")]), T0)
    assert (store.get_claim_tag("w").value, store.get_claim_tag("w").by) == ("confirmed", "theorist")
    claim_tags.set_tag(m, "unconfirmed", "human", "", T0)
    claim_tags.set_tag(m, "confirmed", "rater", "x", T0)
    assert (store.get_claim_tag("w").value, store.get_claim_tag("w").by) == ("unconfirmed", "human")
    dropped = []
    assert not theorist.settle_claim_tags([NewClaimTag(mission="w", tag="confirmed", why="x")], store.all_missions(), dropped)
    assert "the editor set this tag" in dropped[0]

def test_backfill_tags_missions_with_a_brief_once():
    store.save_mission(Mission(slug="old", topic="Old", state=State.DONE, published_at=T0))
    brief = stubs.research(store.get_mission("old"))
    store.save_artifact("old", "brief", brief)
    store.save_artifact("old", "article", article("It is possible that **x** [s1]."))
    store.save_mission(Mission(slug="new", topic="New"))
    assert claim_tags.backfill(T0) == 1 and claim_tags.backfill(T0) == 0
    assert store.get_claim_tag("old").value == "unconfirmed" and store.get_claim_tag("new") is None

def test_the_tag_never_reaches_the_published_site():
    store.save_mission(Mission(slug="pub", topic="Who paid the contractor?"))
    run_at(T0)
    m = store.get_mission("pub"); m.approved = True; store.save_mission(m)
    run_at(T0); run_at(T0 + timedelta(hours=48))
    m = store.get_mission("pub"); m.reviewed = True; store.save_mission(m)
    run_at(T0 + timedelta(hours=49))
    assert store.get_mission("pub").state == State.DONE and store.get_claim_tag("pub")
    for path in Path("site").rglob("*"):
        if path.is_file() and path.suffix in (".html", ".json", ".xml"):
            assert "claim: confirmed" not in path.read_text() and "claim_tag" not in path.read_text()

# ---------- what the human sees and does ----------

def test_ui_proposal_card_approve_mark_revive_and_tag(monkeypatch):
    desk()
    claim_tags.tag_brief(store.get_mission("brief"), stubs.research(store.get_mission("brief")), T0)
    orchestrator.propose_dead_lead("frame", "untestable", "It rests on a label the desk made up.", T0)
    client = TestClient(ui.app)
    home = client.get("/").text
    assert "Theorist proposes this is a dead lead." in home and "/dead-leads/frame/approve" in home
    assert 'class="claim-tag confirmed"' in home                       # the tiny tag on the brief's card
    r = client.post("/dead-leads/frame/approve", follow_redirects=False)
    assert r.status_code == 303 and store.get_mission("frame").state == State.DEAD_LEAD
    page = client.get("/m/frame").text
    assert "Untestable" in page and "/m/frame/requeue" in page
    client.post("/m/frame/requeue")
    assert store.get_mission("frame").state == State.QUEUED
    client.post("/m/frame/dead", data={"verdict": "no_record", "why": "No filing exists."})
    assert store.get_mission("frame").state == State.DEAD_LEAD and store.get_dead_lead("frame").by == "human"
    page = client.get("/m/brief").text
    assert "never published" in page and 'action="/m/brief/claim"' in page
    client.post("/m/brief/claim", data={"tag": "unconfirmed"})
    assert (store.get_claim_tag("brief").value, store.get_claim_tag("brief").by) == ("unconfirmed", "human")
    assert "error=claim" in client.post("/m/brief/claim", data={"tag": "maybe"}, follow_redirects=False).headers["location"]
    assert "error=dead_lead" in client.post("/m/writing/dead", data={"verdict": "refuted", "why": "x"},
                                            follow_redirects=False).headers["location"]

def test_chat_can_propose_a_dead_lead_and_set_a_tag():
    desk()
    assert "waits for the human" in status_tools.propose_dead_lead("frame", "untestable", "A label, not events.")
    assert store.get_dead_lead("frame").by == "chat"
    assert status_tools.set_claim_tag("brief", "unconfirmed", "Only one site shows it.") == "brief: claim: unconfirmed"
    out = status_tools.mission_status("frame")
    assert "Proposed as a dead lead" in out
    assert "Claim tag (ours only, never published): claim: unconfirmed" in status_tools.mission_status("brief")
