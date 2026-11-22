/* Pixel researchers: a cast of small outlined researchers who drop in on the dashboard while missions
 * are in progress. Each in-progress card, and the green pill of the agent working right now, gets its own
 * random schedule: the first visitor turns up after a random wait, walks in from a side, stays 15-30
 * seconds doing comic researcher things, walks off, and after a random 10-20 second gap the next one comes.
 * Never more than two are on the page at once; stages that have waited longest go first. Visitors are
 * drawn from a shuffled roster, so every character gets a turn before anyone repeats. Every character
 * belongs to one of the two workers (the young ones to the Research Assistant, the tenured ones to the
 * Principal Investigator), and a stage tagged with data-worker only gets visitors from that worker's crew.
 * On the Settings page each worker's card has its whole crew standing about (a "crew" stage).
 * Purely decorative: canvases are aria-hidden, ignore the pointer, and nothing here talks to the server.
 */
(() => {
"use strict";
if (window.__researchers) return;
window.__researchers = true;

const LIFE_MIN = 15, LIFE_MAX = 30;          // seconds on stage
const GAP_MIN = 10, GAP_MAX = 20;            // seconds between visitors on one stage
const FIRST_MIN = 1, FIRST_MAX = 20;         // the first visitor's random wait, so stages never start in step
const MAX_ON_STAGE = 2;                      // researchers on the whole page at once
const EDGE = 40;                             // how far off-stage they start and finish their walk
const REDUCE = matchMedia("(prefers-reduced-motion: reduce)").matches;
const now = () => performance.timeOrigin / 1000 + performance.now() / 1000;

// ---------- small helpers ----------

function rng(seed) {                          // mulberry32
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const pick = (r, list) => list[Math.floor(r() * list.length)];
const between = (r, lo, hi) => lo + r() * (hi - lo);
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
function weighted(r, table) {
  let total = 0;
  for (const k in table) total += table[k];
  let x = r() * total;
  for (const k in table) if ((x -= table[k]) < 0) return k;
  return Object.keys(table)[0];
}
function shade(hex, f) {
  const n = parseInt(hex.slice(1), 16);
  const ch = v => Math.round(clamp(f < 1 ? v * f : v + (255 - v) * (f - 1), 0, 255));
  return "#" + ((1 << 24) | (ch(n >> 16) << 16) | (ch((n >> 8) & 255) << 8) | ch(n & 255)).toString(16).slice(1);
}
function shuffle(list, r = Math.random) {
  const a = list.slice();
  for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(r() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; }
  return a;
}
const memory = {
  get(k) { try { return JSON.parse(sessionStorage.getItem(k)); } catch (e) { return null; } },
  set(k, v) { try { sessionStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
};

// ---------- the cast ----------

const INK = "#1d1b22", MOUTH = "#7a3434";
const SKIN = ["#ffe1c6", "#f5cba3", "#e8b38a", "#d49a6a", "#b97c4e", "#9a6038", "#7a4628", "#58321d"];
const BOOKS = ["#b33939", "#227093", "#218c74", "#cc8e35", "#6d214f", "#40407a"];
const NATURAL_HAIR = ["#17110e", "#2e1f17", "#4b3021", "#6e4527", "#93602f", "#c08d4a", "#a3a3a3"];

// Each character keeps their look (outfit, hair, headwear, signature prop); height, build and some
// colors vary per visit. `fav` boosts the actions they're known for; everything else can still happen.
// `worker` is whose crew they're in: "assistant" (young, the small model) or "pi" (tenured, the big model).
const ROSTER = {
  aris:   { worker: "pi", name: "Dr. Aris", skin: [6, 7, 5], hair: ["#cfcfcf", "#b5b5b5"], style: "receding", facial: "mustache",
            brows: true, outfit: "labcoat", shirt: ["#5b7fa6", "#6c8fb8"], pants: ["#3a3a40"], shoes: ["#6b3e26"],
            prop: "tablet", fav: { read: 2, citation: 2.5, notes: 1.5 } },
  aisha:  { worker: "pi", name: "Aisha", skin: [5, 6, 4, 3], head: "hijab", hijab: ["#1f3a5f", "#2d2d4a", "#4a2240", "#1e4d4a"],
            outfit: "abaya", top: ["#a9cbe8", "#c4b5e0", "#b5d8c6"], pants: ["#2c3e57"], shoes: ["#2b2b30"],
            prop: "tablet", fav: { notes: 2.5, search: 1.5, typing: 1.5 } },
  ben:    { worker: "assistant", name: "Ben", skin: [1, 2, 3], hair: ["#6e4527", "#93602f", "#4b3021"], style: "short",
            outfit: "sweater", top: ["#7d8691", "#5f7f6b", "#8a6f5a"], pants: ["#c8b48a", "#b5a27a"],
            shoes: ["#6b3e26"], pack: ["#6b4a2e", "#3d5a80"], prop: null, fav: { stack: 3, telescope: 1.5, eureka: 1.5 } },
  mei:    { worker: "assistant", name: "Mei", skin: [1, 2, 0], hair: ["#141414", "#2a1d17"], style: "bob", face: "glasses",
            outfit: "labcoat", shirt: ["#6f8fb3", "#8aa3c2"], pants: ["#2c3340"], shoes: ["#2b2b30"],
            prop: "laptop", fav: { typing: 3, read: 1.5, scroll: 1.5 } },
  sam:    { worker: "assistant", name: "Sam", skin: [2, 3, 4], hair: ["#1d1d1d"], style: "short", head: "cap", cap: ["#2f3640", "#3d3d55", "#7a2e3b"],
            outfit: "hoodie", top: ["#7a7f87", "#5c6370", "#6b5b7b"], pants: ["#4a78b5", "#3c5a99"], shoes: ["#e8e8e8"],
            prop: "phone", fav: { typing: 2.5, confused: 1.5, citation: 1.5 } },
  evelyn: { worker: "pi", name: "Prof. Evelyn", skin: [0, 1, 2], hair: ["#cfcfcf", "#e4e4e4"], style: "bigcurly",
            outfit: "cardigan", top: ["#8a5a3c", "#6e5a8a", "#4f6e5a"], shirt: ["#efe6d6"], legs: "skirt",
            skirt: ["#2f3640", "#3a2f40"], pants: ["#d8b89a"], shoes: ["#6b3e26"], prop: "book",
            fav: { read: 3, coffee: 2, sleep: 1.5 } },
  david:  { worker: "pi", name: "David", skin: [6, 7, 5], hair: ["#141414"], style: "crop", outfit: "suit",
            top: ["#22346b", "#2b2e38", "#3b3f48"], tie: ["#e8e8e8", "#c0392b", "#d4a017"], shoes: ["#2b2b30"],
            prop: ["briefcase", "tablet"], fav: { citation: 2, papers: 2, notes: 1.5 } },
  javier: { worker: "assistant", name: "Javier", skin: [3, 4, 5], hair: ["#2a1a12"], style: "short", outfit: "overalls",
            shirt: ["#b5473a", "#5f7f6b", "#e8e4da", "#8a6f9e"], pants: ["#3f5f8a", "#4a6f9e"], shoes: ["#6b3e26"], prop: null,
            fav: { dig: 3.5, search: 1.5, telescope: 1 } },
  hanna:  { worker: "assistant", name: "Hanna", skin: [1, 2, 0], hair: ["#a07850", "#6e4527", "#c9a46a"], style: "long", head: "beanie",
            beanie: ["#5d6b55", "#7a4a4a", "#45556b"], outfit: "jacket", top: ["#566152", "#4a5568", "#6b5a45"],
            pants: ["#4a6f9e"], shoes: ["#e8e8e8"], pack: ["#3a3f45", "#5a3f2e"], prop: "laptop",
            fav: { typing: 2, telescope: 1.5, scroll: 1.5 } },
  kenji:  { worker: "pi", name: "Kenji", skin: [1, 2, 3], hair: ["#141414"], style: "neat", face: "glasses", outfit: "labcoat",
            shirt: ["#5a7fa6", "#4f6d8a"], pants: ["#2f3640"], shoes: ["#2b2b30"], prop: "camera",
            fav: { search: 2.5, telescope: 2, eureka: 1.5 } },
  quill:  { worker: "pi", name: "Det. Quill", skin: [0, 1, 2, 3, 4, 5, 6, 7], hair: NATURAL_HAIR, style: "short", head: "deerstalker",
            outfit: "trench", top: ["#b8925a", "#a07c4c"], pants: ["#3a3a40"], shoes: ["#5d4037"], prop: "magnifier",
            facial: ["mustache", null], fav: { search: 4, confused: 1.5 } },
  pip:    { worker: "assistant", name: "Intern Pip", skin: [0, 1, 2, 3, 4, 5, 6, 7], hair: ["#c9572d", "#e1c27f", "#17110e"], style: "spiky",
            head: "propeller", outfit: "tshirt", top: ["#e84393", "#0abde3", "#f39c12", "#27ae60"], pants: ["#556b2f", "#34495e"],
            shoes: ["#c0392b", "#e8e8e8"], prop: "mug", fav: { papers: 2.5, coffee: 2.5, stack: 1.5 } },
};
const CAST = Object.keys(ROSTER);
const crewOf = worker => CAST.filter(k => !worker || ROSTER[k].worker === worker);

function makeLook(kind, seed) {
  const r = rng(seed), t = ROSTER[kind] || ROSTER.aris;
  const one = v => (Array.isArray(v) ? pick(r, v) : v);
  const a = { kind, seed, name: t.name, fav: t.fav || {} };
  a.skin = SKIN[r() < 0.7 ? one(t.skin) : Math.floor(r() * SKIN.length)];
  a.skinD = shade(a.skin, 0.82);
  a.hair = one(t.hair) || "#17110e"; a.hairD = shade(a.hair, 0.72); a.hairL = shade(a.hair, 1.25);
  a.style = t.style || "short"; a.head = t.head || null; a.face = t.face || null; a.facial = one(t.facial) || null;
  a.brows = !!t.brows;
  a.outfit = t.outfit;
  a.top = one(t.top) || "#7d8691"; a.topD = shade(a.top, 0.78);
  a.shirt = one(t.shirt) || "#e8e4da"; a.shirtD = shade(a.shirt, 0.82);
  a.coat = a.outfit === "labcoat" ? "#f4f4f0" : a.top; a.coatD = shade(a.coat, 0.82);
  a.pants = a.outfit === "suit" ? a.top : one(t.pants) || "#3a3a40"; a.pantsD = shade(a.pants, 0.75);
  a.shoes = one(t.shoes) || "#2b2b30"; a.shoesD = shade(a.shoes, 0.75);
  a.legsStyle = t.legs || "pants"; a.skirt = one(t.skirt) || "#2f3640";
  a.pack = one(t.pack) || null;
  a.tie = one(t.tie) || null;
  a.hijab = one(t.hijab) || "#1f3a5f"; a.cap = one(t.cap) || "#2f3640"; a.beanie = one(t.beanie) || "#5d6b55";
  a.prop = one(t.prop) || null;
  a.sleeve = { labcoat: a.coat, suit: a.top, hoodie: a.top, sweater: a.top, cardigan: a.top, jacket: a.top,
               abaya: a.top, trench: a.top, vest: a.shirt, overalls: a.shirt, tshirt: a.top }[a.outfit] || a.top;
  a.sleeveD = shade(a.sleeve, 0.78);
  a.short = a.outfit === "tshirt";            // short sleeves
  a.legs = 11 + Math.floor(r() * 7);          // 11..17 px: short to lanky
  a.torso = 11 + Math.floor(r() * 3);         // 11..13
  a.width = pick(r, [11, 12, 12, 13]);
  a.speed = 20 + a.legs * 1.4 + r() * 6;      // px per second
  a.book = pick(r, BOOKS);
  return a;
}

// ---------- drawing ----------

// P(x, y, w, h, color): one rectangle of pixels. x runs toward the way the researcher faces, y runs up
// from the floor. Mirroring for left-facing happens here, so every part is drawn facing right.
function painter(g, ox, oy, face) {
  return (x, y, w, h, c) => {
    if (!c || w <= 0 || h <= 0) return;
    g.fillStyle = c;
    g.fillRect(ox + (face > 0 ? x : -(x + w - 1)), oy - y - h + 1, w, h);
  };
}
const box = (P, x, y, w, h, c) => { P(x - 1, y - 1, w + 2, h + 2, INK); P(x, y, w, h, c); };

// A thick straight limb from (x0, y0) to (x1, y1), w pixels wide, with its own dark outline so it reads
// against whatever is behind it.
function limb(P, x0, y0, x1, y1, w, c, ink = true) {
  const n = Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0), 1), o = Math.floor((w - 1) / 2), pts = [];
  for (let i = 0; i <= n; i++) pts.push([Math.round(x0 + (x1 - x0) * i / n), Math.round(y0 + (y1 - y0) * i / n)]);
  if (ink) for (const [x, y] of pts) P(x - o - 1, y - o - 1, w + 2, w + 2, INK);
  for (const [x, y] of pts) P(x - o, y - o, w, w, c);
}

function drawLeg(P, a, cx, off, lift, L, color) {
  const lf = lift ? 2 : 0, foot = cx - 1 + off;
  limb(P, cx, L - 2, cx + off, lf + 4, 4, color);
  box(P, foot, lf + 1, 5, 2, color === a.pantsD ? a.shoesD : a.shoes);
}

function drawArm(P, a, spec, g, back) {
  const [mode, v = 0] = spec || ["swing", 0];
  const ax = back ? g.tl - 1 : g.tr, sy = g.sh - 1;
  const d = back ? -1 : 1;
  let elbow, hand;
  switch (mode) {
    case "forward": elbow = [ax, sy - 5]; hand = [ax + 6 + v, sy - 6]; break;
    case "high":    elbow = [ax + 1, sy - 5]; hand = [ax + 5 + v, sy]; break;
    case "low":     elbow = [ax + 1, sy - 5]; hand = [ax + 6 + v, sy - 10]; break;
    case "up":      elbow = [ax + 3 * d, sy + 5]; hand = [ax + (4 + v) * d, g.top + 1]; break;
    case "head":    elbow = [ax + 5, sy + 6]; hand = [ax + 1, g.top - 1 + v]; break;
    case "mouth":   elbow = [ax + 1, sy - 5]; hand = [ax + 3, g.head + 2]; break;
    default:        elbow = [ax + Math.round(v / 2), sy - 6]; hand = [ax + v, sy - a.torso + 3];
  }
  const sleeve = back ? a.sleeveD : a.sleeve, skin = back ? a.skinD : a.skin;
  limb(P, ax, sy, elbow[0], elbow[1], 3, sleeve);
  limb(P, elbow[0], elbow[1], hand[0], hand[1], 3, a.short ? skin : sleeve);
  if (!a.short) P(hand[0] - 1, hand[1] - 1, 3, 1, shade(sleeve, 0.85)); // cuff
  box(P, hand[0] - 1, hand[1] - 1, 3, 3, skin);
  return hand;
}

function drawTorso(P, a, g) {
  const { tl, tr, hip, sh } = g, W = tr - tl + 1, T = sh - hip + 1;
  const plain = (c, cd) => { box(P, tl, hip, W, T, c); P(tl, hip, 2, T, cd); };
  switch (a.outfit) {
    case "labcoat":
      P(tl - 1, hip - 6, W + 2, T + 7, INK);
      P(tl, hip - 5, W, T + 5, a.coat); P(tl, hip - 5, 2, T + 5, a.coatD);
      P(0, hip - 5, 1, 6, INK);                                   // coat tails part
      P(-1, hip + 2, 3, T - 4, a.shirt); P(-2, sh - 2, 5, 3, a.shirt);
      if (a.tie) P(0, hip + 3, 1, T - 4, a.tie);
      P(-2, hip + 2, 1, T - 4, a.coatD); P(2, hip + 2, 1, T - 4, a.coatD);
      P(tr - 4, hip + 2, 3, 1, a.coatD); P(tr - 3, hip + 3, 1, 2, "#2e6fd8");
      break;
    case "suit":
      P(tl - 1, hip - 3, W + 2, T + 4, INK);
      P(tl, hip - 2, W, T + 2, a.top); P(tl, hip - 2, 2, T + 2, a.topD);
      P(-1, sh - 5, 3, 6, "#f2f2f2"); P(-2, sh - 1, 5, 2, "#f2f2f2");
      P(0, sh - 6, 1, 6, a.tie || "#c0392b"); P(-1, sh - 1, 3, 1, a.tie || "#c0392b");
      P(-2, sh - 6, 1, 5, a.topD); P(2, sh - 6, 1, 5, a.topD); P(1, hip + 2, 1, 1, a.topD); P(1, hip + 5, 1, 1, a.topD);
      break;
    case "hoodie":
      plain(a.top, a.topD);
      P(-3, hip + 1, 7, 3, a.topD); P(-2, hip + 2, 5, 1, shade(a.top, 0.7));
      P(-1, sh - 4, 1, 4, "#eeeeee"); P(2, sh - 4, 1, 4, "#eeeeee"); P(tl, hip, W, 1, a.topD);
      break;
    case "cardigan":
      box(P, tl, hip - 1, W, T + 1, a.top); P(tl, hip - 1, 2, T + 1, a.topD);
      P(-1, hip + 1, 3, T - 1, a.shirt); P(-2, sh - 1, 5, 2, a.shirt);
      P(2, hip + 2, 1, 1, a.topD); P(2, hip + 5, 1, 1, a.topD); P(2, hip + 8, 1, 1, a.topD);
      break;
    case "vest":
      plain(a.shirt, a.shirtD);
      P(tl + 1, hip, W - 2, T - 2, "#ff8a1a"); P(-1, hip, 2, T - 2, "#e5741a");
      P(tl + 1, hip + 3, W - 2, 2, "#ffe14d"); P(-4, hip + 5, 2, T - 7, "#ffe14d"); P(2, hip + 5, 2, T - 7, "#ffe14d");
      break;
    case "overalls": {                                             // a shirt under a denim bib and straps
      const bib = T - 5;
      plain(a.shirt, a.shirtD);
      P(tl + 1, hip, W - 2, bib, a.pants); P(tl + 1, hip, 2, bib, a.pantsD);
      P(-3, hip + bib, 2, 5, a.pants); P(2, hip + bib, 2, 5, a.pants);
      P(-3, hip + bib - 1, 1, 1, "#d8b36a"); P(3, hip + bib - 1, 1, 1, "#d8b36a");   // brass buttons
      P(-1, hip + 2, 3, 2, a.pantsD);                                                // bib pocket
      break;
    }
    case "jacket":
      plain(a.top, a.topD);
      P(0, hip, 1, T, a.topD); P(-2, sh - 1, 5, 2, a.topD); P(tr - 4, hip + 2, 3, 1, a.topD);
      break;
    case "abaya": {
      P(tl - 2, 1, W + 4, sh, INK);
      P(tl, 2, W, sh - 1, a.top); P(tl - 1, 2, W + 2, 4, a.top);
      P(tl, 2, 2, sh - 1, a.topD); P(1, 2, 1, sh - 4, a.topD);
      break;
    }
    case "trench":
      P(tl - 1, hip - 7, W + 2, T + 8, INK);
      P(tl, hip - 6, W, T + 6, a.top); P(tl, hip - 6, 2, T + 6, a.topD);
      P(tl, hip + 1, W, 1, "#5a4026"); P(1, hip + 1, 1, 1, "#d8b36a");
      P(-2, sh - 4, 1, 4, a.topD); P(2, sh - 4, 1, 4, a.topD); P(-1, sh - 3, 3, 4, a.shirt);
      P(2, hip + 4, 1, 1, a.topD); P(2, hip + 7, 1, 1, a.topD);
      break;
    case "tshirt":
      plain(a.top, a.topD);
      P(0, hip + 4, 3, 3, "#ffffff"); P(1, hip + 5, 1, 1, a.top); P(-1, sh, 3, 1, a.skinD);
      break;
    default:                                                       // sweater
      plain(a.top, a.topD);
      P(-2, sh, 5, 1, a.topD); P(-1, sh, 3, 1, a.skinD); P(tl, hip, W, 1, a.topD);
  }
}

function drawBack(P, a, g) {                                      // things worn behind the body
  if (a.pack) {
    box(P, g.tl - 4, g.hip + 1, 5, g.sh - g.hip - 2, a.pack);
    P(g.tl - 4, g.hip + 3, 5, 1, shade(a.pack, 0.75));
  }
}

function drawHead(P, a, g, p, t) {
  const y = g.head;
  // behind the face
  if (a.head === "hijab") {
    P(-7, g.sh - 2, 14, 6, INK); P(-6, g.sh - 1, 12, 4, a.hijab);   // drape over the shoulders
    P(-6, y - 2, 13, 15, INK); P(-5, y - 1, 11, 13, a.hijab); P(-4, y + 12, 9, 1, a.hijab);
  }
  if (a.outfit === "hoodie") { P(-6, g.sh + 1, 8, 4, INK); P(-5, g.sh + 1, 6, 3, a.topD); }
  if (a.style === "bigcurly" && a.head !== "hijab") {
    P(-8, y + 2, 16, 11, INK); P(-7, y + 3, 14, 9, a.hair); P(-6, y + 12, 12, 2, a.hair); P(-5, y + 14, 3, 1, INK);
    P(-6, y + 1, 3, 2, a.hair);
  }
  if (a.style === "long" && a.head !== "hijab") { P(-6, y - 5, 5, 15, INK); P(-5, y - 4, 3, 13, a.hair); }
  if (a.style === "bob") { P(-6, y + 1, 4, 11, INK); P(-5, y + 2, 3, 9, a.hair); }
  // neck and face
  P(-1, g.sh + 1, 3, 1, a.skinD);
  if (a.head === "hijab") {
    P(-1, y + 1, 7, 9, a.skin); P(0, y, 5, 1, a.skin); P(-2, y + 1, 1, 9, a.hijab); P(-1, y + 10, 7, 1, a.hijab);
  } else {
    P(-5, y, 11, 11, INK);
    P(-4, y + 1, 10, 9, a.skin); P(-3, y, 8, 1, a.skin); P(-3, y + 10, 8, 1, a.skin);
    P(-3, y + 4, 2, 3, a.skinD); P(-3, y + 5, 1, 1, shade(a.skin, 0.7));      // ear
  }
  // face: eyes, nose, mouth
  const eyes = p.eyes || "open";
  if (eyes === "closed") { P(0, y + 5, 2, 1, INK); P(3, y + 5, 2, 1, INK); }
  else if (eyes === "down") { P(1, y + 5, 1, 1, INK); P(4, y + 5, 1, 1, INK); }
  else if (eyes === "wide") { P(1, y + 5, 1, 3, INK); P(4, y + 5, 1, 3, INK); }
  else { P(1, y + 5, 1, 2, INK); P(4, y + 5, 1, 2, INK); }
  P(3, y + 3, 2, 1, a.skinD);
  if (p.blush) { P(0, y + 3, 1, 1, "#ff8a8a"); P(5, y + 3, 1, 1, "#ff8a8a"); }
  const mouth = p.mouth || "none";
  if (mouth === "smile") { P(2, y + 1, 3, 1, MOUTH); P(1, y + 2, 1, 1, MOUTH); P(5, y + 2, 1, 1, MOUTH); }
  else if (mouth === "o") P(2, y + 1, 2, 2, MOUTH);
  else if (mouth === "flat") P(2, y + 2, 3, 1, MOUTH);
  else P(2, y + 2, 2, 1, shade(a.skin, 0.7));
  if (a.brows && eyes !== "closed") { P(0, y + 8, 2, 1, a.hairD); P(4, y + 8, 2, 1, a.hairD); }
  if (a.facial === "mustache") { P(1, y + 3, 5, 1, a.hair); P(0, y + 2, 1, 1, a.hair); }
  if (a.facial === "beard") { P(-2, y, 8, 3, a.hair); if (mouth === "o") P(2, y + 1, 2, 2, MOUTH); }
  if (a.head !== "hijab") drawHair(P, a, y);
  if (a.face === "glasses") {
    for (const ex of [1, 4]) {                     // round frames with a glint of lens beside each eye
      P(ex - 1, y + 4, 4, 1, INK); P(ex - 1, y + 7, 4, 1, INK); P(ex - 1, y + 5, 1, 2, INK); P(ex + 2, y + 5, 1, 2, INK);
      P(ex + 1, y + 5, 1, 2, "#dff1ff");
    }
    P(-2, y + 6, 2, 1, INK);
  }
  drawHeadwear(P, a, y, t);
}

function drawHair(P, a, y) {
  const c = a.hair, d = a.hairD, l = a.hairL;
  switch (a.style) {
    case "receding":
      P(-5, y + 4, 3, 6, INK); P(-4, y + 5, 2, 5, c); P(-4, y + 10, 4, 1, c); P(-3, y + 11, 3, 1, INK);
      break;
    case "bob":
      P(-5, y + 8, 11, 4, INK); P(-4, y + 9, 10, 2, c); P(-3, y + 11, 8, 1, c); P(0, y + 8, 6, 1, c);
      P(5, y + 3, 1, 6, c); P(-1, y + 10, 3, 1, l);
      break;
    case "bigcurly":
      P(-4, y + 9, 10, 2, c); P(-2, y + 11, 2, 1, d); P(2, y + 12, 2, 1, d); P(-5, y + 7, 1, 1, d); P(5, y + 9, 1, 1, d);
      break;
    case "long":
      P(-5, y + 8, 11, 4, INK); P(-4, y + 9, 10, 2, c); P(-3, y + 11, 8, 1, c); P(-4, y + 4, 2, 5, c); P(4, y + 8, 2, 1, c);
      break;
    case "crop":
      P(-5, y + 8, 11, 4, INK); P(-4, y + 9, 10, 2, c); P(-3, y + 11, 8, 1, c); P(-4, y + 6, 1, 3, c);
      break;
    case "neat":
      P(-5, y + 8, 11, 5, INK); P(-4, y + 9, 10, 3, c); P(-3, y + 12, 8, 1, c); P(-4, y + 5, 2, 4, c);
      P(2, y + 8, 4, 1, c); P(0, y + 11, 1, 1, l);
      break;
    case "spiky":
      P(-5, y + 8, 11, 4, INK); P(-4, y + 9, 10, 2, c); P(-4, y + 5, 2, 4, c);
      for (const [x, h] of [[-4, 2], [-2, 3], [0, 2], [2, 3], [4, 2]]) { P(x - 1, y + 11, 3, h + 1, INK); P(x, y + 11, 1, h, c); }
      break;
    default:                                                       // short
      P(-5, y + 8, 11, 4, INK); P(-4, y + 9, 10, 2, c); P(-3, y + 11, 8, 1, c); P(-4, y + 5, 2, 4, c);
      P(2, y + 8, 3, 1, c); P(-1, y + 10, 3, 1, l);
  }
}

function drawHeadwear(P, a, y, t) {
  switch (a.head) {
    case "cap":
      box(P, -4, y + 9, 10, 3, a.cap); P(-3, y + 12, 8, 1, a.cap); P(-3, y + 13, 8, 1, INK);
      box(P, 5, y + 9, 4, 1, shade(a.cap, 0.75)); P(0, y + 10, 3, 1, "#d8dde3");
      break;
    case "beanie":
      box(P, -5, y + 8, 11, 2, shade(a.beanie, 0.8)); box(P, -4, y + 10, 10, 3, a.beanie);
      P(-3, y + 13, 8, 1, a.beanie); P(-3, y + 14, 8, 1, INK);
      for (let x = -3; x < 6; x += 2) P(x, y + 8, 1, 2, shade(a.beanie, 0.65));
      break;
    case "hardhat":
      box(P, -6, y + 9, 13, 1, "#e0a800"); box(P, -4, y + 10, 10, 3, "#ffd23f"); P(-3, y + 13, 8, 1, "#ffd23f");
      P(-3, y + 14, 8, 1, INK); P(0, y + 10, 1, 4, "#e0a800"); P(-3, y + 12, 2, 1, "#fff2a8");
      break;
    case "deerstalker":
      box(P, -5, y + 9, 11, 4, "#a67c52"); box(P, -8, y + 9, 3, 1, "#8a6440"); box(P, 6, y + 9, 3, 1, "#8a6440");
      for (const [x, yy] of [[-3, 10], [0, 11], [3, 10], [-1, 12], [2, 12]]) P(x, y + yy, 1, 1, "#7d5a39");
      P(0, y + 13, 1, 1, INK);
      break;
    case "propeller": {
      box(P, -4, y + 9, 10, 2, "#e74c3c"); P(-3, y + 11, 8, 1, "#3498db"); P(-3, y + 12, 8, 1, INK);
      P(0, y + 12, 1, 2, "#555"); P(0, y + 14, 1, 1, "#f1c40f");
      const f = Math.floor(t * 14) % 3, w = [9, 5, 1][f];
      box(P, 1 - Math.ceil(w / 2), y + 15, w, 1, "#f1c40f");
      break;
    }
  }
}

function crownOf(a) {
  return 11 + ({ cap: 3, beanie: 4, hardhat: 4, deerstalker: 3, propeller: 6 }[a.head]
    || ({ bigcurly: 4, spiky: 4, neat: 2 }[a.style] || 1));
}

// Draws the whole researcher; returns where the hands and head ended up so props can attach.
function drawBody(P, a, p, t) {
  const L = p.sit ? 4 : Math.max(5, a.legs - 2 * (p.crouch || 0));
  const bob = p.bob || 0, hip = L + bob, sh = hip + a.torso - 1;
  const tl = -Math.ceil(a.width / 2), tr = tl + a.width - 1;
  const g = { L, tl, tr, hip, sh, head: sh + 2 - (p.nod ? 1 : 0) };
  g.top = g.head + 10;
  g.crown = g.head + crownOf(a);
  g.handB = drawArm(P, a, p.armB, g, true);
  drawBack(P, a, g);
  const legC = a.legsStyle === "skirt" ? a.pants : a.pants;
  if (p.sit) {
    box(P, tl + 1, 1, 6, 4, a.pantsD);
    limb(P, 0, 3, a.legs, 3, 4, legC); box(P, a.legs + 1, 1, 3, 5, a.shoes);
  } else {
    drawLeg(P, a, -3, p.legB || 0, p.liftB, L, a.pantsD);
    drawLeg(P, a, 1, p.legF || 0, p.liftF, L, legC);
  }
  if (a.legsStyle === "skirt" && !p.sit) { P(tl, hip - 7, a.width, 8, INK); P(tl + 1, hip - 6, a.width - 2, 6, a.skirt); P(tl + 1, hip - 6, a.width - 2, 1, shade(a.skirt, 1.2)); }
  drawTorso(P, a, g);
  drawHead(P, a, g, p, t);
  g.handF = drawArm(P, a, p.armF, g, false);
  return g;
}

// ---------- props (hx, hy is the middle of the hand holding it) ----------

const PROPS = {
  tablet(P, a, hx, hy) { box(P, hx - 1, hy - 3, 8, 6, "#5f6670"); P(hx, hy - 2, 6, 4, "#bcd3e8"); P(hx + 4, hy + 1, 1, 1, "#ffffff"); },
  laptop(P, a, hx, hy) {                        // open, tilted toward the viewer like the sketch
    box(P, hx - 3, hy - 1, 12, 2, "#a3a9b1");
    box(P, hx - 1, hy + 1, 10, 7, "#7d838b"); P(hx, hy + 2, 8, 5, "#bfe0f2"); P(hx + 5, hy + 5, 2, 1, "#ffffff");
  },
  phone(P, a, hx, hy) { box(P, hx, hy - 1, 3, 5, "#26262b"); P(hx, hy, 3, 3, "#7fd0ff"); },
  book(P, a, hx, hy) {
    box(P, hx - 1, hy - 3, 6, 8, a.book); P(hx - 1, hy - 3, 1, 8, shade(a.book, 0.65));
    P(hx + 4, hy - 2, 1, 6, "#f6f0dc"); P(hx + 1, hy + 2, 3, 1, "#f1c40f");
  },
  briefcase(P, a, hx, hy) { P(hx - 1, hy - 2, 3, 1, INK); box(P, hx - 4, hy - 9, 9, 6, "#2b2b30"); P(hx, hy - 5, 1, 1, "#c9a227"); P(hx - 4, hy - 6, 9, 1, "#3d3d44"); },
  camera(P, a, hx, hy) { box(P, hx - 1, hy - 2, 7, 5, "#2f3138"); box(P, hx + 6, hy - 1, 2, 3, "#555c66"); P(hx + 3, hy + 3, 2, 1, "#dddddd"); P(hx, hy + 2, 1, 1, "#e74c3c"); },
  magnifier(P, a, hx, hy, glint) {
    limb(P, hx + 1, hy - 1, hx + 3, hy - 3, 2, "#6d4c41");
    P(hx + 3, hy - 10, 7, 7, INK); P(hx + 4, hy - 9, 5, 5, glint ? "#ffffff" : "#bfe9ff"); P(hx + 5, hy - 6, 1, 1, "#ffffff");
  },
  mug(P, a, hx, hy) {
    box(P, hx - 1, hy - 2, 5, 5, "#f7f7f7"); P(hx - 1, hy, 5, 1, "#e84393"); P(hx - 1, hy + 2, 5, 1, "#6b3e26");
    P(hx - 3, hy - 1, 2, 3, INK); P(hx - 2, hy, 1, 1, "#f7f7f7");
  },
};
function propClipboard(P, hx, hy, lines) {
  box(P, hx - 1, hy - 4, 8, 10, "#a0703a"); P(hx, hy - 3, 6, 8, "#ffffff"); P(hx + 2, hy + 5, 2, 1, "#9aa0a6");
  for (let i = 0; i < Math.min(lines, 4); i++) P(hx + 1, hy + 2 - i * 2, 2 + ((i * 7) % 3), 1, "#5f6368");
}
function propPencil(P, hx, hy) { P(hx, hy + 1, 1, 4, "#f2c94c"); P(hx, hy, 1, 1, "#444"); }
function propShovel(P, hx, hy, down) {
  const by = down ? 1 : 4;
  limb(P, hx, hy, hx + 8, by + 4, 2, "#8d6e63");
  box(P, hx + 7, by, 5, 5, "#9aa3ad"); P(hx + 7, by + 4, 5, 1, "#c7ced6");
}
function propSpyglass(P, g, ext) {
  const y = g.head + 4, n = Math.round(ext * 3);
  box(P, 6, y, 6, 3, "#b8901c");
  if (n > 0) box(P, 12, y, 6, 3, "#d4a72c");
  if (n > 1) box(P, 18, y, 5, 3, "#e8c050");
  if (n > 2) P(23, y, 1, 3, "#9fd8ff");
}
function propLapLaptop(P, x, glow) { box(P, x, 6, 10, 2, "#a3a9b1"); box(P, x + 10, 8, 2, 8, "#5f6368"); P(x + 9, 8, 1, 8, glow ? "#c8f3ff" : "#7fd0ff"); }
function propScroll(P, x, len) {
  if (len > 1) { box(P, x, 1, len, 2, "#f6efd6"); for (let i = 3; i < len - 3; i += 4) P(x + i, 2, 2, 1, "#b8a77a"); }
  box(P, x + len, 1, 4, 4, "#e2d2a0");
}
function propStack(P, hx, hy, n, wob, colors) {
  for (let i = 0; i < n; i++) {
    const dx = Math.round(Math.sin(wob + i * 0.7) * i * 0.6), w = 9 + (i % 2);
    box(P, hx - 3 + dx, hy + i * 4, w, 3, colors[i % colors.length]); P(hx - 3 + dx + w - 1, hy + i * 4, 1, 3, "#f6f0dc");
  }
}
function propPaper(P, hx, hy) { box(P, hx, hy, 5, 6, "#ffffff"); P(hx + 1, hy + 3, 3, 1, "#9aa0a6"); P(hx + 1, hy + 1, 3, 1, "#c0392b"); }

// What each signature prop does to the pose while walking or standing about.
const CARRY = {
  tablet: ["forward", 0], laptop: ["forward", -1], phone: ["high", 0], book: ["forward", 0],
  briefcase: null, camera: ["forward", 0], magnifier: ["forward", 0], mug: ["forward", 0],
};

// ---------- icons and particles (drawn straight onto the stage) ----------

const ICONS = {
  bulb: ["..kkk..", ".kyyyk.", "kywyyyk", "kyyyyyk", "kyyyyyk", ".kyyyk.", "..kgk..", "..kgk..", "...k..."],
  q: [".kkkk.", "kqqqqk", "kqkkqk", ".kkqqk", "..kqk.", "..kkk.", "..kqk.", "..kkk."],
  bang: ["kkk", "kek", "kek", "kek", "kek", "kkk", "...", "kkk", "kek", "kkk"],
  heart: [".kk.kk.", "kppkppk", "kpppppk", ".kpppk.", "..kpk..", "...k..."],
};
const ICON_COLORS = { k: INK, y: "#ffe066", w: "#ffffff", g: "#9aa0a6", q: "#5fa8ff", e: "#ff5a5a", p: "#ff6b9a" };
function drawIcon(ctx, map, x, y, s = 2) {
  for (let r = 0; r < map.length; r++)
    for (let c = 0; c < map[r].length; c++) {
      const ch = map[r][c];
      if (ch !== ".") { ctx.fillStyle = ICON_COLORS[ch]; ctx.fillRect(x + c * s, y + r * s, s, s); }
    }
}
const Z = ["zzz", "..z", ".z.", "z..", "zzz"];

// ---------- actions ----------
// Each action has init (sets up its own state) and run, which moves the researcher and returns a pose:
// legs, arms, eyes, mouth, plus an optional props(P, g) drawn in front of the body. `carry: true` lets the
// researcher keep holding their signature prop.

const stand = extra => Object.assign({ armF: ["swing", 0], armB: ["swing", 0] }, extra);
function walkPose(r, dist) {
  const ph = dist / (1.1 + r.a.legs * 0.12), s = Math.sin(ph), c = Math.cos(ph), off = Math.round(4 * s);
  return { legF: off, legB: -off, liftF: c > 0.6 ? 1 : 0, liftB: c < -0.6 ? 1 : 0, bob: Math.abs(s) < 0.4 ? 1 : 0,
           armF: ["swing", -Math.round(2 * s)], armB: ["swing", Math.round(2 * s)] };
}
function stepToward(r, s, tx, dt, speed) {
  const d = tx - r.x;
  if (Math.abs(d) < 0.5) return true;
  r.face = Math.sign(d);
  const m = Math.min(Math.abs(d), speed * dt);
  r.x += r.face * m; s.dist = (s.dist || 0) + m;
  return false;
}
function roomAhead(r, need) {                     // turn around if there's no room for the bit
  const [lo, hi] = r.stage.range();
  if (r.face > 0 && r.x + need > hi) r.face = -1;
  else if (r.face < 0 && r.x - need < lo) r.face = 1;
}
const QUOTES = {
  intro: ["On it!", "Reporting in.", "Who funded this?", "Let's dig.", "Facts, please.", "Hello, sources."],
  read: ["hm.", "fascinating", "p. 412...", "a footnote!", "wait, what", "...interesting"],
  search: ["a clue!", "evidence?", "aha, a URL", "hmmmm"],
  sniff: ["sniff sniff", "smells like spin"],
  eureka: ["AHA!", "EUREKA!", "it all connects!", "It is possible that..."],
  confused: ["says who?", "huh?", "source??", "that's spin"],
  citation: ["[citation needed]", "who funded this?", "follow the money", "primary source!", "is that a fact?"],
  coffee: ["coffee #7", "*slurp*", "ahh."],
  notes: ["noted.", "*scribble*", "very suspicious"],
  dig: ["digging deeper", "found a PDF!", "buried on p. 90!"],
  typing: ["ENHANCE", "Ctrl+F", "47 tabs open", "404?!"],
  papers: ["too many sources!", "WHEE", "peer review!"],
  telescope: ["zoom.", "land ho?", "I see bias"],
  scroll: ["the full report", "terms & conditions", "appendix Q..."],
  trip: ["oof", "I'm OK!"],
  wave: ["hi!", "o/", "hello!", "busy busy"],
  stack: ["light reading", "just a few books"],
  topple: ["NOOO"],
};


// The general library: academic nagging and literary misquotes. Dealt from a shuffled deck shared by the
// whole page, so a line comes round again only after all the others have been said.
const QUIPS = [
  // the classics of the margin
  "[citation needed]", "Show your work!", "Where is your bibliography?", "Don't forget logos!",
  "Ethos, pathos... LOGOS.", "Cite your sources!", "Says who, exactly?", "Source?", "Source??",
  "Is that peer-reviewed?", "According to whom?", "Primary or secondary?", "MLA or APA?",
  "Chicago style, obviously.", "Footnote it!", "Define your terms.", "Where's the thesis?",
  "Needs more sources.", "Wikipedia is not a source.", "Cite the cite.", "Who wrote this?",
  "Check the date!", "Page number, please.", "Paraphrase, don't plagiarize.", "Quotation marks!",
  "[sic]", "Allegedly.", "Reportedly...", "n = ?", "p < 0.05?", "Methodology??",
  "Show me the receipts.", "Follow the money.", "Conflict of interest?", "Is this a press release?",
  "Spin detected.", "Fact, or possibility?", "It is possible that...", "Hedge your claims!",
  "Burden of proof!", "Extraordinary claims need extraordinary evidence.", "Occam's razor!",
  "Straw man!", "Ad hominem!", "Slippery slope!", "That's begging the question.", "Red herring!",
  "Anecdote is not data.", "Cherry-picked!", "Is it reproducible?", "Retracted?!",
  "Read past the abstract.", "Fine print time.", "Check the Wayback Machine.", "Ctrl+F 'funding'",
  "Who is the source's source?", "Two sources, minimum.", "Triangulate!", "Correlation is not causation!",
  "Ibid.", "Op. cit.", "See appendix B.", "Et al. al. al.", "Per my last footnote...",
  "Reviewer 2 strikes again.", "Publish or perish!", "The margin is too small for this proof.",
  "Q.E.D.", "Lit review time!", "Annotated bibliography!", "Works cited, please.", "Vide infra.",
  "Cf. literally everything.", "Ergo...", "A priori!", "Post hoc, ergo propter hoc!", "Caveat lector.",
  "Where's the DOI?", "Paywalled. Again.", "Interlibrary loan!", "Shh! This is a library.",
  "Overdue since 1987.", "To the card catalog!", "Microfiche, anyone?", "Footnote 47b...",
  "Endnotes are a crime.", "Oxford comma, please.", "Mistakes were made. (passive voice)",
  "Too many adverbs, really.", "Kill your darlings.", "Show, don't tell.", "Word count: 3.",
  "14pt font is cheating.", "Double spaced!", "Times New Roman only.", "Margins: one inch.",
  "Rough draft #12.", "See me after class.", "Needs a counterargument.", "Steelman it!",
  "Nuance, people!", "Context matters.", "Data, or it didn't happen.", "Trust, but verify.",
  "Verify, then trust.", "Rigor!", "A footnote to a footnote.", "The plot thickens...",
  "Curiouser and curiouser.", "Office hours are now!", "Grant deadline!", "Tenure track, baby.",
  "Hypothesis: more coffee.", "Sample size of one?", "Who funded the study?", "Check the methods section.",
  "That graph has no axes!", "Label your axes!", "Units?!", "Error bars, please.", "Outlier!",
  "Statistically significant? Maybe.", "This needs a control group.", "Double-blind it!",
  "Is that a primary source?", "Quote it exactly.", "Out of context!", "Read the whole thing.",
  "Who benefits?", "Cui bono?", "Find the original.", "Screenshot or it didn't happen.",
  "That's an op-ed.", "Opinion is not evidence.", "Sounds like a press release.",
  "Thesis, antithesis, synthesis!", "Peer review incoming!", "Bold claim. Bold font?",
  // literary misquotes
  "O Romeo, wherefore art thou citations?", "To cite, or not to cite?", "Something is rotten in the footnotes.",
  "A source! A source! My kingdom for a source!", "Et tu, Reviewer 2?", "The lady doth cite too much.",
  "All the world's a peer review.", "Friends, Romans, cite your sources!", "Brevity is the soul of abstracts.",
  "Out, damned typo!", "Double, double, toil and trouble; footnote burn and draft bubble.",
  "What's in a name? A byline.", "Is this a citation I see before me?",
  "Now is the winter of our deadline.", "Parting is such sweet sorrow. Submit!",
  "It was the best of sources, it was the worst of sources.", "Call me Ishmael. Cite me properly.",
  "It is a truth universally acknowledged that a claim must want a source.",
  "All happy footnotes are alike.", "Reader, I cited him.", "So we beat on, footnotes against the current.",
  "Elementary, my dear Watson: check the source.", "The game is afoot!", "Quoth the raven: citation?",
  "Nevermore shall I trust an unsourced claim.", "I think, therefore I cite.", "Veni, vidi, citavi.",
  "Know thyself. Cite thyself.", "The unexamined source is not worth citing.",
  "Not all who wander are lost. Some are in the stacks.", "Hope is the thing with footnotes.",
  "Two roads diverged; I took the one with sources.", "Do not go gentle into that bad draft.",
  "Water, water everywhere, nor any source to cite.", "Big Brother is fact-checking you.",
  "So it goes. (citation needed)", "Don't panic. Cite.", "We are such stuff as drafts are made on.",
  "Lord, what fools these sources be!", "The course of true research never did run smooth.",
  "Cry havoc, and let slip the footnotes!", "Alas, poor Yorick! He had no sources.",
  "Once more unto the library, dear friends!", "A rose by any other name would still need a citation.",
  "Frankly, my dear, I need a DOI.", "I came, I saw, I peer-reviewed.", "Ask not what your source can do for you.",
  "Tomorrow, and tomorrow, and tomorrow: the deadline.", "Neither a borrower nor a plagiarist be.",
  "Though this be madness, yet there is method(ology) in it.", "Exit, pursued by Reviewer 2.",
  "The fault, dear Brutus, is in our sources.", "Get thee to a library!", "Beware the Ides of Deadlines.",
  "I have measured out my life with footnotes.", "April is the cruellest month. Finals.",
  "Happy families are all alike; every unsourced claim is unsourced in its own way.",
  "Whose source is this? I think I know.", "Yes, Virginia, there is a bibliography.",
];
const QUIP_DECK = "researcher:quips";
function nextQuip() {
  let deck = memory.get(QUIP_DECK);
  if (!Array.isArray(deck) || !deck.length) deck = shuffle(QUIPS.map((_, i) => i));
  const i = deck.pop();
  memory.set(QUIP_DECK, deck);
  return QUIPS[i] || QUIPS[0];
}
const readTime = text => clamp(1.6 + text.length * 0.055, 1.8, 5);

const ACTIONS = {
  spawn: {                                       // walk in from one side
    init(r) {
      const [lo, hi] = r.stage.range(), side = r.rand() < 0.5 ? -1 : 1;
      r.x = side < 0 ? lo - EDGE : hi + EDGE;
      const tx = r.stage.kind === "pill" ? r.stage.spawnX(r.rand)
        : side < 0 ? lo + between(r.rand, 10, 140) : hi - between(r.rand, 10, 140);
      return { tx: clamp(tx, lo, hi) };
    },
    run(r, s, dt) {
      r.alpha = r.stage.edgeFade(r.x); r.y = r.stage.hop(r.x);
      if (stepToward(r, s, s.tx, dt, r.a.speed)) {
        r.alpha = 1; r.y = 0; s.done = true;
        if (r.rand() < 0.45) r.say(r.rand() < 0.35 ? `${r.a.name}, reporting in.` : nextQuip());
        return stand({ carry: true });
      }
      return stand(Object.assign(walkPose(r, s.dist || 0), { carry: true }));
    },
  },
  despawn: {                                     // and walk off the nearer side
    init(r) {
      const [lo, hi] = r.stage.range();
      if (r.rand() < 0.5) r.say(pick(r.rand, ["Bye!", "Off to the archives!", "Lunch break!", "brb, library",
        "Exit, pursued by a deadline.", "To the stacks!", "My work here is cited.", "Fin."]));
      return { tx: r.x - lo < hi - r.x ? lo - EDGE : hi + EDGE };
    },
    run(r, s, dt) {
      r.alpha = r.stage.edgeFade(r.x); r.y = r.stage.hop(r.x);
      if (stepToward(r, s, s.tx, dt, r.a.speed * 1.2) || r.alpha <= 0) r.gone = true;
      return stand(Object.assign(walkPose(r, s.dist || 0), { carry: true }));
    },
  },
  walk: {
    init(r, o) {
      const tx = r.stage.pickTarget(r), far = Math.abs(tx - r.x) > 80;
      return { tx, fast: (o && o.fast) || 1, tripAt: r.mood === "busy" && far && r.rand() < 0.15 ? between(r.rand, 0.8, 2) : null };
    },
    run(r, s, dt) {
      const speed = r.a.speed * s.fast * (r.caffeine > 0 ? 1.9 : 1);
      if (stepToward(r, s, s.tx, dt, speed)) { s.done = true; return stand({ carry: true }); }
      if (s.tripAt && s.t > s.tripAt) { s.tripAt = null; r.next = ["walk"]; r.begin("trip"); return stand(); }
      r.y = r.stage.hop(r.x);
      return stand(Object.assign(walkPose(r, s.dist), { carry: true }, r.caffeine > 0 ? { eyes: "wide" } : {}));
    },
  },
  idle: {
    init(r) { return { dur: between(r.rand, 1.2, 3.5), turn: r.rand() < 0.5 ? between(r.rand, 0.4, 1) : -1, tap: r.rand() < 0.4 }; },
    run(r, s) {
      if (s.turn > 0 && s.t > s.turn) { r.face *= -1; s.turn = -1; }
      if (s.t >= s.dur) s.done = true;
      return stand({ carry: true, liftF: s.tap && Math.floor(s.t * 4) % 2 ? 1 : 0, eyes: s.t % 2.3 < 0.12 ? "closed" : "open" });
    },
  },
  read: {
    init(r) { return { dur: between(r.rand, 3, 6), slam: r.rand() < 0.35 }; },
    run(r, s) {
      if (s.t > 1.2 && !s.said && r.rand() < 0.02) { s.said = true; r.quip("read"); }
      if (s.t >= s.dur) { if (s.slam) r.stage.dust(r.x + r.face * 8, 10); s.done = true; }
      const flip = s.t % 1.1 < 0.25;
      return stand({
        armF: ["high", 0], armB: ["high", -1], eyes: "down", nod: Math.floor(s.t * 1.5) % 2,
        props: (P, g) => {
          PROPS.book(P, r.a, g.handF[0], g.handF[1]);
          if (flip) box(P, g.handF[0] + 1 + Math.floor((s.t % 1.1) / 0.06) % 4, g.handF[1] + 6, 2, 2, "#ffffff");
        },
      });
    },
  },
  search: {
    init(r) {
      roomAhead(r, 40);
      const sniff = r.rand() < 0.3;
      const tx = r.stage.kind === "pill" ? r.x : r.x + r.face * between(r.rand, 16, 36);  // pills are too short to roam
      return { dur: between(r.rand, 3.5, 6), sniff, tx, found: r.rand() < 0.55 };
    },
    run(r, s, dt) {
      const pause = Math.floor(s.t / 0.9) % 3 === 2;
      if (!pause) stepToward(r, s, s.tx, dt, r.a.speed * 0.3);
      const end = s.t >= s.dur - 0.9;
      if (end && s.found && !s.cheered) { s.cheered = true; r.icon("bang"); r.quip("search"); }
      if (s.sniff && Math.floor(s.t * 3) !== Math.floor((s.t - 0.016) * 3)) r.stage.puff(r.x + r.face * 10, r.headTop - 12, r.face);
      if (s.sniff && !s.saidSniff && s.t > 1) { s.saidSniff = true; r.quip("sniff"); }
      if (s.t >= s.dur) s.done = true;
      const walk = pause || end ? {} : walkPose(r, s.dist || 0);
      if (s.sniff) return stand(Object.assign({}, walk, { crouch: 2, nod: 1, eyes: "closed", armF: ["low", 0], armB: ["low", 0] }));
      return stand(Object.assign({}, walk, {
        crouch: 1, armF: end && s.found ? ["up", 0] : ["low", 0], eyes: "down",
        props: (P, g) => end && s.found ? propPaper(P, g.handF[0] - 2, g.handF[1] + 1)
          : PROPS.magnifier(P, r.a, g.handF[0], g.handF[1], Math.floor(s.t * 2) % 4 === 0),
      }));
    },
  },
  eureka: {
    init(r) { r.icon("bulb", 1.6); return { dur: 1.8 }; },
    run(r, s) {
      if (s.t > 0.35 && !s.said) { s.said = true; r.quip("eureka"); r.stage.sparkle(r.x, r.headTop, 8); }
      const j = s.t > 0.35 ? Math.abs(Math.sin((s.t - 0.35) * 7)) : 0;
      r.y = j * 10;
      if (s.t >= s.dur) { r.y = 0; s.done = true; }
      return stand({ armF: s.t > 0.35 ? ["up", 0] : ["swing", 0], armB: s.t > 0.35 ? ["up", 0] : ["swing", 0],
                     eyes: "wide", mouth: "o", blush: true, liftF: j > 0.2, liftB: j > 0.2 });
    },
  },
  confused: {
    init(r) { r.icon("q", 2.2); return { dur: 2.4 }; },
    run(r, s) {
      if (s.t > 0.6 && !s.turned) { s.turned = true; r.face *= -1; }
      if (s.t > 1.2 && !s.turned2) { s.turned2 = true; r.face *= -1; if (r.rand() < 0.6) r.quip("confused"); }
      if (s.t >= s.dur) s.done = true;
      return stand({ armF: ["head", Math.floor(s.t * 8) % 2], mouth: "flat" });
    },
  },
  citation: {
    init(r) { roomAhead(r, 20); r.quip("citation"); return { dur: 2.4 }; },
    run(r, s) {
      if (s.t >= s.dur) s.done = true;
      return stand({ armF: ["high", 2 + Math.floor(s.t * 3) % 2], mouth: Math.floor(s.t * 8) % 2 ? "o" : "flat", liftF: s.t < 0.3 ? 1 : 0 });
    },
  },
  coffee: {
    init(r) { return { dur: 3.2 }; },
    run(r, s) {
      const sip = s.t > 0.7 && s.t < 2.2;
      if (sip && Math.random() < 0.12) r.stage.steam(r.x + r.face * 8, r.headTop - 8);
      if (s.t > 2.3 && !s.said) { s.said = true; r.quip("coffee"); r.caffeine = 8; }
      if (s.t >= s.dur) s.done = true;
      return stand({
        armF: sip ? ["mouth", 0] : ["forward", 0], eyes: sip ? "closed" : s.t > 2.3 ? "wide" : "open",
        props: (P, g) => PROPS.mug(P, r.a, g.handF[0] + 1, g.handF[1] + (sip ? -1 : 2)),
      });
    },
  },
  notes: {
    init(r) { return { dur: between(r.rand, 3, 4.5), toss: r.rand() < 0.5 }; },
    run(r, s) {
      const lines = Math.floor(s.t * 1.5);
      if (s.t >= s.dur) {
        if (s.toss) { r.stage.paper(r.x - r.face * 4, r.headTop - 12, -r.face * 24, 36); r.quip("notes"); }
        s.done = true;
      }
      const wiggle = Math.floor(s.t * 10) % 2;
      return stand({
        armB: ["forward", 2], armF: ["forward", wiggle - 1], eyes: "down", nod: 1,
        props: (P, g) => { propClipboard(P, g.handB[0], g.handB[1], lines); propPencil(P, g.handF[0], g.handF[1] + wiggle); },
      });
    },
  },
  sleep: {
    init(r) { return { dur: between(r.rand, 4, 8) }; },
    run(r, s) {
      if (Math.floor(s.t / 0.9) !== Math.floor((s.t - 0.017) / 0.9)) r.stage.z(r.x + r.face * 4, r.headTop - 4);
      const wake = s.t > s.dur - 0.6;
      if (wake && !s.woke) { s.woke = true; r.icon("bang", 0.6); }
      if (s.t >= s.dur) s.done = true;
      r.y = wake ? Math.sin((s.t - (s.dur - 0.6)) / 0.6 * Math.PI) * 8 : 0;
      return wake ? stand({ eyes: "wide", mouth: "o", armF: ["up", 0], armB: ["up", 0] })
        : stand({ sit: true, eyes: "closed", nod: 1, armF: ["low", -2], mouth: Math.floor(s.t) % 3 === 0 ? "o" : "none" });
    },
  },
  dig: {
    init(r) { roomAhead(r, 28); return { dur: between(r.rand, 3.5, 5), hole: 0 }; },
    run(r, s) {
      const cycle = s.t % 0.7, down = cycle < 0.35, end = s.t > s.dur - 1;
      if (!end && cycle < 0.02) { r.stage.dirt(r.x + r.face * 18, r.face); s.hole = Math.min(10, s.hole + 2); }
      if (end && !s.found) { s.found = true; r.quip("dig"); r.stage.sparkle(r.x + r.face * 4, r.headTop, 6); }
      if (s.t >= s.dur) s.done = true;
      r.stage.hole(r.x + r.face * 18, s.hole);
      if (end) return stand({ armF: ["up", 0], eyes: "wide", mouth: "smile", props: (P, g) => propPaper(P, g.handF[0] - 2, g.handF[1] + 1) });
      return stand({ crouch: down ? 1 : 0, armF: ["low", down ? 1 : 0], armB: ["low", -1], eyes: "down",
                     props: (P, g) => propShovel(P, g.handF[0], g.handF[1], down) });
    },
  },
  telescope: {
    init(r) { roomAhead(r, 32); return { dur: between(r.rand, 2.8, 4), turn: r.rand() < 0.5 }; },
    run(r, s) {
      const ext = clamp(s.t / 0.6, 0, 1) * clamp((s.dur - s.t) / 0.4, 0, 1);
      if (s.turn && s.t > s.dur / 2 && !s.turned) { s.turned = true; r.face *= -1; }
      if (s.t > 1.2 && !s.said) { s.said = true; r.quip("telescope"); }
      if (s.t >= s.dur) s.done = true;
      return stand({ armF: ["high", 1], armB: ["high", 0], liftB: 1, props: (P, g) => propSpyglass(P, g, ext) });
    },
  },
  typing: {
    init(r) { roomAhead(r, 28); return { dur: between(r.rand, 3.5, 5.5), phone: r.a.prop === "phone" }; },
    run(r, s) {
      if (s.t > 1.5 && !s.said) { s.said = true; r.quip("typing"); }
      if (s.t >= s.dur) s.done = true;
      const k = Math.floor(s.t * 14) % 2;
      if (s.phone) {                              // thumbs a-blur on a phone
        if (Math.random() < 0.15) r.stage.bit(r.x + r.face * 12, r.headTop - 14);
        return stand({ armF: ["high", k], armB: ["forward", 3 - k], eyes: "down", nod: 1,
                       props: (P, g) => PROPS.phone(P, r.a, g.handF[0], g.handF[1]) });
      }
      if (s.t > 2 && Math.random() < 0.08) r.stage.smoke(r.x + r.face * 14, 18);
      if (Math.random() < 0.25) r.stage.bit(r.x + r.face * 10, 16);
      return stand({ sit: true, armF: ["low", 1 + k], armB: ["low", 2 - k], eyes: "down", props: P => propLapLaptop(P, 2, k) });
    },
  },
  papers: {
    init(r) { return { dur: 1.6 }; },
    run(r, s) {
      if (s.t > 0.25 && !s.thrown) {
        s.thrown = true; r.quip("papers");
        for (let i = 0; i < 9; i++) r.stage.paper(r.x, r.headTop - 4, between(r.rand, -28, 28), between(r.rand, 28, 52));
      }
      if (s.t >= s.dur) { s.done = true; r.next = ["walk", { fast: 2.2 }]; }
      return stand({ armF: s.t > 0.25 ? ["up", 0] : ["low", 0], armB: s.t > 0.25 ? ["up", 0] : ["low", 0],
                     mouth: "o", eyes: s.t > 0.25 ? "wide" : "open", crouch: s.t < 0.25 ? 1 : 0 });
    },
  },
  scroll: {
    init(r) { roomAhead(r, 48); return { dur: between(r.rand, 3.5, 5) }; },
    run(r, s) {
      const out = clamp(s.t / 1.3, 0, 1) * clamp((s.dur - s.t) / 0.25, 0, 1);
      if (s.t > 1.4 && !s.said) { s.said = true; r.quip("scroll"); }
      if (s.t >= s.dur) s.done = true;
      return stand({ armF: ["forward", 0], armB: ["forward", 1], eyes: "down", nod: 1,
                     props: (P, g) => { box(P, g.handF[0] - 1, g.handF[1] - 2, 3, 6, "#e2d2a0"); propScroll(P, g.handF[0] + 2, Math.round(out * 34)); } });
    },
  },
  stack: {
    init(r) {
      const n = 4 + Math.floor(r.rand() * 4);
      return { tx: r.stage.pickTarget(r, 100), n, colors: Array.from({ length: n }, () => pick(r.rand, BOOKS)), topple: r.rand() < 0.3 };
    },
    run(r, s, dt) {
      if (!s.arrived && stepToward(r, s, s.tx, dt, r.a.speed * 0.6)) { s.arrived = s.t; }
      r.y = r.stage.hop(r.x);
      if (s.t > 0.8 && !s.said) { s.said = true; r.quip("stack"); }
      if (s.arrived && s.topple && !s.fell) {
        s.fell = true; r.say(QUOTES.topple[0], 1); r.icon("bang", 0.8);
        for (let i = 0; i < s.n; i++) r.stage.book(r.x + r.face * 6, r.headTop - 6 + i * 4, r.face * between(r.rand, 8, 28), between(r.rand, 8, 24), s.colors[i]);
      }
      if (s.arrived && s.t - s.arrived > 0.8) s.done = true;
      const walking = !s.arrived;
      return stand(Object.assign(walking ? walkPose(r, s.dist || 0) : {}, {
        armF: ["high", -1], armB: ["high", -2], eyes: s.fell ? "wide" : "open", mouth: s.fell ? "o" : "none",
        props: s.fell ? null : (P, g) => propStack(P, g.handF[0], g.handF[1] + 2, s.n, s.t * 5, s.colors),
      }));
    },
  },
  trip: {
    init(r) { r.quip("trip"); return { dur: 1.9 }; },
    run(r, s) {
      r.tilt = s.t < 0.18 ? s.t / 0.18 : s.t < 1.3 ? 1 : clamp(1 - (s.t - 1.3) / 0.3, 0, 1);
      if (s.t > 0.18 && !s.thud) { s.thud = true; r.stage.dust(r.x + r.face * 20, 10); }
      if (s.t > 0.3 && s.t < 1.3 && Math.random() < 0.15) r.stage.star(r.x + r.face * 38, 12);
      if (s.t >= s.dur) { r.tilt = 0; s.done = true; }
      return stand({ eyes: s.t < 1.3 ? "closed" : "open", armF: ["up", 0], armB: ["up", 0], mouth: "o" });
    },
  },
  wave: {
    init(r) { r.quip("wave"); if (r.rand() < 0.35) r.icon("heart", 1.2); return { dur: 1.5 }; },
    run(r, s) {
      if (s.t >= s.dur) s.done = true;
      return stand({ armF: ["up", Math.floor(s.t * 6) % 2 ? 2 : 0], mouth: "smile", blush: true });
    },
  },
};

const MOODS = {
  // the mission an agent is working on right now
  busy: { read: 2, search: 3, notes: 2, dig: 2, typing: 2, eureka: 1.2, confused: 1, citation: 1.5,
          telescope: 1, papers: 1, stack: 1.2, coffee: 1, scroll: 1.2, idle: 0.6 },
  // in progress, waiting for its turn in a running loop
  waiting: { idle: 2, read: 2, coffee: 2, notes: 1, telescope: 1, confused: 1, citation: 1, sleep: 1,
             stack: 1, typing: 1, search: 1, scroll: 1, dig: 0.6 },
  // the loop is off: nothing is moving, so mostly naps
  idle: { idle: 2, sleep: 4, coffee: 2, read: 1, confused: 0.6, telescope: 0.4 },
  // standing by their worker's card on the Settings page: mostly still, now and then a little bit of business
  crew: { idle: 6, read: 1.2, notes: 1, coffee: 1.2, typing: 0.8, citation: 0.6, scroll: 0.6, confused: 0.4,
          eureka: 0.4, telescope: 0.4, sleep: 0.5 },
};

// ---------- who visits next: a shuffled roster, so everyone gets a turn ----------

const BAG = "researcher:bag";
function nextCharacter(busy, worker) {           // worker: only that crew; none: anyone
  const key = BAG + (worker ? ":" + worker : ""), crew = crewOf(worker);
  let bag = (memory.get(key) || []).filter(k => crew.includes(k));
  if (!bag.length) bag = shuffle(crew);
  let i = bag.findIndex(k => !busy.has(k));      // skip anyone already on stage elsewhere
  if (i < 0) i = 0;
  const [kind] = bag.splice(i, 1);
  memory.set(key, bag);
  return kind;
}

// ---------- a researcher ----------

class Researcher {
  constructor(stage, rec, carry) {
    this.stage = stage; this.mood = stage.mood;
    this.a = makeLook(rec.kind, rec.seed);
    this.rand = rng(rec.seed ^ 0x5bd1e995);
    const [lo, hi] = stage.range();
    this.x = carry && carry.x != null ? clamp(carry.x, lo, hi) : stage.spawnX(this.rand);
    this.face = carry && carry.face ? carry.face : (this.rand() < 0.5 ? 1 : -1);
    this.y = 0; this.alpha = 1; this.squash = 1; this.tilt = 0; this.caffeine = 0;
    this.bubble = null; this.badge = null; this.headTop = 48; this.topY = 52; this.prev = null; this.clock = 0;
    this.quipIn = stage.mood === "crew" ? between(this.rand, 6, 40) : between(this.rand, 4, 9);
    this.begin(carry ? "idle" : "spawn");
  }
  say(text, dur) {
    if (this.stage.kind === "crew" && this.stage.talking(this)) return;   // a crew takes turns to speak
    this.bubble = { text, t: 0, dur: dur || readTime(text), y: null, lines: null }; }
  quip(key) {                                    // a line for the bit, or one from the big library
    const own = QUOTES[key];
    this.say(own && this.rand() < 0.4 ? pick(this.rand, own) : nextQuip());
  }
  icon(kind, dur = 1.2) { this.badge = { kind, t: 0, dur }; }
  begin(name, opts) {
    this.prev = this.act && this.act.name;
    this.act = Object.assign({ name, t: 0 }, ACTIONS[name].init ? ACTIONS[name].init(this, opts) : {});
  }
  chooseNext() {
    if (this.next) { const [n, o] = this.next; this.next = null; return this.begin(n, o); }
    const after = this.act.name;
    if (after !== "walk" && after !== "stack" && this.rand() < (this.mood === "crew" ? 0.18 : 0.55)) return this.begin("walk");
    const table = Object.assign({}, MOODS[this.mood] || MOODS.busy);
    for (const k in this.a.fav) if (k in table || !["idle", "crew"].includes(this.mood)) table[k] = (table[k] || 0.5) * this.a.fav[k];
    delete table[after];
    this.begin(weighted(this.rand, table));
  }
  leave() {
    if (this.leaving) return;
    this.leaving = true; this.tilt = 0; this.y = 0; this.badge = null; this.caffeine = 0;
    this.begin("despawn");
  }
  poke() {
    if (this.leaving || ["spawn", "trip", "wave", "despawn", "sleep"].includes(this.act.name)) return;
    this.y = 0; this.begin("wave");
  }
  update(dt) {
    this.clock += dt;
    this.caffeine = Math.max(0, this.caffeine - dt);
    if (this.bubble && (this.bubble.t += dt) > this.bubble.dur) this.bubble = null;
    if (this.badge && (this.badge.t += dt) > this.badge.dur) this.badge = null;
    if ((this.quipIn -= dt) <= 0) {               // now and then, an unprompted remark
      this.quipIn = this.mood === "crew" ? between(this.rand, 25, 60) : between(this.rand, 8, 16);
      if (!this.bubble && !this.badge && !this.leaving && ["walk", "idle"].includes(this.act.name)) this.say(nextQuip());
    }
    const act = this.act;
    act.t += dt;
    let pose = ACTIONS[act.name].run(this, act, dt);
    const prop = this.a.prop;
    if (pose.carry && prop && !pose.props) {      // keep holding the signature prop
      pose = Object.assign({}, pose);
      if (CARRY[prop]) pose.armF = CARRY[prop];
      pose.props = (P, g) => PROPS[prop](P, this.a, g.handF[0], g.handF[1]);
    }
    this.pose = pose;
    if (this.act === act && act.done && !this.gone) this.chooseNext();
  }
}

// ---------- stages: a canvas laid over a card or the agent pills ----------

const OFF_W = 120, OFF_H = 96, OX = 60, OY = 86;
const PILL_HEADROOM = 84;   // the offscreen sprite canvas and the feet's place in it
const stages = [];

class Stage {
  constructor(host, kind) {
    this.host = host; this.kind = kind;
    this.key = "researcher:" + (kind === "pill" ? "pill" : kind === "crew" ? "crew:" + host.dataset.crew : host.getAttribute("href"));
    this.mood = kind === "pill" ? "busy" : kind === "crew" ? "crew" : host.dataset.lab || "busy";
    const lit = kind === "pill" && host.querySelector(".agent.on[data-worker]");
    this.worker = (lit ? lit.dataset.worker : host.dataset.worker) || null;   // whose crew visits
    this.canvas = document.createElement("canvas");
    this.canvas.className = "lab-stage";
    this.canvas.setAttribute("aria-hidden", "true");
    host.appendChild(this.canvas);
    this.ctx = this.canvas.getContext("2d");
    this.off = this.layer(); this.ink = this.layer(); this.comp = this.layer();
    this.fx = []; this.marks = [];
    this.layout();
    if ("ResizeObserver" in window) new ResizeObserver(() => this.layout()).observe(host);
    host.addEventListener("mouseenter", () => this.resident && this.resident.poke());
    this.schedule();
  }
  layer() { const c = document.createElement("canvas"); c.width = OFF_W; c.height = OFF_H; return c.getContext("2d"); }
  layout() {
    const dpr = window.devicePixelRatio || 1;
    if (!this.host.isConnected || !this.host.clientWidth) return;   // detached or hidden: keep the last layout
    if (this.kind === "pill") {
      const pills = [...this.host.querySelectorAll(".agent")];
      const on = this.host.querySelector(".agent.on") || pills[0];
      if (!on) return;
      const row = pills.filter(p => Math.abs(p.offsetTop - on.offsetTop) < 4);
      this.canvas.style.top = -PILL_HEADROOM + "px";           // room above for speech bubbles
      this.canvas.style.height = on.offsetTop + PILL_HEADROOM + "px";
      this.cssW = this.host.clientWidth; this.cssH = on.offsetTop + PILL_HEADROOM;
      this.platforms = row.map(p => [p.offsetLeft, p.offsetLeft + p.offsetWidth]);
      this.home = [on.offsetLeft, on.offsetLeft + on.offsetWidth];
    } else {
      this.cssW = this.host.clientWidth; this.cssH = this.canvas.clientHeight || 100;
    }
    this.W = Math.floor(this.cssW); this.H = Math.floor(this.cssH);
    this.ground = this.kind === "pill" ? this.H - 1 : this.H - 9;     // the row the soles stand on
    this.canvas.width = Math.round(this.cssW * dpr); this.canvas.height = Math.round(this.cssH * dpr);
    this.dpr = dpr;
    if (this.resident) { const [lo, hi] = this.range(); this.resident.x = clamp(this.resident.x, lo - 40, hi + 40); }
    if (REDUCE) this.draw();
  }
  range() {
    if (this.kind === "pill") {
      const p = this.platforms || [[8, this.W - 8]];
      return [p[0][0] + 6, p[p.length - 1][1] - 6];
    }
    return [20, this.W - 20];
  }
  edgeFade(x) {                                  // fade in and out over the last stretch off-stage
    const [lo, hi] = this.range();
    return clamp(Math.min(x - (lo - EDGE), hi + EDGE - x) / 26, 0, 1);
  }
  spawnX(r) {
    if (this.kind === "pill" && this.home) return between(r, this.home[0] + 8, this.home[1] - 8);
    const [lo, hi] = this.range();
    return between(r, lo + 20, hi - 20);
  }
  pickTarget(r, max = 220) {
    if (this.kind === "pill") {                   // somewhere on a nearby pill, never in the gap between two
      const near = (this.platforms || []).map(p => [Math.max(p[0] + 6, r.x - max), Math.min(p[1] - 6, r.x + max)])
        .filter(([a, b]) => b > a);
      const home = this.home && [this.home[0] + 6, this.home[1] - 6];
      const [a, b] = home && (r.rand() < 0.65 || !near.length) ? home : pick(r.rand, near);
      return between(r.rand, a, b);
    }
    const [lo, hi] = this.range();
    let x = between(r.rand, lo, hi);
    if (Math.abs(x - r.x) < 24) x = r.x + (r.rand() < 0.5 ? -1 : 1) * between(r.rand, 36, 100);
    return clamp(clamp(x, r.x - max, r.x + max), lo, hi);   // a stroll, not a cross-country hike
  }
  hop(x) {                                         // jump over the gaps between pills
    if (this.kind !== "pill" || !this.platforms) return 0;
    for (let i = 0; i + 1 < this.platforms.length; i++) {
      const a = this.platforms[i][1] - 2, b = this.platforms[i + 1][0] + 2;
      if (x > a && x < b) return Math.sin((x - a) / (b - a) * Math.PI) * 12;
    }
    return 0;
  }
  schedule() {
    const t = now();
    let rec = memory.get(this.key);
    if (rec && (!(rec.life <= LIFE_MAX) || !ROSTER[rec.kind])) rec = null;
    if (rec && rec.kind && this.worker && ROSTER[rec.kind].worker !== this.worker) rec = null;   // the step changed hands
    const out = stages.filter(s => s !== this && s.resident).length;
    if (rec && t < rec.born + rec.life - 1 && out < MAX_ON_STAGE) this.spawn(rec, true);
    else if (rec && rec.next && t < rec.next) this.nextAt = rec.next;
    else this.nextAt = t + between(Math.random, FIRST_MIN, FIRST_MAX);
  }
  spawn(rec, resume) {
    if (!rec) {
      const busy = new Set(stages.filter(s => s !== this && s.resident).map(s => s.resident.a.kind));
      rec = { kind: nextCharacter(busy, this.worker), seed: Math.floor(Math.random() * 2 ** 31), born: now(),
              life: between(Math.random, LIFE_MIN, LIFE_MAX) };
      memory.set(this.key, rec);
    }
    this.rec = rec; this.nextAt = null;
    this.resident = new Researcher(this, rec, resume ? rec : null);
  }
  save() {
    if (this.resident && this.rec) memory.set(this.key, Object.assign({}, this.rec, { x: this.resident.x, face: this.resident.face }));
    else if (this.nextAt) memory.set(this.key, { next: this.nextAt, life: 0 });
  }
  update(dt) {
    const t = now(), r = this.resident;
    if (r) {
      if (!r.leaving && t >= this.rec.born + this.rec.life) r.leave();
      r.update(dt);
      if (r.gone) { this.resident = null; this.nextAt = t + between(Math.random, GAP_MIN, GAP_MAX); this.fx.length = 0; }
    }
    for (const p of this.fx) {
      p.life -= dt; p.vy -= (p.g || 0) * dt;
      p.x += p.vx * dt + (p.sway ? Math.sin(p.life * p.sway) * 0.6 : 0); p.y += p.vy * dt;
      if (p.floor && p.y <= 0) { p.y = 0; p.vx *= 0.5; p.vy = 0; p.g = 0; }
    }
    this.fx = this.fx.filter(p => p.life > 0);
    this.marks = this.marks.filter(m => (m.life -= dt) > 0);
  }

  // effects; x is in pixels from the left, y in pixels up from the floor
  add(p) { if (this.fx.length < 140) this.fx.push(Object.assign({ vx: 0, vy: 0, size: 2, max: p.life }, p)); }
  dust(x, n) { for (let i = 0; i < n; i++) this.add({ kind: "smoke", x, y: 2, vx: (Math.random() - 0.5) * 60, vy: Math.random() * 16, life: 0.5, size: 4 }); }
  dirt(x, face) { for (let i = 0; i < 6; i++) this.add({ kind: "px", c: "#7a5230", x, y: 2, vx: -face * (12 + Math.random() * 28), vy: 36 + Math.random() * 20, g: 120, life: 0.9, floor: true }); }
  steam(x, y) { this.add({ kind: "px", c: "#d5d8dc", x: x + (Math.random() - 0.5) * 4, y, vy: 12, life: 1, sway: 6, fade: true }); }
  smoke(x, y) { this.add({ kind: "smoke", x, y, vx: (Math.random() - 0.5) * 8, vy: 16, life: 1.2, size: 4 }); }
  bit(x, y) { this.add({ kind: "px", c: Math.random() < 0.5 ? "#2fc48d" : "#5fa8ff", x: x + (Math.random() - 0.5) * 8, y, vx: (Math.random() - 0.5) * 12, vy: 24, life: 0.6, fade: true }); }
  puff(x, y, face) { this.add({ kind: "px", c: "#c8ccd2", x, y, vx: face * 12, vy: 6, life: 0.4, fade: true }); }
  z(x, y) { this.add({ kind: "z", x, y, vx: 6, vy: 10, life: 2.2, sway: 3, fade: true }); }
  star(x, y) { this.add({ kind: "star", x: x + (Math.random() - 0.5) * 16, y: y + Math.random() * 6, vy: 8, life: 0.6, fade: true }); }
  sparkle(x, y, n) { for (let i = 0; i < n; i++) this.add({ kind: "star", x: x + (Math.random() - 0.5) * 36, y: y + (Math.random() - 0.5) * 20, vy: 12, life: 0.5 + Math.random() * 0.5, fade: true }); }
  paper(x, y, vx, vy) { this.add({ kind: "paper", x, y, vx, vy, g: 60, life: 2.6, sway: 8, floor: true, fade: true }); }
  book(x, y, vx, vy, c) { this.add({ kind: "book", c, x, y, vx, vy, g: 140, life: 2.2, floor: true, fade: true }); }
  hole(x, n) { this.marks.push({ kind: "hole", x, n, life: 0.05 }); }

  // ---------- drawing ----------
  draw() {
    const ctx = this.ctx, k = this.dpr;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    ctx.imageSmoothingEnabled = false;
    ctx.setTransform(k, 0, 0, k, 0, 0);
    const G = this.ground;
    for (const m of this.marks) {
      if (m.kind === "hole") { ctx.fillStyle = "rgba(60,40,25,.55)"; ctx.fillRect(Math.round(m.x - m.n), G - 1, m.n * 2 + 1, 3); }
    }
    const r = this.resident;
    if (r && r.pose) this.drawResearcher(ctx, r);
    for (const p of this.fx) this.drawFx(ctx, p);
    if (r) this.drawOverlay(ctx, r);
  }
  drawResearcher(ctx, r) {
    const { off, ink, comp } = this, p = r.pose;
    off.clearRect(0, 0, OFF_W, OFF_H);
    const paint = painter(off, OX, OY, r.face);
    let peak = 0;
    const P = (x, y, w, h, c) => { if (c && w > 0 && h > 0 && y + h > peak) peak = y + h; paint(x, y, w, h, c); };
    const g = drawBody(P, r.a, p, r.clock);
    if (p.props) {
      p.props(P, g);
      const [hx, hy] = g.handF; P(hx - 1, hy - 1, 3, 3, r.a.skin);   // fingers in front of what they hold
    }
    r.headTop = g.crown + 2 + Math.round(r.y);
    r.topY = peak + 1 + Math.round(r.y);                        // the highest pixel, props and raised arms included
    // a one-pixel dark outline around the whole silhouette, like the hand-drawn sprites
    ink.globalCompositeOperation = "source-over";
    ink.clearRect(0, 0, OFF_W, OFF_H); ink.drawImage(off.canvas, 0, 0);
    ink.globalCompositeOperation = "source-in"; ink.fillStyle = INK; ink.fillRect(0, 0, OFF_W, OFF_H);
    comp.clearRect(0, 0, OFF_W, OFF_H);
    for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) comp.drawImage(ink.canvas, dx, dy);
    comp.drawImage(off.canvas, 0, 0);
    const jit = r.caffeine > 0 ? Math.round(Math.random() * 2 - 1) : 0;
    const x = Math.round(r.x + jit), y = this.ground - Math.round(r.y);
    ctx.save();
    ctx.globalAlpha = clamp(r.alpha, 0, 1);
    ctx.translate(x, y + 1);
    if (r.tilt) { ctx.translate(0, -Math.round(r.tilt * 8)); ctx.rotate(r.face * r.tilt * Math.PI / 2); }
    if (r.squash !== 1) ctx.scale(1, r.squash);
    ctx.drawImage(comp.canvas, -OX, -OY - 1);
    ctx.restore();
    if (r.tilt > 0.9) { r.headTop = 16; r.topY = 18; }
  }
  drawFx(ctx, p) {
    const x = Math.round(p.x), y = this.ground - Math.round(p.y);
    ctx.globalAlpha = p.fade ? clamp(p.life / p.max * 1.5, 0, 1) : 1;
    switch (p.kind) {
      case "px": ctx.fillStyle = p.c; ctx.fillRect(x, y, p.size, p.size); break;
      case "smoke": {
        const s = Math.round(p.size + (1 - p.life / p.max) * 6);
        ctx.globalAlpha = clamp(p.life / p.max, 0, 1) * 0.8;
        ctx.fillStyle = "#e4e6ea"; ctx.fillRect(x - (s >> 1), y - (s >> 1), s, s);
        ctx.fillStyle = "#b8bcc4"; ctx.fillRect(x - (s >> 1), y + s - (s >> 1) - 2, s, 2);
        break;
      }
      case "z": { ctx.fillStyle = "#7aa7ff"; for (let r = 0; r < 5; r++) for (let c = 0; c < 3; c++) if (Z[r][c] === "z") ctx.fillRect(x + c * 2, y + r * 2 - 10, 2, 2); break; }
      case "star": ctx.fillStyle = "#ffd43b"; ctx.fillRect(x, y - 2, 2, 6); ctx.fillRect(x - 2, y, 6, 2); break;
      case "paper": {
        const flat = Math.floor(p.life * 6) % 2;
        ctx.fillStyle = INK; ctx.fillRect(x - 1, y - 1, flat ? 8 : 5, flat ? 5 : 8);
        ctx.fillStyle = "#ffffff"; ctx.fillRect(x, y, flat ? 6 : 3, flat ? 3 : 6);
        break;
      }
      case "book": ctx.fillStyle = INK; ctx.fillRect(x - 1, y - 5, 12, 6); ctx.fillStyle = p.c; ctx.fillRect(x, y - 4, 10, 4); break;
    }
    ctx.globalAlpha = 1;
  }
  drawOverlay(ctx, r) {
    const top = this.ground - r.topY;
    if (r.badge) {
      const map = ICONS[r.badge.kind], bounce = Math.round(Math.abs(Math.sin(r.badge.t * 6)) * 3);
      const blink = r.badge.kind === "bulb" && Math.floor(r.badge.t * 8) % 2 && r.badge.t < 0.5;
      if (!blink) drawIcon(ctx, map, Math.round(r.x - map[0].length), top - map.length * 2 - 6 - bounce);
    }
    if (r.bubble && !r.badge) this.drawBubble(ctx, r, top);
  }
  drawBubble(ctx, r, top) {
    const css = getComputedStyle(document.documentElement);
    const bg = css.getPropertyValue("--bg").trim() || "#fff", fg = css.getPropertyValue("--fg").trim() || "#111";
    const b = r.bubble, LH = 12;
    ctx.font = BUBBLE_FONT;
    if (!b.lines) b.lines = wrapText(ctx, b.text, Math.min(BUBBLE_MAX, this.cssW - 16));
    const tw = Math.max(...b.lines.map(l => Math.ceil(ctx.measureText(l).width)));
    const w = Math.min(this.cssW - 2, Math.ceil((tw + 12) / 2) * 2), h = b.lines.length * LH + 6;
    // Sit clear above the highest pixel of the researcher, and only ever drift up, never down onto the head.
    const want = top - h - 10;
    b.y = b.y == null ? want : Math.min(b.y, want);
    const ty = Math.max(1, Math.round(b.y));
    const cx = Math.round(r.x), x = Math.round(clamp(cx - w / 2, 1, this.cssW - w - 1));
    ctx.globalAlpha = Math.min(1, b.t / 0.12) * Math.min(1, (b.dur - b.t) / 0.2 + 0.2);
    ctx.fillStyle = fg;
    ctx.fillRect(x + 2, ty, w - 4, h); ctx.fillRect(x, ty + 2, w, h - 4);
    ctx.fillStyle = bg;
    ctx.fillRect(x + 2, ty + 2, w - 4, h - 4);
    const tx = Math.round(clamp(cx, x + 6, x + w - 8) / 2) * 2;   // the tail points down at the speaker
    ctx.fillStyle = fg; ctx.fillRect(tx - 2, ty + h, 6, 2); ctx.fillRect(tx, ty + h + 2, 2, 2);
    ctx.fillStyle = bg; ctx.fillRect(tx, ty + h - 2, 2, 2);
    ctx.fillStyle = fg; ctx.textBaseline = "middle";
    b.lines.forEach((line, i) => ctx.fillText(line, x + 6, ty + 3 + LH * i + LH / 2 + 0.5));
    ctx.globalAlpha = 1;
  }
}

// ---------- a crew: a worker's whole cast standing about on its Settings card ----------

class Crew extends Stage {
  constructor(host) {
    super(host, "crew");
    const crew = crewOf(host.dataset.crew);
    this.slots = crew.map((kind, i) => {
      const slot = Object.create(this);          // each researcher keeps to its own patch of floor
      slot.range = () => { const w = (this.W || 320) / crew.length; return [w * i + 10, w * (i + 1) - 10]; };
      const seed = [...kind].reduce((h, c) => Math.imul(h ^ c.charCodeAt(0), 16777619), 2166136261) >>> 0;
      const [lo, hi] = slot.range();
      const r = new Researcher(slot, { kind, seed }, { x: (lo + hi) / 2, face: i < crew.length / 2 ? 1 : -1 });
      r.act.t = -Math.random() * 3;               // so they don't all fidget in step
      return r;
    });
    host.addEventListener("mouseenter", () => this.slots.length && pick(Math.random, this.slots).poke());
  }
  schedule() {}                                   // always on stage: nobody visits, nobody leaves
  talking(who) { return (this.slots || []).some(r => r !== who && (r.bubble || r.badge)); }
  layout() {
    super.layout();
    for (const r of this.slots || []) { const [lo, hi] = r.stage.range(); r.x = clamp(r.x, lo, hi); }
  }
  update(dt) {
    for (const r of this.slots) r.update(dt);
    this.resident = null;
    super.update(dt);                             // effects and marks
  }
  draw() {
    const ctx = this.ctx, k = this.dpr;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    ctx.imageSmoothingEnabled = false;
    ctx.setTransform(k, 0, 0, k, 0, 0);
    for (const r of this.slots) if (r.pose) this.drawResearcher(ctx, r);
    for (const p of this.fx) this.drawFx(ctx, p);
    for (const r of this.slots) this.drawOverlay(ctx, r);
  }
}

const BUBBLE_FONT = "700 10px ui-monospace, SFMono-Regular, Menlo, monospace", BUBBLE_MAX = 232;
// Greedy word wrap; a word too long for a line gets broken rather than spilling out of the bubble.
function wrapText(ctx, text, max) {
  const lines = [];
  let line = "";
  for (let word of text.split(/\s+/)) {
    while (ctx.measureText(word).width > max) {
      let k = word.length - 1;
      while (k > 1 && ctx.measureText(word.slice(0, k) + "-").width > max) k--;
      if (line) { lines.push(line); line = ""; }
      lines.push(word.slice(0, k) + "-"); word = word.slice(k);
    }
    const next = line ? line + " " + word : word;
    if (line && ctx.measureText(next).width > max) { lines.push(line); line = word; } else line = next;
  }
  if (line) lines.push(line);
  return lines;
}

// ---------- start ----------

// Let waiting stages in, longest-waiting first, while fewer than MAX_ON_STAGE researchers are out.
function admit() {
  const t = now();
  let free = MAX_ON_STAGE - stages.filter(s => s.resident).length;
  if (free <= 0) return;
  const ready = stages.filter(s => !s.resident && s.nextAt && t >= s.nextAt).sort((a, b) => a.nextAt - b.nextAt);
  for (const s of ready) { if (free-- <= 0) break; s.spawn(); }
}

const crews = [];
function boot() {
  document.querySelectorAll(".card[data-lab]").forEach(c => stages.push(new Stage(c, "card")));
  const strip = document.querySelector(".agents[data-lab]");
  if (strip && strip.querySelector(".agent.on")) stages.push(new Stage(strip, "pill"));
  document.querySelectorAll(".crew[data-crew]").forEach(c => crews.push(new Crew(c)));
  if (!stages.length && !crews.length) return;
  addEventListener("pagehide", () => stages.forEach(s => s.save()));
  if (REDUCE) {                                   // one still researcher per stage, no wandering
    stages.slice(0, MAX_ON_STAGE).forEach(s => { if (!s.resident) s.spawn(null, true); s.resident.update(0); s.draw(); });
    crews.forEach(c => { c.slots.forEach(r => r.update(0)); c.draw(); });
    return;
  }
  let last = performance.now();
  const frame = t => {
    const dt = clamp((t - last) / 1000, 0, 0.05); last = t;
    admit();
    for (const s of stages) { s.update(dt); s.draw(); }
    for (const c of crews) { c.update(dt); c.draw(); }
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}
window.Researchers = { stages, crews, ROSTER, makeLook, ACTIONS, Stage, Researcher, Crew };   // handy from the console
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
