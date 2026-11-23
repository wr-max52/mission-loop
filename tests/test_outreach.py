"""Outreach: the Outreach agent picks a person, a channel and a question; code (contacts.py) writes the message from
the template, checks it, sends it on email, Bluesky or 1F916, records it and reads replies back; the orchestrator
waits for those replies (settings' timer, or until everyone answered) before writing."""
import json, os, smtplib, stat
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
import httpx, pytest
from ollama import Message
from starlette.testclient import TestClient
import agents, contacts, doctor, orchestrator, settings, stopping, store, ui
from agents import f916, llm, outreach_agent, stubs
from contracts import ChannelPost, Mission, OutreachRecord, State
from test_bluesky import FakeBluesky, sky   # noqa: F401  (fixture: a fake Bluesky server and login)

T0 = datetime(2026, 11, 22, 9, 0, tzinfo=timezone.utc)
SITE = "https://desk.example.org/"
Q = "Did the agency approve the contract before the audit?"

# ---------- fakes ----------

class FakeSMTP:
    sent: list = []
    bad_login = False
    refuse: set = set()

    def __init__(self, host, port, timeout=30, context=None):
        self.host, self.port = host, port
    def starttls(self, context=None): pass
    def login(self, user, password):
        if FakeSMTP.bad_login or password != "app-pw":
            raise smtplib.SMTPAuthenticationError(535, b"bad login")
    def send_message(self, msg):
        if msg["To"] in FakeSMTP.refuse:
            raise smtplib.SMTPRecipientsRefused({msg["To"]: (550, b"no such user")})
        FakeSMTP.sent.append(msg)
    def __enter__(self): return self
    def __exit__(self, *exc): pass

class FakeIMAP:
    inbox: list = []

    def __init__(self, host, timeout=30): pass
    def login(self, user, password): pass
    def select(self, box, readonly=False): return "OK", [b"1"]
    def search(self, charset, *criteria):
        ref = criteria[-1].strip('"')
        nums = [str(i + 1).encode() for i, raw in enumerate(FakeIMAP.inbox) if ref.encode() in raw]
        return "OK", [b" ".join(nums)]
    def fetch(self, num, what):
        raw = FakeIMAP.inbox[int(num) - 1]
        return "OK", [(b"1 (RFC822 {%d}" % len(raw), raw), b")"]
    def logout(self): pass

class FakeAppview:
    """Bluesky's public read API: handles, people search and post threads."""
    def __init__(self):
        self.handles = {"jane.bsky.social": "did:plc:jane"}
        self.threads: dict[str, list] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "public.api.bsky.app"
        p = request.url.params
        if request.url.path == "/xrpc/com.atproto.identity.resolveHandle":
            did = self.handles.get(p["handle"])
            return httpx.Response(200, json={"did": did}) if did else httpx.Response(400, json={"error": "InvalidRequest"})
        if request.url.path == "/xrpc/app.bsky.actor.searchActors":
            return httpx.Response(200, json={"actors": [{"handle": "jane.bsky.social", "displayName": "Jane Doe",
                                                         "description": "Energy reporter"}]})
        if request.url.path == "/xrpc/app.bsky.feed.getPostThread":
            return httpx.Response(200, json={"thread": {"post": {}, "replies": self.threads.get(p["uri"], [])}})
        if request.url.path == "/xrpc/app.bsky.feed.getPosts":
            return httpx.Response(200, json={"posts": [{"uri": u, "cid": f"cid-{u}"} for u in p.get_list("uris")]})
        return httpx.Response(404)

class FakeF916:
    """1f916.ai's post threads and comments."""
    def __init__(self):
        self.comments = {101: [{"id": 55, "author": "sam-agent", "body": "Was the audit public?", "parent_id": None,
                                "created_at": 1, "mod_state": "visible"},
                               {"id": 56, "author": "loop-desk", "body": "Our own comment", "parent_id": None,
                                "created_at": 2}]}
        self.posted = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.startswith("/api/post/"):
            pid = int(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"comments": self.comments.get(pid, [])})
        if request.method == "POST" and request.url.path == "/api/comment":
            assert request.headers["authorization"] == "Bearer 1f916_sk_test"
            self.posted.append(json.loads(request.content))
            return httpx.Response(201, json={"comment_id": 900 + len(self.posted)})
        return httpx.Response(404, json={"error": "no route"})

@pytest.fixture(autouse=True)
def setup(monkeypatch):
    FakeSMTP.sent, FakeSMTP.bad_login, FakeSMTP.refuse, FakeIMAP.inbox = [], False, set(), []
    settings.set_site_url(SITE)
    settings.set_outreach(True, 48, 3, list(settings.OUTREACH_CHANNELS))
    store.save_mission(Mission(slug="grid", topic="Texas grid", state=State.OUTREACH, approved=True))

@pytest.fixture
def mail(monkeypatch):
    monkeypatch.setattr(contacts, "SMTP", FakeSMTP)
    monkeypatch.setattr(contacts, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(contacts, "IMAP", FakeIMAP)
    settings.save_email_config(settings.clean_email_config("smtp.example.com", 587, "imap.example.com",
                                                           "desk@example.com", "app-pw", "Loop Desk", ""))

@pytest.fixture
def appview(monkeypatch):
    fake = FakeAppview()
    monkeypatch.setattr(contacts, "TRANSPORT", httpx.MockTransport(fake))
    return fake

@pytest.fixture
def forum(monkeypatch):
    fake = FakeF916()
    monkeypatch.setattr(f916, "TRANSPORT", httpx.MockTransport(fake))
    Path("data").mkdir(exist_ok=True)
    f916.KEY_FILE.write_text(json.dumps({"handle": "loop-desk", "secret": "1f916_sk_test"}))
    store.save_mission(Mission(slug="old", topic="Old article", state=State.DONE, published_at=T0,
                               channels={"1f916": ChannelPost(at=T0, where="https://1f916.ai/api/post/101")}))
    return fake

def send(channel="email", address="jane@agency.gov", name="Jane", topic="the Texas grid outage", question=Q,
         slug="grid", now=T0):
    return contacts.send(slug, channel, address, name, topic, question, now)

# ---------- the message ----------

def test_code_fills_the_template_with_the_site_address():
    assert contacts.message("Jane", "the grid", "Did it fail?") == (
        "Hi Jane, I'm an AI Agent on a research mission about the grid and I need your help answering this: "
        "Did it fail? You can learn about my missions here " + SITE)
    settings.set_site_url("")
    assert contacts.message("Jane", "the grid", "Did it fail?").endswith("answering this: Did it fail?")

# ---------- email ----------

def test_email_is_sent_from_the_template_and_recorded(mail):
    said = send()
    assert said.startswith("Sent to Jane (jane@agency.gov) on Email") and "You may ask 2 more" in said
    msg = FakeSMTP.sent[0]
    assert msg["To"] == "jane@agency.gov" and "Loop Desk" in msg["From"] and "desk@example.com" in msg["From"]
    assert "Texas grid outage" in msg["Subject"]
    body = msg.get_content()
    assert body.startswith("Hi Jane, I'm an AI Agent on a research mission about the Texas grid outage") and Q in body
    [r] = contacts.records("grid")
    assert (r.channel, r.address, r.name, r.question, r.ref) == ("email", "jane@agency.gov", "Jane", Q, msg["Message-ID"])

@pytest.mark.parametrize("fields, why", [
    ({"channel": "fax"}, "isn't an outreach channel that is set up. Use one of: email"),
    ({"address": "jane at agency"}, "isn't an email address"),
    ({"name": "Jane Smith"}, "use their first name only ('Jane', not 'Jane Smith')"),
    ({"question": "Did the audit happen"}, "ending in a single '?'"),
    ({"question": "Was it? Or not?"}, "ending in a single '?'"),
    ({"question": "Did it " + "really " * 40 + "happen?"}, "keep it between 10 and 200"),
    ({"topic": " ".join(["word"] * 20)}, "topic: a few words"),
    ({"question": "Is https://x.org right?"}, "no links"),
    ({"address": "desk@example.com"}, "our own address"),
])
def test_a_bad_request_is_not_sent_and_says_why(mail, fields, why):
    said = send(**fields)
    assert said.startswith("Not sent:") and why in said, said
    assert FakeSMTP.sent == [] and contacts.records("grid") == []

def test_a_title_and_surname_is_a_name_too(mail):
    assert send(name="Dr. Smith").startswith("Sent to Dr. Smith")

def test_nobody_is_asked_twice_and_a_mission_asks_at_most_its_count(mail):
    settings.set_outreach(True, 48, 2, ["email"])
    assert send().startswith("Sent")
    assert "already asked about this mission" in send(name="Jane")
    assert "That was the last person" in send(address="bo@agency.gov", name="Bo")
    assert "the most it may" in send(address="cy@agency.gov", name="Cy")
    assert len(FakeSMTP.sent) == 2

def test_one_person_is_asked_about_one_mission_a_week(mail):
    store.save_mission(Mission(slug="dam", topic="Dam", state=State.OUTREACH))
    assert send().startswith("Sent")
    assert "asked about another mission on 2026-11-22" in send(slug="dam", now=T0 + timedelta(days=3))
    assert send(slug="dam", now=T0 + timedelta(days=8)).startswith("Sent")

def test_mail_server_refusals_come_back_as_reasons(mail):
    FakeSMTP.refuse = {"gone@agency.gov"}
    assert "refused gone@agency.gov" in send(address="gone@agency.gov")
    FakeSMTP.bad_login = True
    assert "email login is wrong" in send()

def test_a_channel_not_set_up_or_unticked_is_refused(mail):
    settings.set_outreach(True, 48, 3, ["bluesky"])
    assert "no outreach channel is set up" in send()

# ---------- Bluesky ----------

def test_bluesky_post_mentions_the_person_and_links_the_site(sky, appview):
    said = send("bluesky", "@Jane.bsky.social", "Jane")
    assert said.startswith("Sent to Jane (jane.bsky.social) on Bluesky"), said
    post = sky.posts[0]
    assert post["text"].startswith("@jane.bsky.social Hi Jane, I'm an AI Agent on a research mission about")
    assert post["text"].endswith("You can learn about my missions here desk.example.org")   # shown short
    mention, link = post["facets"]
    assert mention["features"][0] == {"$type": "app.bsky.richtext.facet#mention", "did": "did:plc:jane"}
    assert mention["index"] == {"byteStart": 0, "byteEnd": len("@jane.bsky.social")}
    assert link["features"][0]["uri"] == SITE
    start = link["index"]["byteStart"]
    assert post["text"].encode()[start:link["index"]["byteEnd"]] == b"desk.example.org"
    [r] = contacts.records("grid")
    assert r.where == "https://bsky.app/profile/loopdesk.bsky.social/post/rk1" and r.ref.startswith("at://")

def test_a_bluesky_post_over_300_characters_says_how_much_to_cut(sky, appview):
    said = send("bluesky", "jane.bsky.social", "Jane", question="Did " + "the agency " * 15 + "approve it?")
    assert "over Bluesky's 300" in said and "by at least" in said
    assert sky.posts == []

def test_an_unknown_bluesky_handle_is_refused(sky, appview):
    assert "no Bluesky account has the handle @nobody.bsky.social" in send("bluesky", "nobody.bsky.social")
    assert "isn't a Bluesky handle" in send("bluesky", "jane smith")

def test_people_search_on_bluesky(appview):
    assert "@jane.bsky.social: Jane Doe. Energy reporter" in contacts.find_on_bluesky("Jane energy")

# ---------- 1F916 ----------

def test_1f916_outreach_replies_under_their_comment(forum):
    listed = contacts.commenters_text()
    assert '@sam-agent on our post about old: "Was the audit public?"' in listed and "loop-desk" not in listed
    assert send("1f916", "@sam-agent", "Sam").startswith("Sent to Sam")
    [posted] = forum.posted
    assert posted["post_id"] == 101 and posted["parent_id"] == 55
    assert posted["body"].startswith("@sam-agent Hi Sam, I'm an AI Agent")
    assert contacts.records("grid")[0].ref == "101:901"

def test_on_1f916_only_commenters_can_be_asked(forum):
    assert "hasn't commented on any of our 1F916 posts" in send("1f916", "stranger", "Lee")
    assert forum.posted == []

# ---------- replies ----------

def record(channel, address, ref, name="Jane") -> OutreachRecord:
    return OutreachRecord(channel=channel, address=address, name=name, topic="t", question=Q, message="m",
                          sent_at=T0, ref=ref)

def test_bluesky_replies_from_the_contact_are_kept_once(appview):
    appview.threads["at://p/1"] = [
        {"post": {"uri": "at://r/1", "author": {"handle": "stranger.bsky.social"}, "record": {"text": "lol"}}},
        {"post": {"uri": "at://r/2", "author": {"handle": "jane.bsky.social"},
                  "record": {"text": "Yes, in March.", "createdAt": "2026-11-22T10:00:00Z"}}, "replies": []}]
    found = contacts.check_replies([record("bluesky", "jane.bsky.social", "at://p/1")], T0)
    found = contacts.check_replies(found, T0)
    assert [r["text"] for r in found[0].replies] == ["Yes, in March."] and found[0].replies[0]["is_comment"]

def test_1f916_replies_under_our_comment_are_kept(forum):
    forum.comments[101].append({"id": 70, "author": "sam-agent", "body": "No, it was sealed.", "parent_id": 901,
                                "created_at": 3})
    [r] = contacts.check_replies([record("1f916", "sam-agent", "101:901", "Sam")], T0)
    assert r.replies[0]["text"] == "No, it was sealed."

def test_email_replies_are_read_without_the_quoted_message(mail):
    reply = EmailMessage()
    reply["From"], reply["To"], reply["Subject"] = "Jane <Jane@Agency.gov>", "desk@example.com", "Re: question"
    reply["In-Reply-To"], reply["Message-ID"] = "<abc@example.com>", "<r1@agency.gov>"
    reply.set_content("Yes, two weeks before.\n\nOn Mon, Nov 22, 2026 Loop Desk wrote:\n> Hi Jane, ...\n")
    FakeIMAP.inbox = [bytes(reply)]
    [r] = contacts.check_replies([record("email", "jane@agency.gov", "<abc@example.com>")], T0)
    assert r.replies[0]["text"] == "Yes, two weeks before." and contacts.answered(r)

def email_answer(text="Yes, two weeks before.", mid="<r1@agency.gov>") -> bytes:
    reply = EmailMessage()
    reply["From"], reply["To"], reply["Subject"] = "Jane <jane@agency.gov>", "desk@example.com", "Re: question"
    reply["In-Reply-To"], reply["Message-ID"] = "<abc@example.com>", mid
    reply.set_content(text + "\n")
    return bytes(reply)

def test_an_answer_by_email_gets_one_canned_thank_you_in_its_thread(mail):
    FakeIMAP.inbox = [email_answer()]
    [r] = contacts.check_replies([record("email", "jane@agency.gov", "<abc@example.com>")], T0)
    [thanks] = FakeSMTP.sent
    assert thanks["To"] == "jane@agency.gov" and thanks["In-Reply-To"] == "<r1@agency.gov>"
    assert thanks["Subject"].startswith("Re: A question from an AI research agent")
    assert thanks.get_content().strip() == ("Thanks Jane, that really helps. I'll weigh your answer in my research. "
                                            "You can learn about my missions here " + SITE)
    assert r.thanked_at == T0 and r.thanks_where == "email"
    FakeIMAP.inbox.append(email_answer("One more thing.", "<r2@agency.gov>"))
    [r] = contacts.check_replies([r], T0 + timedelta(hours=1))
    assert len(r.replies) == 2 and len(FakeSMTP.sent) == 1          # thanked once, not once per answer

def test_nobody_is_thanked_before_they_answer(mail):
    [r] = contacts.check_replies([record("email", "jane@agency.gov", "<abc@example.com>")], T0)
    assert FakeSMTP.sent == [] and r.thanked_at is None

def test_a_thank_you_that_fails_is_tried_again_at_the_next_check(mail, monkeypatch):
    FakeIMAP.inbox = [email_answer()]
    monkeypatch.setattr(contacts, "SMTP", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    [r] = contacts.check_replies([record("email", "jane@agency.gov", "<abc@example.com>")], T0)
    assert r.replies and r.thanked_at is None
    monkeypatch.setattr(contacts, "SMTP", FakeSMTP)
    [r] = contacts.check_replies([r], T0 + timedelta(minutes=10))
    assert r.thanked_at and len(FakeSMTP.sent) == 1

def test_an_answer_on_bluesky_gets_a_reply_in_the_thread(sky, appview):
    appview.threads["at://p/1"] = [{"post": {"uri": "at://r/2", "author": {"handle": "jane.bsky.social"},
                                             "record": {"text": "Yes, in March."}}}]
    [r] = contacts.check_replies([record("bluesky", "jane.bsky.social", "at://p/1")], T0)
    [post] = sky.posts
    assert post["reply"] == {"root": {"uri": "at://p/1", "cid": "cid-at://p/1"},
                             "parent": {"uri": "at://r/2", "cid": "cid-at://r/2"}}
    assert post["text"].startswith("Thanks Jane, that really helps.") and post["text"].endswith("desk.example.org")
    assert post["facets"][0]["features"][0]["uri"] == SITE
    assert r.thanks_where == "https://bsky.app/profile/loopdesk.bsky.social/post/rk1"
    contacts.check_replies([r], T0 + timedelta(hours=1))
    assert len(sky.posts) == 1          # our own reply is never read back as their answer, nor thanked again

def test_an_answer_on_1f916_gets_a_reply_under_it(forum):
    forum.comments[101].append({"id": 70, "author": "sam-agent", "body": "No, it was sealed.", "parent_id": 901,
                                "created_at": 3})
    [r] = contacts.check_replies([record("1f916", "sam-agent", "101:901", "Sam")], T0)
    [posted] = forum.posted
    assert posted["post_id"] == 101 and posted["parent_id"] == 70
    assert posted["body"].startswith("@sam-agent Thanks Sam, that really helps.")
    assert r.thanked_at == T0 and r.thanks_where.endswith("/api/comment/901")

def test_a_channel_that_cant_be_read_is_skipped(mail, monkeypatch):
    monkeypatch.setattr(contacts, "IMAP", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    [r] = contacts.check_replies([record("email", "jane@agency.gov", "<abc@example.com>")], T0)
    assert r.replies == []

# ---------- the step in the loop ----------

def run_at(t): orchestrator.run_until_idle(lambda: t)

def approved(slug="m"):
    m = Mission(slug=slug, topic="Heat pumps", state=State.BRIEF_READY, approved=True)
    store.save_artifact(slug, "brief", stubs.research(m))
    store.save_mission(m)

@pytest.mark.usefixtures("stub_agents")
def test_an_approved_brief_goes_to_outreach_then_waits_for_replies():
    approved()
    run_at(T0)
    m = store.get_mission("m")
    assert m.state == State.AWAITING_COMMENTS and m.wait_until == T0 + timedelta(hours=48)
    assert len(contacts.records("m")) == 3
    notes = [e["note"] for e in store.events("m")]
    assert "brief approved; outreach next" in notes and any(n.startswith("asked Person1 (email)") for n in notes)
    run_at(T0 + timedelta(hours=47))
    assert store.get_mission("m").state == State.AWAITING_COMMENTS
    run_at(T0 + timedelta(hours=48))
    assert store.get_mission("m").state == State.ARTICLE_READY

@pytest.mark.usefixtures("stub_agents")
def test_the_wait_follows_the_settings_timer():
    settings.set_outreach(True, 2, 3, ["email"])
    approved()
    run_at(T0)
    run_at(T0 + timedelta(hours=2))
    assert store.get_mission("m").state == State.ARTICLE_READY

@pytest.mark.usefixtures("stub_agents")
def test_writing_starts_once_everyone_has_answered(monkeypatch):
    def answers(records, now):
        if now >= T0 + timedelta(hours=1):
            for r in records:
                r.replies, r.thanked_at = [{"id": "x", "from": r.name, "text": "Yes.", "is_comment": True}], now
        return records
    monkeypatch.setattr(agents, "check_replies", answers)
    approved()
    run_at(T0)
    run_at(T0 + timedelta(minutes=5))              # replies are checked at most every 10 minutes
    assert store.get_mission("m").state == State.AWAITING_COMMENTS
    run_at(T0 + timedelta(hours=1))
    assert store.get_mission("m").state == State.ARTICLE_READY
    assert any("every contact answered (3); thanked 3; writing next" in e["note"] for e in store.events("m"))
    assert "Thanked " in TestClient(ui.app).get("/m/m").text

@pytest.mark.usefixtures("stub_agents")
@pytest.mark.parametrize("outcome, note", [([], "outreach asked nobody; writing next"),
                                           (RuntimeError("model gone"), "outreach crashed (model gone)")])
def test_outreach_that_asks_nobody_or_crashes_never_blocks(monkeypatch, outcome, note):
    def run(m, brief, now):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(agents, "outreach", run)
    approved()
    run_at(T0)
    assert store.get_mission("m").state == State.ARTICLE_READY
    assert any(note in e["note"] for e in store.events("m"))

@pytest.mark.usefixtures("stub_agents")
def test_a_stop_mid_outreach_carries_on_next_start(monkeypatch):
    def stopped(m, brief, now):
        raise stopping.Interrupted([])
    monkeypatch.setattr(agents, "outreach", stopped)
    approved()
    run_at(T0)
    assert store.get_mission("m").state == State.OUTREACH

@pytest.mark.usefixtures("stub_agents")
def test_with_no_channel_set_up_missions_skip_outreach(monkeypatch):
    monkeypatch.setattr(agents, "outreach_channels", lambda: [])
    monkeypatch.setattr(agents, "outreach", lambda *a: pytest.fail("outreach ran with no channel"))
    approved()
    run_at(T0)
    assert store.get_mission("m").state == State.ARTICLE_READY

def test_outreach_is_the_principal_investigators_task():
    assert orchestrator.role(Mission(slug="x", topic="t", state=State.OUTREACH)) == "pi"

# ---------- the agent ----------

def test_the_guard_refuses_an_email_address_the_run_never_saw():
    guard = outreach_agent.OutreachGuard(["fetch", "send_outreach"])
    ask = {"channel": "email", "address": "jane@agency.gov", "name": "Jane", "topic": "t", "question": Q}
    assert "isn't on any page or search result" in guard.refuse("send_outreach", ask)
    guard.explain("fetch", {"url": "https://agency.gov/press"}, "Press office: Jane@Agency.gov")
    assert guard.refuse("send_outreach", ask) is None

def tool_call(_tool, **args):
    return Message(role="assistant", content="", tool_calls=[
        Message.ToolCall(function=Message.ToolCall.Function(name=_tool, arguments=args))])

class FakeToolbox:
    opened: list = []

    def __init__(self, enabled, env=None):
        FakeToolbox.opened.append((enabled, env))
        self.env, self.skipped = env, {}
        self.tools = [{"type": "function", "function": {"name": n}} for n in ("fetch", "send_outreach")]
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): pass
    async def call(self, name, args):
        if name == "fetch":
            return "Agency press office. Contact Jane Doe at jane@agency.gov."
        return contacts.send(self.env["MISSION_LOOP_MISSION"], **args)   # what outreach_tools.py does

async def test_the_agent_asks_through_the_tool_for_its_own_mission(mail, monkeypatch):
    FakeToolbox.opened = []
    m = store.get_mission("grid")
    brief = stubs.research(m)
    replies = iter([tool_call("fetch", url=brief.sources[0].url),   # an address the prompt shows
                    tool_call("send_outreach", channel="email", address="jane@agency.gov", name="Jane",
                              topic="the Texas grid outage", question=Q),
                    Message(role="assistant", content="DONE")])
    seen = []
    async def fake_chat(messages, tools=None, format=None, model=None):
        seen.append((messages[0]["content"], model))
        return next(replies)
    monkeypatch.setattr(llm, "chat", fake_chat)
    found = await outreach_agent.run_outreach(m, brief, T0, toolbox_factory=FakeToolbox)
    assert [r.address for r in found] == ["jane@agency.gov"] and len(FakeSMTP.sent) == 1
    assert FakeToolbox.opened[0][1] == {"MISSION_LOOP_MISSION": "grid"}
    assert "outreach" in FakeToolbox.opened[0][0]
    system, model = seen[0]
    assert model == "big-test:30b"                                   # the Principal Investigator's model
    assert brief.thesis in system and "I need your help answering this: <question>" in system
    assert "email (an email address)" in system and "bluesky" not in system.split("Channels you can use:")[1][:60]

async def test_the_agent_is_not_started_without_a_channel(monkeypatch):
    settings.set_outreach(True, 48, 3, [])
    m = store.get_mission("grid")
    found = await outreach_agent.run_outreach(m, stubs.research(m), T0,
                                              toolbox_factory=lambda *a, **k: pytest.fail("toolbox opened"))
    assert found == []

# ---------- settings and pages ----------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(orchestrator, "LOCK_FILE", Path("data/loop.lock"))
    return TestClient(ui.app)

def test_outreach_settings_are_saved_and_shown(client):
    page = client.get("/settings").text
    assert 'id="outreach"' in page and "Wait for replies" in page and "People per mission" in page
    assert "I&#x27;m an AI Agent on a research mission about the 2024 Texas grid outage" in page
    r = client.post("/settings/outreach", data={"on": "on", "wait": "12", "contacts": "2", "channels": ["email"]},
                    follow_redirects=False)
    assert r.status_code in (302, 303) and "done=outreach_saved" in r.headers["location"]
    assert settings.outreach_on() and settings.outreach_wait_hours() == 12 and settings.outreach_contacts() == 2
    assert settings.outreach_channels() == ["email"]
    client.post("/settings/outreach", data={"wait": "48", "contacts": "3", "channels": list(settings.OUTREACH_CHANNELS)})
    assert not settings.outreach_on() and "outreach_wait_hours" not in settings.saved()

def test_outreach_settings_refuse_out_of_range(client):
    r = client.post("/settings/outreach", data={"on": "on", "wait": "9999", "contacts": "3"}, follow_redirects=False)
    assert "error=outreach" in r.headers["location"]

def test_email_is_checked_before_it_is_saved(client, monkeypatch):
    monkeypatch.setattr(contacts, "SMTP", FakeSMTP)
    monkeypatch.setattr(contacts, "IMAP", FakeIMAP)
    form = {"smtp_host": "smtp.example.com", "smtp_port": "587", "imap_host": "imap.example.com",
            "username": "desk@example.com", "password": "wrong", "from_name": "", "from_address": ""}
    page = client.post("/settings/email", data=form).text
    assert "Email wasn&#x27;t saved: the mail server refused" in page and not settings.EMAIL_FILE.exists()
    client.post("/settings/email", data=form | {"password": "app-pw"})
    assert settings.email_config()["password"] == "app-pw"
    assert stat.S_IMODE(os.stat(settings.EMAIL_FILE).st_mode) == 0o600
    page = client.get("/settings").text
    assert "app-pw" not in page
    assert "Sending as" in page and "Replies are read from imap.example.com" in page
    client.post("/settings/email", data=form | {"password": "", "from_name": "Desk"})   # blank keeps the password
    assert settings.email_config()["password"] == "app-pw" and settings.email_config()["from_name"] == "Desk"
    client.post("/settings/email", data={"forget": "1"})
    assert settings.email_config() == {}

def test_the_mission_page_shows_who_was_asked_and_what_they_said(client):
    r = record("email", "jane@agency.gov", "<a@b>")
    r.message = "Hi Jane, I'm an AI Agent ..."
    r.replies = [{"id": "1", "from": "Jane", "text": "Yes <b>really</b>.", "at": "2026-11-22T10:00", "is_comment": True}]
    store.save_artifact("grid", "outreach", [r])
    m = store.get_mission("grid")
    m.state, m.wait_until = State.AWAITING_COMMENTS, T0 + timedelta(hours=48)
    store.save_mission(m)
    page = client.get("/m/grid").text
    assert "Outreach: 1 of 1 answered" in page and "Yes &lt;b&gt;really&lt;/b&gt;." in page
    assert "Waiting for replies until Nov 24, 09:00 UTC · 1 of 1 answered" in page

def test_the_setup_check_flags_outreach_with_no_channel():
    settings.set_outreach(True, 48, 3, ["email"])
    [check] = [c for c in doctor.checks() if c.key == "outreach"]
    assert not check.ok and check.link == "/settings#outreach"
    settings.set_outreach(False, 48, 3, ["email"])
    assert not [c for c in doctor.checks() if c.key == "outreach"]
