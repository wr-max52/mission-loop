/* The pipeline strip, kept live. Every two seconds it asks /api/flow what is happening and relights the
 * strip in place: the step working right now, the counts waiting at each human review and parked at each
 * check, and the line underneath. It never reloads the page, so the pixel researchers on the strip keep
 * walking. When the working step changes it fires a "flowchange" event on #flow saying where the work
 * went, which assets/researchers.js turns into a researcher chasing the work or handing it off.
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
    last = { step: el.dataset.step || null, slug: el.dataset.slug || null, waiting: {} };
    el.querySelectorAll(".human[data-step]").forEach(h => {
      last.waiting[h.dataset.step] = Number(h.querySelector(".count").textContent) || 0;
    });
  }
  if (!timer) {
    timer = setInterval(poll, EVERY);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  }
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
  const step = d.step && shown.has(d.step) ? d.step : null;
  root.classList.toggle("off", !d.running);
  root.querySelectorAll(".step[data-step], .check[data-step]").forEach(e => {
    const on = e.dataset.step === step;
    e.classList.toggle("on", on);
    if (on) e.setAttribute("aria-current", "step"); else e.removeAttribute("aria-current");
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

  const from = { step: last.step, slug: last.slug }, to = { step, slug: step ? d.slug : null };
  const moved = from.step !== to.step || from.slug !== to.slug, flipped = last.running !== undefined && last.running !== d.running;
  root.dataset.step = to.step || ""; root.dataset.slug = to.slug || "";
  last.step = to.step; last.slug = to.slug; last.running = d.running;
  if (moved) {
    const outcome = from.slug ? d.states[from.slug] || null : null;
    root.dispatchEvent(new CustomEvent("flowchange", { bubbles: true, detail: { from, to, outcome, running: d.running } }));
  }
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
