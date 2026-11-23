"""The avatar studio: avatars saved on this machine, every page handed them, and the designer held to real parts."""
import asyncio, base64, json
from pathlib import Path
import ollama, pytest
from starlette.testclient import TestClient
import agents, avatars, settings, ui
from agents import avatar, llm

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = json.loads((ROOT / "tools" / "example-sprite.json").read_text())
CATALOG = {   # what assets/researchers.js sends (PARTS, FLAG_KEYS, COLOR_KEYS), trimmed to what the tests use
    "worker": ["assistant", "pi"], "style": ["short", "bob"], "head": [None, "cap", "beanie"], "outfit": ["labcoat", "hoodie"],
    "face": [None, "round"], "facial": [None, "mustache"], "legs": ["pants", "skirt"], "prop": [None, "mug"],
    "brow": ["flat", "stern"], "mouth": ["none", "flat"], "eyes": ["open", "wide"], "arm": ["swing", "up"],
    "layer": ["back", "head"], "icon": ["heart"], "flags": ["lash", "ahoge"], "colors": ["hair", "top", "beanie", "cap"]}

@pytest.fixture
def client():
    return TestClient(ui.app)

# ---------- data/sprites.json ----------

def test_saving_names_the_next_custom_sprite_and_keeps_order():
    assert avatars.next_key() == "custom-sprite-1"
    avatars.save("custom-sprite-1", EXAMPLE)
    avatars.save("zed", dict(EXAMPLE, name="Zed"))
    assert avatars.next_key() == "custom-sprite-2"
    avatars.save("nova", dict(EXAMPLE, name="Nova"), replace="custom-sprite-1")    # editing renames in place
    assert list(avatars.custom()) == ["nova", "zed"] and avatars.custom()["nova"]["name"] == "Nova"

@pytest.mark.parametrize("key, spec, says", [
    ("Bad Key", EXAMPLE, "lowercase"),
    ("pip", EXAMPLE, "built-in"),
    ("x", {"name": "x"}, "starts from one of the built-in"),
    ("x", dict(EXAMPLE, maps=[{"rows": ["k" * 24] * 24}] * 60), "too big"),
])
def test_saving_refuses_what_a_person_can_fix(key, spec, says):
    with pytest.raises(avatars.AvatarError, match=says):
        avatars.save(key, spec)

def test_a_taken_name_is_refused_unless_it_is_the_one_being_edited():
    avatars.save("nova", EXAMPLE)
    with pytest.raises(avatars.AvatarError, match="already"):
        avatars.save("nova", EXAMPLE)
    avatars.save("nova", dict(EXAMPLE, name="Nova 2"), replace="nova")
    assert avatars.custom()["nova"]["name"] == "Nova 2"

def test_switching_off_and_deleting():
    avatars.save("nova", EXAMPLE)
    avatars.set_on("pip", False); avatars.set_on("nova", False)
    assert avatars.off() == ["pip", "nova"]
    avatars.set_on("pip", True)
    assert avatars.off() == ["nova"]
    assert avatars.delete("nova") and avatars.off() == [] and not avatars.delete("nova")
    with pytest.raises(avatars.AvatarError):
        avatars.set_on("nobody", False)

def test_every_page_hands_the_sprites_over_without_breaking_out_of_the_script(client):
    assert "RESEARCHER_SPRITES" not in client.get("/").text        # nothing customised: nothing added
    avatars.save("nova", dict(EXAMPLE, name="</script><b>x"))
    avatars.set_on("pip", False)
    html = client.get("/settings").text
    tag = html.split("<script>window.RESEARCHER_SPRITES=")[1].split(";</script>")[0]
    assert "</script>" not in tag and json.loads(tag) == {"add": avatars.custom(), "off": ["pip"]}
    assert 'href="/avatars"' in html and "1 of your own" in html and "1 staying home" in html

# ---------- the studio's routes ----------

def test_the_studio_page(client):
    html = client.get("/avatars").text
    assert 'id="studio" data-next="custom-sprite-1"' in html
    assert html.index("/assets/researchers.js") < html.index("/assets/avatars.js")
    assert client.get("/assets/avatars.js").status_code == 200
    assert "didn't load" in client.get("/assets/avatars.js").text          # it says so instead of staying blank

def test_the_sprite_studio_opens_in_the_app(client):
    """tools/sprites.html, served by the app with the drawing script at its stamped address."""
    page = client.get("/sprites")
    assert page.status_code == 200 and "<h1>Sprite studio</h1>" in page.text
    assert f'<script src="{ui.asset("researchers.js")}"></script>' in page.text and "../assets" not in page.text
    assert 'href="/settings"' in page.text and page.headers["cache-control"] == "no-cache"
    assert client.get("/sprites/example.json").json()["base"] == "mei"
    assert 'fetch("/assets/cyborgs.json")' in page.text and len(client.get("/assets/cyborgs.json").json()) == 12
    assert 'href="/sprites"' in client.get("/settings").text

def test_a_script_address_changes_when_the_script_does(tmp_path, monkeypatch):
    """So a browser never goes on using a copy from before an update."""
    monkeypatch.setattr(ui, "ASSETS", tmp_path)
    (tmp_path / "x.js").write_text("one")
    first = ui.asset("x.js")
    assert first.startswith("/assets/x.js?v=") and ui.asset("x.js") == first
    (tmp_path / "x.js").write_text("three")                              # a different length, whatever the clock says
    assert ui.asset("x.js") != first
    assert ui.asset("gone.js") == "/assets/gone.js"

def test_save_toggle_and_delete_through_the_ui(client):
    r = client.post("/avatars/save", json={"key": "nova", "spec": EXAMPLE})
    assert r.json() == {"ok": True, "key": "nova"} and avatars.custom()["nova"] == EXAMPLE
    assert client.post("/avatars/save", json={"key": "pip", "spec": EXAMPLE}).status_code == 400
    assert client.post("/avatars/nova/off").json() == {"ok": True} and avatars.off() == ["nova"]
    assert client.post("/avatars/nova/delete").json() == {"ok": True} and avatars.custom() == {}
    assert client.post("/avatars/nova/delete").status_code == 404

def test_another_site_cant_save_or_ask(client):
    bad = {"origin": "https://evil.example"}
    assert client.post("/avatars/save", json={"key": "nova", "spec": EXAMPLE}, headers=bad).status_code == 403
    assert client.post("/avatars/ask", json={"spec": EXAMPLE, "message": "hi"}, headers=bad).status_code == 403
    assert client.post("/avatars/pip/off", headers=bad).status_code == 403 and avatars.off() == []

def test_ask_hands_everything_to_the_designer(client, monkeypatch):
    seen = {}
    def fake(spec, message, look, model, image, errors, catalog):
        seen.update(spec=spec, message=message, model=model, errors=errors, catalog=catalog)
        return {"reply": "Done.", "refused": False, "spec": dict(spec, hair="#ff8fc7"), "change": {"hair": "#ff8fc7"}}
    monkeypatch.setattr(agents, "edit_avatar", fake)
    r = client.post("/avatars/ask", json={"spec": EXAMPLE, "message": "pink hair", "model": "m", "catalog": CATALOG,
                                          "errors": ["maps[0]: covers the face"]})
    assert r.json()["spec"]["hair"] == "#ff8fc7"
    assert seen == {"spec": EXAMPLE, "message": "pink hair", "model": "m", "errors": ["maps[0]: covers the face"], "catalog": CATALOG}

def test_ask_says_when_the_model_is_out(client, monkeypatch):
    def down(*a):
        raise ConnectionError("refused")
    monkeypatch.setattr(agents, "edit_avatar", down)
    r = client.post("/avatars/ask", json={"spec": EXAMPLE, "message": "pink hair"})
    assert r.status_code == 502 and "Is Ollama running?" in r.json()["error"]

# ---------- the designer ----------

def fake_model(monkeypatch, answer: dict, calls: list | None = None):
    async def chat(messages, tools=None, format=None, model=None, stoppable=True, think=None):
        if calls is not None:
            calls.append({"messages": messages, "format": format, "model": model, "stoppable": stoppable, "think": think})
        return ollama.Message(role="assistant", content=json.dumps(answer))
    monkeypatch.setattr(llm, "chat", chat)

def test_the_answer_may_only_name_parts_that_exist_in_the_order_people_describe_them():
    s = avatar.schema(CATALOG)
    assert s["required"] == ["refused", "change", "reply"]                 # decide, change, then say what changed
    change = s["properties"]["change"]
    assert change["properties"]["head"] == {"enum": [None, "cap", "beanie"]} and change["additionalProperties"] is False
    order = list(change["properties"])
    assert order.index("head") < order.index("beanie") < order.index("outfit") < order.index("top")   # each part, then its colors
    assert change["properties"]["hair"]["pattern"] == "^#[0-9a-fA-F]{6}$"
    assert avatar.schema(None)["properties"]["change"] == {"type": "object"}   # no catalog: the browser still checks

def test_an_edit_lays_only_real_changes_over_the_spec(monkeypatch):
    calls = []
    fake_model(monkeypatch, {"refused": False, "change": {"head": "beanie", "beanie": "#c0392b", "outfit": "labcoat", "base": "pip"},
                             "reply": "A red beanie."}, calls)
    out = avatar.edit({"base": "mei", "name": "M"}, "a red beanie", look={"outfit": "labcoat"}, model="m", catalog=CATALOG)
    assert out["change"] == {"head": "beanie", "beanie": "#c0392b"}            # the coat it already had isn't a change
    assert out["spec"] == {"base": "mei", "name": "M", "head": "beanie", "beanie": "#c0392b"}   # and base never moves
    call = calls[0]
    assert call["think"] is False and call["stoppable"] is False and call["format"] == avatar.schema(CATALOG)
    system = call["messages"][0]["content"]
    assert "## Maps" in system and "## How animation works" not in system   # the rulebook is SPRITES.md itself

def test_a_reply_that_changes_nothing_says_so(monkeypatch):
    fake_model(monkeypatch, {"refused": False, "change": {"top": "#f4f4f0"}, "reply": "Now in your picture's purple!"})
    out = avatar.edit({"base": "mei", "name": "M"}, "my colours", look={"top": "#f4f4f0"}, model="m")
    assert out["change"] == {} and out["reply"].startswith("I couldn't work out a change")

def test_an_empty_maps_list_takes_the_drawings_off():
    assert "maps" not in avatar.merge(EXAMPLE, {"maps": []})

def test_nudity_is_refused_before_any_model_is_asked(monkeypatch):
    async def never(*a, **k):
        raise AssertionError("the model was asked")
    monkeypatch.setattr(llm, "chat", never)
    out = avatar.edit(EXAMPLE, "make her naked", model="m")
    assert out["refused"] and out["spec"] == EXAMPLE and "clothed" in out["reply"]
    assert agents.edit_avatar is not None

def test_a_picture_goes_only_to_a_local_model_that_can_see(monkeypatch):
    pic = base64.b64encode(b"\x89PNG fake").decode()
    for model, sees, attached in [("bonsai:4b", True, True), ("llama:7b", False, False), ("anthropic/claude", None, False)]:
        calls = []
        fake_model(monkeypatch, {"refused": False, "change": {"hair": "#7048e8"}, "reply": "Purple."}, calls)
        monkeypatch.setattr(avatar, "can_see", lambda m, s=sees: s)
        out = avatar.edit({"base": "mei", "name": "M"}, "match it", model=model, image=pic)
        user = calls[0]["messages"][1]
        assert ("images" in user) is attached and out["saw_image"] is attached
        assert attached or "couldn't see your picture" in out["reply"]
    with pytest.raises(ValueError, match="one piece"):
        avatar.edit({"base": "mei", "name": "M"}, "match it", model="m", image="not base64!!")

def test_llm_passes_think_through_to_ollama_only_when_asked(monkeypatch):
    sent = []
    class Client:
        def __init__(self, host=None): pass
        async def chat(self, **kw):
            sent.append(kw)
            return ollama.ChatResponse(message=ollama.Message(role="assistant", content="{}"))
    monkeypatch.setattr(ollama, "AsyncClient", Client)
    asyncio.run(llm.chat([{"role": "user", "content": "hi"}], model="m", stoppable=False, think=False))
    asyncio.run(llm.chat([{"role": "user", "content": "hi"}], model="m", stoppable=False))
    assert sent[0]["think"] is False and "think" not in sent[1]

def test_code_fixes_the_drawing_slips_it_safely_can(monkeypatch):
    fake_model(monkeypatch, {"refused": False, "reply": "Horns!", "change": {"maps": [
        {"layer": "head", "at": [-3, 12], "rows": ["h...h", "hh.hh", "h"], "pal": {"h": "#c0392b"}}]}})
    m = avatar.edit({"base": "mei", "name": "M"}, "devil horns", model="m")["change"]["maps"][0]
    assert m["pal"] == {"a": "#c0392b"} and m["rows"] == ["a...a", "aa.aa", "a...."]   # a free letter, rows padded

# ---------- cyborg sprites: one switch for the Cyborgs row under Who visits (#151) ----------

def test_the_cyborg_row_s_switch_hands_every_page_the_cyborg_crew(client):
    assert "cyborgs" not in avatars.config() and not settings.cyborgs()
    assert "/settings/cyborgs" not in client.get("/settings").text                  # it lives in the studio now
    assert "data-cyborgs" not in client.get("/avatars").text
    r = client.post("/avatars/cyborgs", json={"on": True})
    assert r.status_code == 200 and r.json() == {"ok": True, "on": True} and settings.cyborgs()
    cfg = avatars.config()
    assert len(cfg["cyborgs"]) == 12 and cfg["cyborgs"]["kenji-borg"]["base"] == "kenji" and "off" not in cfg
    assert '"kenji-borg"' in client.get("/").text and "data-cyborgs" in client.get("/avatars").text
    assert "cyborgs visiting" in client.get("/settings").text
    assert client.post("/avatars/cyborgs", json={"on": False}).json() == {"ok": True, "on": False}
    assert not settings.cyborgs() and "cyborgs" not in avatars.config() and "cyborgs" not in settings.saved()

def test_the_cyborg_switch_refuses_anything_but_on_or_off(client):
    assert client.post("/avatars/cyborgs", json={"on": "yes"}).status_code == 400
    assert client.post("/avatars/cyborgs", content=b"nope", headers={"Content-Type": "application/json"}).status_code == 400
    assert not settings.cyborgs()

def test_an_avatar_saved_under_a_cyborg_s_name_wins(client):
    settings.set_cyborgs(True)
    avatars.save("pip-borg", EXAMPLE)
    cfg = avatars.config()
    assert "pip-borg" not in cfg["cyborgs"] and cfg["add"]["pip-borg"] == EXAMPLE and len(cfg["cyborgs"]) == 11
