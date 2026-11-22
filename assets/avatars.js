/* The avatar studio (/avatars): build a researcher from pieces, or ask a model to, and pick who visits.
 *
 * An avatar is a sprite spec (SPRITES.md): a built-in researcher to start from plus overrides. Every piece on the
 * right is shown as a tiny render of *your* avatar wearing it (renderSprite), so picking is seeing. The preview on
 * the left plays the avatar through a few bits on a little floor. "Ask a model" sends the spec and a request (and
 * an image, if the model can see) to the designer (agents/avatar.py); its reply is checked here with validateSprite,
 * sent back with the problems for up to two repair rounds, and whatever still breaks a rule is dropped field by
 * field, so the avatar on screen always follows the rules. Saving writes data/sprites.json through the UI, and the
 * Who visits panel switches any researcher on or off. Nothing here runs a model or touches the loop.
 */
(() => {
"use strict";
const R = window.Researchers, root = document.getElementById("studio");
if (!R || !root || !R.renderSprite) return;

// ---------- small helpers ----------

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(kid));
  return el;
}
async function post(url, body) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `The studio couldn't reach the app (${res.status}).`);
  return data;
}
const clone = o => JSON.parse(JSON.stringify(o));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const keyFrom = text => String(text || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32).replace(/-+$/, "");
const KEY = /^[a-z0-9][a-z0-9-]{0,31}$/;
const pick = list => list[Math.floor(Math.random() * list.length)];
const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;

// ---------- the pieces ----------

const NAMES = {
  style: { short: "Short", tousled: "Tousled", receding: "Receding", bob: "Bob", bigcurly: "Big curls", long: "Long", crop: "Crop", neat: "Side part", spiky: "Spiky" },
  head: { null: "None", hijab: "Hijab", cap: "Cap", beanie: "Beanie", hardhat: "Hard hat", deerstalker: "Deerstalker", propeller: "Propeller" },
  outfit: { labcoat: "Lab coat", suit: "Suit", hoodie: "Hoodie", sweater: "Sweater", cardigan: "Cardigan", vest: "Hi-vis", overalls: "Overalls",
            jacket: "Jacket", abaya: "Abaya", trench: "Trench", tshirt: "T-shirt" },
  legs: { pants: "Trousers", skirt: "Skirt" },
  face: { null: "None", round: "Round", browline: "Browline", halfmoon: "Half-moon" },
  facial: { null: "None", walrus: "Walrus", mustache: "Mustache" },
  brow: { flat: "Calm", raise: "Curious", stern: "Stern", worry: "Worried", skeptic: "Skeptical" },
  mouth: { none: "Neutral", smile: "Smile", grin: "Grin", o: "Surprised", flat: "Unimpressed", smirk: "Smirk" },
  prop: { null: "Nothing", tablet: "Tablet", laptop: "Laptop", phone: "Phone", book: "Book", briefcase: "Briefcase", camera: "Camera",
          magnifier: "Magnifier", mug: "Mug" },
  worker: { assistant: "Young crew", pi: "Tenured crew" },
};
const FLAGS = { ahoge: "Stray strand", bushy: "Bushy brows", lash: "Lashes", lids: "Sleepy lids", freckles: "Freckles", grin: "Fang grin",
                rolled: "Rolled sleeves", pencil: "Pencil on ear" };
const HAIR = ["#17110e", "#2e1f17", "#4b3021", "#6e4527", "#93602f", "#c08d4a", "#e1c27f", "#c9572d", "#a3a3a3", "#e4e4e4",
              "#f2f0f7", "#ff8fc7", "#b197fc", "#74c0fc", "#63e6be", "#e03131"];
const CLOTH = ["#f4f4f0", "#e8e4da", "#2b2b30", "#3a3a40", "#7d8691", "#22346b", "#3d6a9e", "#74c0fc", "#218c74", "#5f7f6b",
               "#b5d8c6", "#8a5a3c", "#cc8e35", "#f1c40f", "#e67e22", "#c0392b", "#e84393", "#6e5a8a", "#b197fc", "#ffd8a8"];
// Bits that make good favourites: the vibe tab weights these up.
const VIBES = ["read", "search", "notes", "dig", "typing", "coffee", "telescope", "eureka", "beaker", "rocket", "microscope", "chalkboard",
               "corkboard", "chart", "stamp", "duck", "globe", "juggle", "pizza", "dance", "cat", "bird", "flower", "sneeze", "plane", "sleep"];
const SHOW = [["rest", 2.4], ["walk", 2.4], ["wave", 1.5], ["quirk", 0], ["read", 3], ["coffee", 3.2], ["eureka", 1.8], ["typing", 3]];

// Which colors a tab offers depends on what the avatar wears.
const colorsFor = (tab, look) => ({
  body: [],
  hair: look.head === "hijab" ? [] : [["hair", "Hair", HAIR]],
  face: look.face ? [["frame", "Frames", CLOTH]] : [],
  clothes: [["top", "Top", CLOTH], ["shirt", "Shirt", CLOTH], ["pants", "Trousers", CLOTH], ["shoes", "Shoes", CLOTH]]
    .concat(look.legs === "skirt" ? [["skirt", "Skirt", CLOTH]] : [])
    .concat(look.outfit === "suit" || look.tie ? [["tie", "Tie", CLOTH]] : [])
    .concat(look.outfit === "labcoat" ? [["bow", "Bow tie", CLOTH]] : []),
  hat: ({ cap: [["cap", "Cap", CLOTH]], beanie: [["beanie", "Beanie", CLOTH], ["pom", "Pom-pom", CLOTH]], hijab: [["hijab", "Hijab", CLOTH]] })[look.head] || [],
  prop: look.pack ? [["pack", "Backpack", CLOTH]] : [],
}[tab] || []);

const TABS = [
  { id: "body", label: "Body", groups: [{ title: "Start from", kind: "base" }, { title: "Skin", kind: "skin" }, { title: "Crew", kind: "enum", field: "worker", chips: true }] },
  { id: "hair", label: "Hair", groups: [{ title: "Style", kind: "enum", field: "style", crop: "head" }, { title: "Extras", kind: "flags", fields: ["ahoge"] }] },
  { id: "face", label: "Face", groups: [{ title: "Glasses", kind: "enum", field: "face", crop: "head" }, { title: "Mustache", kind: "enum", field: "facial", crop: "head" },
      { title: "Resting brows", kind: "enum", field: "brow", crop: "head" }, { title: "Resting mouth", kind: "enum", field: "mouth", crop: "head" },
      { title: "Details", kind: "flags", fields: ["lash", "lids", "freckles", "grin", "bushy"] }] },
  { id: "clothes", label: "Clothes", groups: [{ title: "Outfit", kind: "enum", field: "outfit", crop: "bust" }, { title: "Legs", kind: "enum", field: "legs", crop: "full" },
      { title: "Details", kind: "flags", fields: ["rolled", "pencil"] }] },
  { id: "hat", label: "Hat", groups: [{ title: "Headwear", kind: "enum", field: "head", crop: "head" }] },
  { id: "prop", label: "Prop", groups: [{ title: "Signature prop", kind: "enum", field: "prop", crop: "full" }] },
  { id: "vibe", label: "Vibe", groups: [{ title: "Favourite things to do", kind: "vibes" }, { title: "Their quirk", kind: "quirk" }] },
  { id: "extras", label: "Drawn", groups: [{ title: "Drawn by a model", kind: "extras" }] },
];

// ---------- state ----------

const params = new URLSearchParams(location.search);
const saved = (window.RESEARCHER_SPRITES && window.RESEARCHER_SPRITES.add) || {};
const state = {
  key: "", original: "", spec: null, history: [], future: [], tab: "body", face: 1, seed: 11,
  show: 0, t: 0, auto: true, dirty: false, model: "", models: [], image: null, busy: false,
};
const editKey = params.get("edit");
if (editKey && saved[editKey]) { state.spec = clone(saved[editKey]); state.key = state.original = editKey; }
else {
  const base = R.BUILT_IN.includes(params.get("base")) ? params.get("base") : "mei";
  state.spec = { base, name: `${R.ROSTER[base].name.replace(/^(Dr\.|Prof\.|Det\.|Intern) /, "")} 2` };
  state.key = root.dataset.next || "custom-sprite-1";
}
const look = () => { const v = R.validateSprite(state.spec); return v.ok ? v.sprite : Object.assign({}, R.ROSTER[state.spec.base] || {}, state.spec); };
// How it looks now, for the model: one value per field (a built-in's per-visit color lists only invite copying).
const lookNow = () => Object.fromEntries(Object.entries(look()).filter(([k]) => k !== "fav")
  .map(([k, v]) => [k, Array.isArray(v) && k !== "skin" && k !== "maps" ? v[0] : v]));
const valueOf = (spec, field) => {
  const v = field in spec ? spec[field] : (R.ROSTER[spec.base] || {})[field];
  return Array.isArray(v) ? v[0] : v ?? null;
};

function change(next, opts = {}) {             // every edit goes through here, so undo always works
  if (same(next, state.spec)) return;
  state.history.push(clone(state.spec)); if (state.history.length > 60) state.history.shift();
  state.future = [];
  state.spec = next; state.dirty = true;
  if (opts.show) { state.show = SHOW.findIndex(s => s[0] === opts.show); state.t = 0; }
  render();
}
const set = (field, value) => {
  const next = clone(state.spec);
  next[field] = value;
  return next;
};

// ---------- drawing tiles ----------

const scratch = document.createElement("canvas");
function snapshot(spec, opts = {}) {             // a still of the avatar, cropped to its pixels
  const res = R.renderSprite(scratch, spec, Object.assign({ seed: state.seed, action: "rest", t: 0.5 }, opts));
  if (!res.ok) return null;
  const g = scratch.getContext("2d"), { width: W, height: H } = scratch, d = g.getImageData(0, 0, W, H).data;
  let x0 = W, y0 = H, x1 = 0, y1 = 0;
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) if (d[(y * W + x) * 4 + 3]) { x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y); }
  return x1 < x0 ? null : { canvas: scratch, x0, y0, x1, y1 };
}
function tile(spec, crop, scale) {               // a canvas showing the avatar (or its head, or down to the waist)
  if (crop === "head" || crop === "bust") { spec = clone(spec); spec.prop = null; delete spec.propMap; }   // nothing in front of the face
  const c = h("canvas", { class: "px", "aria-hidden": "true" }), snap = snapshot(spec);
  const box = { head: [24, 24], bust: [30, 40], full: [36, 62] }[crop || "full"];
  c.width = box[0] * scale; c.height = box[1] * scale;
  c.style.width = c.width / 2 + "px"; c.style.height = c.height / 2 + "px";   // drawn at double density, crisp on any screen
  if (!snap) return c;
  const g = c.getContext("2d"); g.imageSmoothingEnabled = false;
  const cx = Math.round((snap.x0 + snap.x1) / 2), w = box[0];
  const sx = cx - Math.floor(w / 2), sy = crop === "full" ? snap.y1 + 1 - box[1] : snap.y0 - 1;   // feet down, or top down
  g.drawImage(snap.canvas, sx, sy, w, box[1], 0, 0, w * scale, box[1] * scale);
  return c;
}

// ---------- the layout ----------

const stageCanvas = h("canvas", { class: "px stage-canvas", role: "img", "aria-label": "Your avatar, moving" });
const nowLabel = h("span", { class: "now-doing" });
const status = h("p", { class: "av-status", "aria-live": "polite" });
const nameIn = h("input", { type: "text", maxlength: 24, "aria-label": "Name", oninput: e => { const n = clone(state.spec); n.name = e.target.value; state.spec = n; state.dirty = true; keyHint(); check(); } });
const keyIn = h("input", { type: "text", maxlength: 32, spellcheck: "false", "aria-label": "Saved as", oninput: () => keyHint() });
const keyNote = h("span", { class: "hint" });
const undoBtn = h("button", { type: "button", onclick: () => undo(), title: "Undo" }, "Undo");
const redoBtn = h("button", { type: "button", onclick: () => redo(), title: "Redo" }, "Redo");
const saveBtn = h("button", { type: "button", class: "primary", onclick: () => save() }, "Save avatar");
const tabBar = h("div", { class: "av-tabs", role: "tablist", "aria-label": "Pieces" });
const panel = h("div", { class: "av-panel", role: "tabpanel" });
const actions = h("div", { class: "av-acts" });
const castHost = h("div", { class: "av-cast" });
const log = h("div", { class: "av-log", "aria-live": "polite" });
const modelSel = h("select", { "aria-label": "Model", onchange: e => { state.model = e.target.value; flag(); } });
const askIn = h("textarea", { rows: 2, maxlength: 1000, placeholder: "Give them cat ears and a purple scarf", "aria-label": "What to change" });
const fileIn = h("input", { type: "file", accept: "image/*", hidden: true, onchange: e => attach(e.target.files[0]) });
const attachBtn = h("button", { type: "button", class: "attach", title: "Attach a picture to go by", onclick: () => fileIn.click() }, "📎 Picture");
const thumb = h("span", { class: "av-thumb", hidden: true });
const flagEl = h("p", { class: "av-flag", hidden: true, role: "note" });
const askBtn = h("button", { class: "primary" }, "Ask");

function layout() {
  for (const [i, [name]] of SHOW.entries())
    actions.append(h("button", { type: "button", "data-i": i, onclick: () => { state.show = i; state.t = 0; state.auto = false; markAct(); } }, name === "rest" ? "idle" : name));
  actions.append(h("button", { type: "button", class: "auto", onclick: () => { state.auto = !state.auto; markAct(); }, title: "Play them all in turn" }, "↻ all"));
  for (const t of TABS)
    tabBar.append(h("button", { type: "button", role: "tab", id: `tab-${t.id}`, "aria-controls": "av-panel", onclick: () => { state.tab = t.id; render(); } }, t.label));
  panel.id = "av-panel";
  const suggestions = ["Give them cat ears", "A cozy wizard vibe", "Make the outfit forest green", "Grumpy but lovable", "Match my picture"];
  const form = h("form", { class: "av-ask", onsubmit: e => { e.preventDefault(); ask(); } },
    h("div", { class: "row" }, h("label", { class: "model" }, "Model ", modelSel), attachBtn, fileIn, thumb),
    flagEl,
    h("div", { class: "row grow" }, askIn, askBtn));
  askIn.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(); } });
  root.replaceChildren(
    h("section", { class: "av-studio" },
      h("div", { class: "av-left" },
        h("div", { class: "av-room" }, stageCanvas, nowLabel),
        actions,
        h("div", { class: "av-fields" },
          h("label", {}, "Name", nameIn),
          h("label", {}, "Saved as", keyIn, keyNote)),
        h("div", { class: "av-buttons" }, saveBtn, undoBtn, redoBtn,
          h("button", { type: "button", onclick: () => randomize(), title: "Dress them at random" }, "🎲 Surprise me"),
          h("button", { type: "button", onclick: () => { state.face *= -1; }, title: "Turn around" }, "⇄ Turn"),
          h("button", { type: "button", onclick: () => { state.seed = 1 + Math.floor(Math.random() * 9999); render(); }, title: "Height, build and colours vary a little per visit" }, "Another visit")),
        status),
      h("div", { class: "av-right" }, tabBar, panel)),
    h("h2", { class: "group" }, h("span", { text: "Ask a model" })),
    h("p", { class: "hint" }, "Say what you want, or attach a picture. The model picks pieces that exist and draws small extras only when nothing fits; every change is checked against the rules (SPRITES.md) before it lands, and Undo takes it back. Small models follow words well; bigger ones read pictures better."),
    h("div", { class: "av-chat" }, log, h("div", { class: "suggest" },
      suggestions.map(s => h("button", { type: "button", onclick: () => { if (s.startsWith("Match")) fileIn.click(); else { askIn.value = s; ask(); } } }, s))), form),
    h("h2", { class: "group" }, h("span", { text: "Who visits" })),
    h("p", { class: "hint" }, "Switch anyone off and they stay home. A crew always keeps someone: if all of one crew is off, they all still come."),
    castHost);
}

// ---------- rendering the editor ----------

function render() {
  nameIn.value = state.spec.name || "";
  if (document.activeElement !== keyIn && !keyIn.value) keyIn.value = state.key;
  undoBtn.disabled = !state.history.length; redoBtn.disabled = !state.future.length;
  for (const b of tabBar.children) b.setAttribute("aria-selected", String(b.id === `tab-${state.tab}`));
  const tab = TABS.find(t => t.id === state.tab), lk = look();
  panel.replaceChildren(...tab.groups.map(g => group(g, lk)), ...swatchRows(tab.id, lk));
  keyHint(); check(); markAct();
}

function group(g, lk) {
  const body = h("div", { class: "av-group" }, h("h3", { text: g.title }));
  const grid = h("div", { class: g.chips || g.kind === "flags" || g.kind === "vibes" ? "chips" : "tiles" });
  body.append(grid);
  if (g.kind === "base") {
    for (const k of R.BUILT_IN) {
      const on = state.spec.base === k;
      grid.append(h("button", { type: "button", class: "tile" + (on ? " on" : ""), "aria-pressed": String(on), title: `Start from ${R.ROSTER[k].name}`,
        onclick: () => { if (!on) change(rebase(k), { show: "wave" }); } }, tile({ base: k, name: "x" }, "full", 4), h("span", { text: R.ROSTER[k].name.replace(/^(Dr\.|Prof\.|Det\.|Intern) /, "") })));
    }
  } else if (g.kind === "skin") {
    const cur = (state.spec.skin || R.ROSTER[state.spec.base].skin || [0])[0];
    R.SKIN.forEach((c, i) => grid.append(h("button", { type: "button", class: "swatch" + (cur === i && state.spec.skin ? " on" : ""), style: `--c:${c}`, title: `Skin tone ${i + 1}`,
      "aria-label": `Skin tone ${i + 1}`, onclick: () => change(set("skin", [i])) })));
    grid.className = "swatches";
    grid.append(h("button", { type: "button", class: "chip" + (state.spec.skin ? "" : " on"), onclick: () => { const n = clone(state.spec); delete n.skin; change(n); } }, "Like the original"));
  } else if (g.kind === "enum") {
    const cur = valueOf(state.spec, g.field);
    for (const v of R.PARTS[g.field]) {
      const on = cur === v, label = (NAMES[g.field] || {})[String(v)] || String(v);
      const pickIt = () => { if (!on) change(set(g.field, v), { show: g.field === "prop" ? "walk" : g.field === "head" ? "eureka" : null }); };
      if (g.chips) { grid.append(h("button", { type: "button", class: "chip" + (on ? " on" : ""), "aria-pressed": String(on), onclick: pickIt }, label)); continue; }
      const preview = set(g.field, v);
      if (g.field === "brow" || g.field === "mouth") preview[g.field] = v;
      grid.append(h("button", { type: "button", class: "tile" + (on ? " on" : ""), "aria-pressed": String(on), title: label, onclick: pickIt },
        tile(preview, g.crop, g.crop === "head" ? 7 : 4), h("span", { text: label })));
    }
  } else if (g.kind === "flags") {
    for (const f of g.fields) {
      const on = !!valueOf(state.spec, f);
      grid.append(h("button", { type: "button", class: "chip" + (on ? " on" : ""), "aria-pressed": String(on), onclick: () => change(set(f, !on)) }, FLAGS[f]));
    }
  } else if (g.kind === "vibes") {
    const fav = state.spec.fav || {};
    body.append(h("p", { class: "hint", text: "Pick a few: they'll do these more often." }));
    for (const v of VIBES) {
      const on = (fav[v] || 0) >= 2;
      grid.append(h("button", { type: "button", class: "chip" + (on ? " on" : ""), "aria-pressed": String(on), onclick: () => {
        const next = clone(state.spec), f = Object.assign({}, next.fav || R.ROSTER[next.base].fav || {});
        if (on) delete f[v]; else f[v] = 2.5;
        next.fav = f; change(next, { show: R.ACTIONS[v] ? null : null });
      } }, v));
    }
  } else if (g.kind === "quirk") {
    const own = !!state.spec.quirk;
    body.append(h("p", { class: "hint", text: own ? "Their own quirk, drawn by a model." : `Like ${R.ROSTER[state.spec.base].name}'s: every researcher has a habit between bits. Ask a model for a new one.` }),
      h("div", { class: "chips" },
        h("button", { type: "button", class: "chip", onclick: () => { state.show = SHOW.findIndex(s => s[0] === "quirk"); state.t = 0; state.auto = false; markAct(); } }, "▶ Play it"),
        own ? h("button", { type: "button", class: "chip", onclick: () => { const n = clone(state.spec); delete n.quirk; change(n); } }, "Use the original") : null));
    grid.remove();
  } else if (g.kind === "extras") {
    grid.remove();
    const maps = state.spec.maps || [];
    if (!maps.length && !state.spec.propMap && !state.spec.quirk)
      body.append(h("p", { class: "hint", text: "Nothing yet. Ask a model for something no piece has (cat ears, a scarf, a tail) and it draws it here." }));
    maps.forEach((m, i) => body.append(h("div", { class: "extra" }, mini(m), h("span", { text: `${m.layer === "back" ? "Behind" : m.layer === "body" ? "On the clothes" : m.layer === "behindHead" ? "Behind the head" : "On the head"}${m.frames ? ` · moves (${m.frames.length} frames)` : ""}` }),
      h("button", { type: "button", onclick: () => { const n = clone(state.spec); n.maps.splice(i, 1); if (!n.maps.length) delete n.maps; change(n); } }, "Remove"))));
    if (state.spec.propMap) body.append(h("div", { class: "extra" }, mini(state.spec.propMap), h("span", { text: "Their own prop" }),
      h("button", { type: "button", onclick: () => { const n = clone(state.spec); delete n.propMap; change(n); } }, "Remove")));
    const json = h("textarea", { class: "av-json", rows: 10, spellcheck: "false", "aria-label": "The spec as JSON" });
    json.value = JSON.stringify(state.spec, null, 2);
    body.append(h("details", {}, h("summary", { text: "The spec (JSON)" }), json,
      h("button", { type: "button", onclick: () => {
        try { const n = JSON.parse(json.value); const v = R.validateSprite(n); if (!v.ok) throw new Error(v.errors.join("; ")); change(n); }
        catch (e) { note(e.message, true); }
      } }, "Apply")));
  }
  return body;
}

function mini(m) {                               // a pinned map drawn on its own, so you can see what it is
  const rows = m.frames ? m.frames[0] : m.rows, c = h("canvas", { class: "px mini", "aria-hidden": "true" }), s = 4;
  c.width = rows[0].length * s; c.height = rows.length * s; c.style.width = c.width / 2 + "px";
  const g = c.getContext("2d"), lk = look(), slot = { s: "#e8b38a", S: "#c99a75", h: lk.hair, H: lk.hair, l: lk.hair, t: lk.top, T: lk.top, c: lk.shirt, p: lk.pants, P: lk.pants, o: lk.shoes, k: "#1d1b22", w: "#ffffff" };
  rows.forEach((row, y) => [...row].forEach((ch, x) => {
    if (ch === ".") return;
    const col = (m.pal && m.pal[ch]) || slot[ch];
    g.fillStyle = Array.isArray(col) ? col[0] : col || "#1d1b22"; g.fillRect(x * s, y * s, s, s);
  }));
  return c;
}

function swatchRows(tab, lk) {
  return colorsFor(tab, lk).map(([field, title, palette]) => {
    const cur = valueOf(state.spec, field);
    const row = h("div", { class: "swatches" });
    for (const c of palette) row.append(h("button", { type: "button", class: "swatch" + (cur === c ? " on" : ""), style: `--c:${c}`, title: c, "aria-label": `${title} ${c}`,
      onclick: () => change(set(field, c)) }));
    const own = h("input", { type: "color", value: /^#[0-9a-f]{6}$/i.test(cur || "") ? cur : "#888888", "aria-label": `${title}: any colour`, title: "Any colour",
      onchange: e => change(set(field, e.target.value.toLowerCase())) });
    row.append(h("label", { class: "swatch own" }, own));
    return h("div", { class: "av-group" }, h("h3", { text: `${title} colour` }), row);
  });
}

function rebase(k) {                             // a new start keeps the name and anything drawn
  const next = { base: k, name: state.spec.name };
  for (const f of ["maps", "propMap", "quirk", "fav"]) if (state.spec[f]) next[f] = clone(state.spec[f]);
  return next;
}

function randomize() {
  const P = R.PARTS, next = { base: state.spec.base, name: state.spec.name };
  for (const f of ["maps", "propMap", "quirk", "fav"]) if (state.spec[f]) next[f] = clone(state.spec[f]);
  next.style = pick(P.style); next.outfit = pick(P.outfit); next.legs = Math.random() < 0.25 ? "skirt" : "pants";
  next.head = Math.random() < 0.5 ? null : pick(P.head.filter(Boolean));
  next.face = Math.random() < 0.6 ? null : pick(P.face.filter(Boolean));
  next.facial = Math.random() < 0.85 ? null : pick(P.facial.filter(Boolean));
  next.prop = pick(P.prop); next.brow = pick(P.brow); next.mouth = pick(["none", "none", "smile", "smirk", "grin"]);
  next.skin = [Math.floor(Math.random() * R.SKIN.length)];
  next.hair = pick(HAIR); for (const f of ["top", "shirt", "pants", "shoes", "cap", "beanie", "frame"]) next[f] = pick(CLOTH);
  for (const f of ["ahoge", "lash", "freckles", "rolled", "pencil"]) next[f] = Math.random() < 0.2;
  change(next, { show: "wave" });
}

function undo() { if (!state.history.length) return; state.future.push(clone(state.spec)); state.spec = state.history.pop(); state.dirty = true; render(); }
function redo() { if (!state.future.length) return; state.history.push(clone(state.spec)); state.spec = state.future.pop(); state.dirty = true; render(); }

function keyHint() {
  const k = keyIn.value.trim();
  const taken = k !== state.original && (R.BUILT_IN.includes(k) || k in saved);
  keyNote.textContent = !KEY.test(k) ? "lowercase letters, digits and dashes" : taken ? "that name is taken" : state.original && k !== state.original ? `renames ${state.original}` : "";
  keyNote.classList.toggle("bad", !KEY.test(k) || taken);
}
function check() {
  const v = R.validateSprite(state.spec);
  status.className = "av-status" + (v.ok ? " ok" : " bad");
  status.textContent = v.ok ? (state.dirty ? "Follows the rules. Not saved yet." : "Follows the rules.") : v.errors.slice(0, 3).join(" · ");
  saveBtn.disabled = !v.ok;
  return v;
}
function note(text, bad) { status.className = "av-status" + (bad ? " bad" : " ok"); status.textContent = text; }

async function save() {
  const v = check(), key = keyIn.value.trim();
  if (!v.ok) return;
  try {
    await post("/avatars/save", { key, spec: state.spec, replace: state.original });
    saved[key] = clone(state.spec); if (state.original && state.original !== key) delete saved[state.original];
    R.registerSprite(key, state.spec); R.setSpriteEnabled(key, true);
    state.key = state.original = key; state.dirty = false;
    history.replaceState(null, "", `/avatars?edit=${encodeURIComponent(key)}`);
    note(`Saved as ${key}. ${state.spec.name} will drop by with the ${look().worker === "pi" ? "tenured" : "young"} crew.`);
    state.show = SHOW.findIndex(s => s[0] === "wave"); state.t = 0;
    cast();
  } catch (e) { note(e.message, true); }
}
addEventListener("beforeunload", e => { if (state.dirty) e.preventDefault(); });

// ---------- the live preview ----------

function markAct() {
  for (const b of actions.children) b.classList.toggle("on", b.classList.contains("auto") ? state.auto : +b.dataset.i === state.show);
}
let last = performance.now();
function frame(now) {
  const dt = Math.min(0.05, (now - last) / 1000); last = now;
  const [action, len] = SHOW[state.show], dur = len || ((look().quirk && look().quirk.dur) || 2.6);
  state.t += dt;
  if (state.t > dur + 0.6) { state.t = 0; if (state.auto) state.show = (state.show + 1) % SHOW.length; markAct(); }
  R.renderSprite(stageCanvas, state.spec, { action, t: Math.min(state.t, dur), scale: 4, seed: state.seed, face: state.face, width: 76, height: 66, effects: true });
  nowLabel.textContent = action === "rest" ? "" : action;
  if (pending) R.renderSprite(pending, state.spec, { action: "typing", t: 0.3 + (now / 1000) % 2.5, scale: 2, seed: state.seed, width: 40, height: 40 });
  if (!reduce) requestAnimationFrame(frame);
}

// ---------- ask a model ----------

let pending = null;
function bubble(who, text, extra) {
  const b = h("div", { class: `msg ${who}` }, extra || null, h("p", { text }));
  log.append(b); log.scrollTop = log.scrollHeight;
  return b;
}
function flag() {
  const m = state.models.find(x => x.name === state.model);
  const vision = m ? m.vision : null;
  attachBtn.classList.toggle("sees", vision === true);
  attachBtn.title = vision === true ? "This model can see pictures" : "Attach a picture to go by";
  flagEl.hidden = !state.image || vision === true;
  flagEl.textContent = vision === false ? "⚠ This model can't see pictures. It will go by your words; pick a model marked “sees pictures” to use it."
    : "⚠ Not sure this model can see pictures. It may go by your words alone.";
}
function attach(file) {
  if (!file) return;
  const img = new Image(), url = URL.createObjectURL(file);
  img.onload = () => {                           // shrunk to 384 px: plenty for colours and style, quick to send
    const k = Math.min(1, 384 / Math.max(img.width, img.height)), c = document.createElement("canvas");
    c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
    c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
    state.image = c.toDataURL("image/png");
    thumb.replaceChildren(h("img", { src: state.image, alt: "Your picture" }), h("button", { type: "button", "aria-label": "Remove the picture", onclick: () => { state.image = null; thumb.hidden = true; flag(); } }, "×"));
    thumb.hidden = false; URL.revokeObjectURL(url); flag();
  };
  img.onerror = () => note("That file isn't a picture the browser can read.", true);
  img.src = url; fileIn.value = "";
}

// Applies each field of a reply that still breaks a rule on its own, if that field alone keeps the spec valid.
function salvage(current, proposed) {
  let acc = clone(current); const kept = [], dropped = [];
  for (const [k, v] of Object.entries(proposed)) {
    if (k === "base" || same(acc[k], v)) continue;
    const trial = Object.assign(clone(acc), { [k]: v });
    if (R.validateSprite(trial).ok) { acc = trial; kept.push(k); } else dropped.push(k);
  }
  return { spec: acc, kept, dropped };
}

async function ask() {
  const message = askIn.value.trim();
  if (state.busy || (!message && !state.image)) return;
  state.busy = true; askBtn.disabled = true;
  bubble("me", message || "(a picture)", state.image ? h("img", { src: state.image, alt: "" }) : null);
  askIn.value = "";
  pending = h("canvas", { class: "px", "aria-hidden": "true" }); pending.style.width = "40px";
  const wait = bubble("them pending", "Sketching…", pending);
  const before = clone(state.spec), image = state.image;
  const catalog = Object.assign({}, R.PARTS, { flags: R.FLAG_KEYS, colors: R.COLOR_KEYS });   // what the model may pick from
  let body = { message, spec: before, look: lookNow(), model: state.model, image, catalog }, reply = null;
  try {
    for (let round = 0; round < 3; round++) {
      reply = await post("/avatars/ask", body);
      if (reply.refused) break;
      const v = R.validateSprite(reply.spec);
      if (v.ok) break;
      if (round === 2) break;
      wait.querySelector("p").textContent = `Fixing ${v.errors.length} thing${v.errors.length === 1 ? "" : "s"} the rules caught…`;
      body = { message, spec: reply.spec, look: lookNow(), model: state.model, image, catalog, errors: v.errors };
    }
    wait.remove(); pending = null;
    if (reply.refused) { bubble("them refused", reply.reply); return; }
    let next = reply.spec, text = reply.reply || "Done.", extra = "";
    if (!R.validateSprite(next).ok) {
      const s = salvage(before, next), drawn = s.dropped.some(k => k === "maps" || k === "propMap");
      next = s.spec;
      if (!s.kept.length) text = drawn ? "That one needed drawing, and the drawing kept breaking the rules, so I left it out. Bigger models draw better; pieces always work."
        : "I couldn't make that follow the rules, so nothing changed.";
      else extra = ` (Left out ${s.dropped.join(", ")}: it broke the rules.)`;
    }
    next.base = next.base || before.base;
    if (same(next, before)) { bubble("them", text); return; }
    change(next, { show: "wave" });
    const msg = bubble("them", text + extra);
    msg.append(h("button", { type: "button", class: "link", onclick: e => { if (same(state.spec, next)) { undo(); e.target.textContent = "Undone"; e.target.disabled = true; } } }, "Undo"));
    state.image = null; thumb.hidden = true; flag();
  } catch (e) {
    wait.remove(); pending = null;
    bubble("them refused", e.message);
  } finally { state.busy = false; askBtn.disabled = false; }
}

async function models() {
  try {
    const data = await (await fetch("/avatars/models")).json();
    state.models = data.models || [];
  } catch (e) { state.models = []; }
  if (!state.models.length) { modelSel.append(h("option", { value: "" }, "No model found: start Ollama or add a provider")); askBtn.disabled = true; return; }
  for (const m of state.models) modelSel.append(h("option", { value: m.name }, m.name + (m.vision ? " · sees pictures" : "")));
  state.model = state.models[0].name; flag();
}

// ---------- who visits ----------

function cast() {
  const off = new Set((window.RESEARCHER_SPRITES && window.RESEARCHER_SPRITES.off) || []);
  castHost.replaceChildren();
  for (const [worker, title] of [["assistant", "Young crew · the Research Assistant's"], ["pi", "Tenured crew · the Principal Investigator's"]]) {
    const row = h("div", { class: "crew-row" });
    for (const k of R.CAST.filter(k => R.ROSTER[k].worker === worker && !k.startsWith("__"))) {
      const custom = !R.BUILT_IN.includes(k), on = !off.has(k);
      const sw = h("input", { type: "checkbox", role: "switch", checked: on, "aria-label": `${R.ROSTER[k].name} visits`, onchange: async e => {
        try { await post(`/avatars/${encodeURIComponent(k)}/${e.target.checked ? "on" : "off"}`, {}); }
        catch (err) { e.target.checked = !e.target.checked; return note(err.message, true); }
        if (e.target.checked) off.delete(k); else off.add(k);
        (window.RESEARCHER_SPRITES = window.RESEARCHER_SPRITES || {}).off = [...off];
        R.setSpriteEnabled(k, e.target.checked); card.classList.toggle("off", !e.target.checked);
      } });
      const go = (url) => { if (!state.dirty || confirm("Leave without saving this avatar?")) { state.dirty = false; location.href = url; } };
      const card = h("div", { class: "visitor" + (on ? "" : " off") + (k === state.original ? " editing" : "") },
        tile(k === state.original ? state.spec : custom ? saved[k] : { base: k, name: "x" }, "full", 3),
        h("strong", { text: R.ROSTER[k].name }), h("small", { text: custom ? k : "built-in" }),
        h("label", { class: "switch" }, sw, on ? "Visits" : "Stays home"),
        h("div", { class: "visitor-acts" },
          custom ? h("button", { type: "button", onclick: () => go(`/avatars?edit=${encodeURIComponent(k)}`) }, "Edit")
                 : h("button", { type: "button", onclick: () => go(`/avatars?base=${k}`) }, "Remix"),
          custom ? h("button", { type: "button", class: "danger", onclick: async () => {
            if (!confirm(`Delete ${R.ROSTER[k].name} (${k})? This can't be undone.`)) return;
            try { await post(`/avatars/${encodeURIComponent(k)}/delete`, {}); state.dirty = false; location.href = k === state.original ? "/avatars" : location.href; }
            catch (err) { note(err.message, true); }
          } }, "Delete") : null));
      sw.addEventListener("change", () => { sw.parentNode.lastChild.textContent = sw.checked ? "Visits" : "Stays home"; });
      row.append(card);
    }
    castHost.append(h("h3", { text: title }), row);
  }
}

layout(); render(); cast(); models();
if (reduce) frame(performance.now()); else requestAnimationFrame(frame);
})();
