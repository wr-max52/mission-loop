from checks.article_check import check_article, MIN_WORDS
from contracts import Mission, ResearchBrief, Source, POC, Claim, Article, Section, OutreachRecord

M = Mission(slug="a", topic="t")
Q1 = "The committee advanced the bill by a vote of 14 to 9 on March 3."
Q2 = "Critics argue the disclosure rules already cover the same trades."
FILL = " ".join(["The record shows further procedural detail."] * 12)   # padding to clear MIN_WORDS

BRIEF = ResearchBrief(
    thesis="The bill advanced", claims=[Claim(text="It advanced", source_ids=["gov"])],
    counterpoint="Critics say it duplicates rules",
    sources=[Source(id="gov", url="https://congress.gov/x", title="G", quote=Q1, stance="supports"),
             Source(id="news", url="https://news.example/y", title="N", quote=Q1, stance="supports"),
             Source(id="op", url="https://op.example/z", title="O", quote=Q2, stance="contradicts")],
    pocs=[POC(name=f"P{i}", role="r", email=f"p{i}@x.org", contact_source_url="https://x.org", why_them="w")
          for i in range(3)],
    question_for_pocs="?")

def article(**sections) -> Article:
    secs = sections or {
        "What happened": [f'The committee said "{Q1}" [gov]. {FILL} [news]', f"{FILL} [gov, news]"],
        "The other side": [f"Critics disagree [op]. {FILL} [op]", f"{FILL} [gov]"],
    }
    return Article(title="Bill advances", dek="A committee vote moved it forward.",
                   sections=[Section(heading=h, paragraphs=p) for h, p in secs.items()])

def test_good_article_passes():
    ok, notes = check_article(M, BRIEF, article())
    assert ok, notes

def test_uncited_paragraph_fails():
    a = article()
    a.sections[0].paragraphs.append("An unsupported paragraph with no citation at all.")
    ok, notes = check_article(M, BRIEF, a)
    assert not ok and "has no citation" in notes

def test_unknown_source_id_fails():
    a = article()
    a.sections[1].paragraphs[1] += " [wiki]"
    ok, notes = check_article(M, BRIEF, a)
    assert not ok and "unknown source id 'wiki'" in notes

def test_url_in_text_fails():
    a = article()
    a.sections[1].paragraphs[1] += " See https://evil.example for more."
    ok, notes = check_article(M, BRIEF, a)
    assert not ok and "URL" in notes

def test_invented_quote_fails():
    a = article()
    a.sections[0].paragraphs[1] = f'The chair called it "the most important reform in fifty years of history" [gov]. {FILL}'
    ok, notes = check_article(M, BRIEF, a)
    assert not ok and "quotes words not found" in notes

def test_short_scare_quotes_are_allowed():
    a = article()
    a.sections[0].paragraphs[1] = f'Supporters call it a "clean" bill [gov]. {FILL}'
    ok, notes = check_article(M, BRIEF, a)
    assert ok, notes

def test_poc_comment_can_be_quoted():
    said = "We expect a floor vote before the August recess begins."
    rec = OutreachRecord(poc_email="p0@x.org", gmail_thread_id="t", sent_at="2026-11-02T09:00:00Z",
                         replies=[{"is_comment": True, "from": "P0", "text": said}])
    a = article()
    a.sections[0].paragraphs[1] = f'One aide said "{said}" [gov]. {FILL}'
    assert not check_article(M, BRIEF, a)[0]
    ok, notes = check_article(M, BRIEF, a, [rec])
    assert ok, notes

def test_opposing_view_must_be_cited():
    a = article(**{"One": [f"{FILL} [gov]", f"{FILL} [news]"], "Two": [f"{FILL} [gov]", f"{FILL} [gov]"]})
    ok, notes = check_article(M, BRIEF, a)
    assert not ok and "opposing view is not cited" in notes

def test_too_short_fails():
    a = article(**{"One": ["Short [gov]."], "Two": ["Also short [op]."]})
    ok, notes = check_article(M, BRIEF, a)
    assert not ok and f"at least {MIN_WORDS}" in notes

MAYBE = "It is possible that the vote was moved up [gov]."
BRIEF_WITH_POSSIBILITY = BRIEF.model_copy(update={"claims": BRIEF.claims + [
    Claim(text="It is possible that the vote was moved up", source_ids=["gov"], kind="possibility")]})

def test_possibility_from_the_brief_passes():
    a = article()
    a.sections[0].paragraphs.append(MAYBE)
    ok, notes = check_article(M, BRIEF_WITH_POSSIBILITY, a)
    assert ok, notes

def test_possibility_not_in_the_brief_fails():
    a = article()
    a.sections[0].paragraphs.append(MAYBE)
    ok, notes = check_article(M, BRIEF, a)
    assert not ok and "brief has no possibilities" in notes

def test_brief_possibilities_must_reach_the_article():
    ok, notes = check_article(M, BRIEF_WITH_POSSIBILITY, article())
    assert not ok and "possibilities are missing" in notes

# A model sometimes uses a source's URL as its id, and the writer then cites [https://...] (it did what the
# brief said). Code gives such sources short ids and points citations of a brief source's URL at its id.
from contracts import cite_by_id

URL_ID_BRIEF = ResearchBrief(**{**BRIEF.model_dump(), "sources": [
    {**s.model_dump(), "id": s.url} for s in BRIEF.sources],
    "claims": [{"text": "It advanced", "source_ids": ["https://congress.gov/x"]}]})

def test_url_ids_become_short_ids_and_claims_follow():
    assert [s.id for s in URL_ID_BRIEF.sources] == ["s1", "s2", "s3"]
    assert URL_ID_BRIEF.claims[0].source_ids == ["s1"]

def test_short_ids_skip_ones_already_taken():
    b = ResearchBrief(**{**BRIEF.model_dump(), "sources": [
        {**BRIEF.sources[0].model_dump(), "id": "s1"}, {**BRIEF.sources[1].model_dump(), "id": "https://news.example/y"}],
        "claims": [{"text": "c", "source_ids": ["s1", "https://news.example/y/"]}]})
    assert [s.id for s in b.sources] == ["s1", "s2"] and b.claims[0].source_ids == ["s1", "s2"]

def test_a_claim_citing_a_url_points_at_that_sources_id():
    b = ResearchBrief(**{**BRIEF.model_dump(), "claims": [{"text": "c", "source_ids": ["https://op.example/z"]}]})
    assert b.claims[0].source_ids == ["op"]

def test_saved_brief_with_url_ids_is_fixed_when_read_back():
    raw = BRIEF.model_dump()
    for s in raw["sources"]:
        s["id"] = s["url"]
    assert ResearchBrief(**raw).sources[2].id == "s3"

def test_citations_of_brief_urls_become_ids():
    text = "It passed [https://congress.gov/x]. Critics object [https://op.example/z/, news]."
    assert cite_by_id(text, BRIEF) == "It passed [gov]. Critics object [op, news]."

def test_outside_urls_are_left_for_the_check():
    text = "See [https://congress.gov/x, https://evil.example] and [link](https://congress.gov/x)."
    assert cite_by_id(text, BRIEF) == text

def test_article_citing_by_url_passes_once_rewritten():
    a = article()
    for sec in a.sections:
        sec.paragraphs = [p.replace("[gov]", "[https://congress.gov/x]").replace("[op]", "[https://op.example/z]")
                          for p in sec.paragraphs]
    assert not check_article(M, BRIEF, a)[0]
    for sec in a.sections:
        sec.paragraphs = [cite_by_id(p, BRIEF) for p in sec.paragraphs]
    ok, notes = check_article(M, BRIEF, a)
    assert ok, notes

# --- the claim box and bold key words ------------------------------------------------------------------------

def test_a_cited_claim_box_and_bold_words_pass():
    a = article().model_copy(update={"claim": "**The committee advanced** the bill, **14 to 9** [gov]."})
    a.sections[0].paragraphs[0] = f'The committee said "{Q1}" [gov]. **It voted** on March 3 [news]. {FILL} [news]'
    assert check_article(M, BRIEF, a) == (True, "")

def test_bold_inside_a_quotation_still_matches_the_source():
    a = article()
    a.sections[0].paragraphs[0] = 'The committee said "The committee **advanced the bill** by a vote of 14" [gov]. ' + FILL
    assert check_article(M, BRIEF, a)[0]

def test_a_claim_box_is_checked_like_a_paragraph():
    for claim, problem in [("The committee advanced it.", "The claim at the top has no citation"),
                           ("It advanced [nope].", "unknown source id 'nope'"),
                           ("It is possible that it advanced [gov].", "brief has no possibilities")]:
        ok, notes = check_article(M, BRIEF, article().model_copy(update={"claim": claim}))
        assert not ok and problem in notes

def test_an_article_from_before_the_claim_box_still_passes():
    assert article().claim == "" and check_article(M, BRIEF, article())[0]
