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
