/* The pipeline strip, kept live. Every two seconds it asks /api/flow what is happening and relights the
 * strip in place: the steps working right now (one per busy worker), the counts waiting at each human review
 * and parked at each check, and the lines underneath. It never reloads the page, so the pixel researchers on
 * the strip keep walking. When a worker's step changes it fires a "flowchange" event on #flow saying which
 * worker and where its work went, which assets/researchers.js turns into that worker's researcher chasing
 * the work or handing it off.
 */
(() => {
"use strict";
if (window.Flow) return;

const EVERY = 2000;
let root = null, timer = null, last = null, busy = false;

function attach() {
  const el = document.getElementById("flow");
  if (!el) return;
  if (el !== root) {
    root = el;
    last = { working: byWorker(JSON.parse(el.dataset.working || "[]")), waiting: {} };
    el.querySelectorAll(".human[data-step]").forEach(h => {
      last.waiting[h.dataset.step] = Number(h.querySelector(".count").textContent) || 0;
    });
  }
  if (!timer) {
    timer = setInterval(poll, EVERY);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  }
}

function byWorker(list) {                       // {worker: {step, slug}}
  const out = {};
  for (const e of list) out[e.worker] = { step: e.step, slug: e.slug };
  return out;
}

async function poll() {
  if (busy || document.hidden || !root || !root.isConnected) return;
  busy = true;
  try {
    const res = await fetch("/api/flow", { cache: "no-store" });
    if (res.ok) apply(await res.json());
  } catch (e) { /* the UI server is restarting; try again next time */ }
  busy = false;
}

function apply(d) {
  const shown = new Set([...root.querySelectorAll(".flow-track [data-step]")].map(e => e.dataset.step));
  const working = d.working.filter(e => shown.has(e.step)), lit = {};
  for (const e of working) lit[e.step] = e.worker;
  root.classList.toggle("off", !d.running);
  root.querySelectorAll(".step[data-step], .check[data-step]").forEach(e => {
    const w = lit[e.dataset.step];
    e.classList.toggle("on", !!w);
    if (w) { e.setAttribute("aria-current", "step"); e.dataset.worker = w; }
    else { e.removeAttribute("aria-current"); delete e.dataset.worker; }
  });
  root.querySelectorAll(".human[data-step]").forEach(h => {
    const key = h.dataset.step, n = d.waiting[key] || 0, count = h.querySelector(".count");
    count.textContent = n; count.hidden = !n;
    h.classList.toggle("waiting", n > 0);
    if (n > (last.waiting[key] || 0)) arrive(h);        // something just landed on your desk
    last.waiting[key] = n;
  });
  root.querySelectorAll(".check[data-step]").forEach(c => {
    const n = d.parked[c.dataset.step] || 0, b = c.querySelector(".parked-count");
    if (b) { b.textContent = n; b.hidden = !n; b.title = `${n} parked here`; }
  });
  const now = root.querySelector(".now");
  if (now && now.dataset.html !== d.now) { now.innerHTML = d.now; now.dataset.html = d.now; }

  // One event per worker whose step changed. A mission another worker has just picked up was handed over.
  const atWork = byWorker(working), flipped = last.running !== undefined && last.running !== d.running;
  root.dataset.working = JSON.stringify(working.map(e => ({ worker: e.worker, step: e.step, slug: e.slug })));
  let moved = false;
  for (const w of new Set([...Object.keys(last.working), ...Object.keys(atWork)])) {
    const was = last.working[w] || {}, is = atWork[w] || {};
    const from = { step: was.step || null, slug: was.slug ?? null }, to = { step: is.step || null, slug: is.slug ?? null };
    if (from.step === to.step && from.slug === to.slug) continue;
    moved = true;
    const outcome = from.slug ? d.states[from.slug] || null : null;
    const handoff = !!from.slug && working.some(e => e.worker !== w && e.slug === from.slug);
    root.dispatchEvent(new CustomEvent("flowchange", { bubbles: true, detail: { worker: w, from, to, outcome, handoff, running: d.running } }));
  }
  last.working = atWork; last.running = d.running;
  if ((moved || flipped) && window.checkPage) setTimeout(window.checkPage, 300);   // bring the rest of the page up to date too
}

function arrive(h) {
  h.classList.remove("arrived");
  void h.offsetWidth;                                     // restart the animation
  h.classList.add("arrived");
  setTimeout(() => h.classList.remove("arrived"), 3200);
}

window.Flow = { attach, poll };
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", attach); else attach();
})();
