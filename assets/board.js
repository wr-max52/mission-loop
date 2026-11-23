// The cork board (ui.board_page): every mission and epic as a sticky note, strung together. Pan by dragging the
// board, zoom with the wheel or a pinch, drag a note to move it (its corner resizes it). A mission note opens a
// preview; an epic note picks it, and its degree selector fades everything more than N strings away and glides to
// fit the rest. Polls /api/board and updates notes in place, so new missions appear on their own. No library.
(() => {
  const root = document.getElementById("board");
  if (!root) return;
  const world = root.querySelector(".world"), svg = root.querySelector("svg.strings");
  const pop = root.querySelector(".board-pop"), crumb = root.querySelector(".board-crumb");
  const zoomLabel = root.querySelector(".board-zoom");
  const NS = "http://www.w3.org/2000/svg";
  const tr = s => (window.I18N || {})[s] || s;     // the UI's language (ui.JS_STRINGS)
  const BASE = 160, MIN_Z = 0.12, MAX_Z = 2.6, GLIDE_MS = 520, CLICK_PX = 5;
  let data = JSON.parse(document.getElementById("board-data").textContent);
  const view = { x: 0, y: 0, z: 1 };             // screen = world * z + (x, y)
  const els = new Map();                          // note id -> element
  let picked = null, degree = 1, opened = null, glideId = 0, sig = data.sig;

  const byId = id => data.notes.find(n => n.id === id);
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const tilt = id => { let h = 0; for (const c of id) h = (h * 31 + c.charCodeAt(0)) | 0; return ((Math.abs(h) % 9) - 4) * 0.7; };

  // ---------- view ----------
  const MOVING = new Set(["grid", "dark", "arcade"]);   // surfaces whose pattern pans and zooms with the notes
  function apply() {
    world.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.z})`;
    if (data.style && MOVING.has(data.style.surface)) {  // only these read --gx/--gy/--gz (never inherited, see BOARD_CSS)
      root.style.setProperty("--gx", view.x + "px"); root.style.setProperty("--gy", view.y + "px");
      root.style.setProperty("--gz", view.z);
    }
    const pct = Math.round(view.z * 100) + "%";
    if (zoomLabel && zoomLabel.textContent !== pct) zoomLabel.textContent = pct;
    placePop();
  }
  const clampZ = z => Math.min(MAX_Z, Math.max(MIN_Z, z));
  function zoomAt(px, py, factor) {
    const z = clampZ(view.z * factor), k = z / view.z;
    view.x = px - (px - view.x) * k; view.y = py - (py - view.y) * k; view.z = z;
    apply();
  }
  function glide(to, ms = GLIDE_MS) {             // the world point at the centre and the zoom move smoothly
    const w = root.clientWidth, h = root.clientHeight, id = ++glideId;
    const from = { cx: (w / 2 - view.x) / view.z, cy: (h / 2 - view.y) / view.z, z: view.z };
    const target = { cx: (w / 2 - to.x) / to.z, cy: (h / 2 - to.y) / to.z, z: to.z };
    const t0 = performance.now(), ease = t => t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
    const step = now => {
      if (id !== glideId) return;
      const t = Math.min(1, (now - t0) / ms), e = ease(t);
      const z = Math.exp(Math.log(from.z) + (Math.log(target.z) - Math.log(from.z)) * e);
      const cx = from.cx + (target.cx - from.cx) * e, cy = from.cy + (target.cy - from.cy) * e;
      view.z = z; view.x = w / 2 - cx * z; view.y = h / 2 - cy * z;
      apply();
      if (t < 1) requestAnimationFrame(step);
    };
    if (ms <= 0) { Object.assign(view, to); apply(); } else requestAnimationFrame(step);
  }
  function box(ids) {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const id of ids) {
      const n = byId(id), el = els.get(id);
      if (!n || !el) continue;
      const w = el.offsetWidth / 2, h = el.offsetHeight / 2;
      x0 = Math.min(x0, n.x - w); x1 = Math.max(x1, n.x + w); y0 = Math.min(y0, n.y - h); y1 = Math.max(y1, n.y + h);
    }
    return isFinite(x0) ? { x0, y0, x1, y1 } : null;
  }
  function fit(ids, ms = GLIDE_MS, most = 1.4) {
    const b = box(ids);
    if (!b) return;
    const pad = 70, padY = 95, w = root.clientWidth, h = root.clientHeight;   // room for the bars above and below
    const z = clampZ(Math.min((w - pad * 2) / Math.max(1, b.x1 - b.x0), (h - padY * 2) / Math.max(1, b.y1 - b.y0), most));
    const cx = (b.x0 + b.x1) / 2, cy = (b.y0 + b.y1) / 2;
    glide({ z, x: w / 2 - cx * z, y: h / 2 - cy * z }, ms);
  }

  // ---------- notes ----------
  const hue = id => { let h = 7; for (const c of id) h = (h * 33 + c.charCodeAt(0)) % 360; return h; };
  const RAINBOW = [4, 28, 48, 140, 205, 265];      // alphabet magnet colours, in order
  function alphaWord(text) {                       // the word the alphabet magnets spell: the first strong one
    const words = String(text).replace(/[^A-Za-z0-9 ]/g, " ").split(/\s+/).filter(Boolean);
    const w = words.find(x => x.length >= 4) || words[0] || "?";
    return w.toUpperCase().slice(0, 8);
  }
  function magnetHtml(n) {                         // missions grow from letters to word tiles to a postcard
    if (n.kind === "epic")
      return `<div class="souvenir" style="--h:${hue(n.slug)}"><svg class="horseshoe" viewBox="0 0 40 30" aria-hidden="true">`
        + `<path d="M6 2v12a14 14 0 0 0 28 0V2h-8v12a6 6 0 0 1-12 0V2z"/><path class="tip" d="M6 2h8v5H6zM26 2h8v5h-8z"/></svg>`
        + `<div class="k">${n.proposed ? tr("Proposed epic") : tr("Epic")}</div><div class="t">${esc(n.title)}</div>`
        + `<div class="n">${esc(n.count_text)}</div></div>`;
    if (n.kind === "unsorted") return `<div class="t">${esc(n.title)}</div><div class="n">${esc(n.count_text)}</div>`;
    const live = n.live ? `<span class="now">${tr("working now")}</span>` : "", grip = `<span class="grip" title="${tr("Resize")}"></span>`;
    if (n.paper === "done" || n.paper === "publish") {
      const mark = n.paper === "done" ? tr("Published") : tr("Article ready");
      return `<div class="postcard"><div class="photo" style="--h:${hue(n.epic || n.slug)}"><span class="greet">${tr("Greetings from")}</span>`
        + `<span class="place">${esc(n.epic || tr("the desk"))}</span></div><div class="back"><div class="t">${esc(n.short)}</div>`
        + `<span class="stampbox"></span><span class="postmark">${mark}</span></div></div>${live}${grip}`;
    }
    if (n.paper === "queued" && !n.faded || n.paper === "research") {
      const letters = [...alphaWord(n.short)].map((c, i) =>
        `<b style="--h:${RAINBOW[i % RAINBOW.length]};--r:${((i * 37) % 11) - 5}deg">${esc(c)}</b>`).join("");
      return `<div class="alpha">${letters}</div><div class="tape">${esc(n.short)}</div>${live}${grip}`;
    }
    const words = String(n.short).split(/\s+/).slice(0, 10);
    const tiles = words.map((w, i) => `<i style="--r:${((i * 53) % 7) - 3}deg">${esc(w)}</i>`).join("");
    const tag = n.dead ? tr("Dead lead") : n.paper === "parked" ? tr("Parked") : n.faded ? tr("Combined") : n.stage;
    return `<div class="tiles">${tiles}<i class="tag">${esc(tag)}</i></div>${live}${grip}`;
  }
  function noteHtml(n) {
    if (data.style && data.style.cards === "magnets") return magnetHtml(n);
    if (n.kind === "epic")
      return `<span class="pin"></span><div class="k">${n.proposed ? tr("Proposed epic") : tr("Epic")}</div>`
        + `<div class="t">${esc(n.title)}</div><div class="n">${esc(n.count_text)}</div>`;
    if (n.kind === "unsorted") return `<div class="t">${esc(n.title)}</div><div class="n">${esc(n.count_text)}</div>`;
    const stamp = n.paper === "done" ? tr("Published") : n.dead ? tr("Dead lead") : n.paper === "parked" ? tr("Parked") : n.faded ? tr("Combined") : "";
    return `<span class="pin"></span><div class="t">${esc(n.short)}</div>`
      + (stamp ? `<span class="stamp">${stamp}</span>` : `<div class="stage">${esc(n.stage)}</div>`)
      + (n.live ? `<span class="now">${tr("working now")}</span>` : "") + `<span class="grip" title="${tr("Resize")}"></span>`;
  }
  function render() {
    const seen = new Set();
    for (const n of data.notes) {
      seen.add(n.id);
      let el = els.get(n.id);
      if (!el) {
        el = document.createElement("div");
        el.dataset.id = n.id;
        world.appendChild(el);
        els.set(n.id, el);
      }
      const html = noteHtml(n);
      if (el.dataset.html !== html) {
        const visitor = el.querySelector("canvas.lab-stage");      // a researcher standing on it stays
        el.innerHTML = html; el.dataset.html = html;
        if (visitor) el.appendChild(visitor);
      }
      if (n.live && data.animations) {                              // assets/researchers.js sends someone over
        el.dataset.lab = "busy";
        if (n.worker) el.dataset.worker = n.worker; else delete el.dataset.worker;
      } else { delete el.dataset.lab; delete el.dataset.worker; }
      const cls = ["note", n.kind, n.paper || "", n.live ? "live" : "", n.faded ? "combined" : "",
                   n.proposed ? "proposed" : ""].filter(Boolean).join(" ");
      if (el.className.replace(/ ?(faded|lifted)\b/g, "") !== cls)  // shade() and a drag own those two
        el.className = cls + (el.classList.contains("faded") ? " faded" : "");
      if (el !== dragEl) {
        set(el, "left", n.x + "px"); set(el, "top", n.y + "px"); set(el, "--s", String(n.size));
      }
      set(el, "--r", (n.kind === "unsorted" ? 0 : tilt(n.id)) + "deg");
      el.pinH = null;                                               // its height may have changed: measure again
    }
    for (const [id, el] of els) if (!seen.has(id)) { el.remove(); els.delete(id); }
    strings();
    shade();
    if (window.Researchers) window.Researchers.rescan();
  }
  function set(el, prop, value) {                  // a write only when it changes anything, so nothing is restyled for nothing
    if (el.style.getPropertyValue(prop) !== value) el.style.setProperty(prop, value);
  }
  function pinOf(id) {
    const n = byId(id), el = els.get(id);
    if (!el) return [n.x, n.y];
    if (el.pinH == null) el.pinH = el.offsetHeight;   // measured once per render, not once per string
    return [n.x, n.y - el.pinH / 2 + 12];
  }
  // Strings are kept, not rebuilt: each is one <g> keyed by its ends, and only a path whose ends moved is rewritten.
  // The svg is sized to the strings it holds, so its layer and its shadow are no bigger than the strings themselves.
  const strEls = new Map();
  let strKind = null;
  function strings(only) {                         // only: the note being dragged; strings not touching it stay as they are
    const kind = data.style ? data.style.string : "string";
    if (kind !== strKind) { svg.replaceChildren(); strEls.clear(); strKind = kind; }   // a new kind draws other paths
    const seen = new Set(), pad = 30;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const s of data.strings) {
      if (!els.has(s.a) || !els.has(s.b)) continue;
      const key = s.kind + "|" + s.a + "|" + s.b;
      seen.add(key);
      let g = strEls.get(key);
      if (!g || !only || s.a === only || s.b === only) {
        const [ax, ay] = pinOf(s.a), [bx, by] = pinOf(s.b), d = Math.hypot(bx - ax, by - ay);
        const sag = kind === "laser" ? 0 : (s.kind === "epic" ? .12 : s.kind === "link" ? .08 : .05) * (kind === "wire" ? .5 : 1);
        const my = Math.round((ay + by) / 2), cx = (ax + bx) / 2, cy = (ay + by) / 2 + d * sag;
        const path = kind === "pixel" ? `M${ax},${ay} V${my} H${bx} V${by}` : `M${ax},${ay} Q${cx},${cy} ${bx},${by}`;
        if (!g) {
          g = document.createElementNS(NS, "g");
          g.dataset.a = s.a; g.dataset.b = s.b;
          g.setAttribute("class", "str " + s.kind);
          for (const cls of kind === "string" || kind === "pixel" ? ["line", "hit"] : ["line", "core", "hit"]) {
            const p = document.createElementNS(NS, "path");
            p.setAttribute("class", cls);
            g.appendChild(p);
          }
          g.appendChild(document.createElementNS(NS, "title"));
          svg.appendChild(g);
          strEls.set(key, g);
        }
        if (g.path !== path) { for (const p of g.querySelectorAll("path")) p.setAttribute("d", path); g.path = path; }
        const why = s.why || "", t = g.querySelector("title");
        if (t.textContent !== why) t.textContent = why;
        g.box = [Math.min(ax, bx, cx), Math.min(ay, by, cy), Math.max(ax, bx, cx), Math.max(ay, by, cy)];  // the curve stays inside
      }
      x0 = Math.min(x0, g.box[0]); y0 = Math.min(y0, g.box[1]); x1 = Math.max(x1, g.box[2]); y1 = Math.max(y1, g.box[3]);
    }
    for (const [key, g] of strEls) if (!seen.has(key)) { g.remove(); strEls.delete(key); }
    if (!isFinite(x0)) { x0 = y0 = 0; x1 = y1 = 1; }
    x0 = Math.floor(x0 - pad); y0 = Math.floor(y0 - pad); x1 = Math.ceil(x1 + pad); y1 = Math.ceil(y1 + pad);
    const vb = `${x0} ${y0} ${x1 - x0} ${y1 - y0}`;
    if (svg.getAttribute("viewBox") !== vb) {
      svg.setAttribute("viewBox", vb);
      svg.style.left = x0 + "px"; svg.style.top = y0 + "px";
      svg.style.width = (x1 - x0) + "px"; svg.style.height = (y1 - y0) + "px";
    }
  }

  // ---------- picking an epic: degrees ----------
  function within(start, n) {                     // every note at most n strings from start
    const next = new Map();
    for (const s of data.strings) {
      (next.get(s.a) || next.set(s.a, []).get(s.a)).push(s.b);
      (next.get(s.b) || next.set(s.b, []).get(s.b)).push(s.a);
    }
    const dist = new Map([[start, 0]]), queue = [start];
    while (queue.length) {
      const at = queue.shift(), d = dist.get(at);
      if (d >= n) continue;
      for (const to of next.get(at) || []) if (!dist.has(to)) { dist.set(to, d + 1); queue.push(to); }
    }
    return new Set(dist.keys());
  }
  function shade() {
    const keep = picked && byId(picked) ? within(picked, degree) : null;
    for (const [id, el] of els) el.classList.toggle("faded", !!keep && !keep.has(id));
    for (const g of svg.children) g.classList.toggle("faded", !!keep && !(keep.has(g.dataset.a) && keep.has(g.dataset.b)));
    if (!crumb) return keep;
    if (!keep) { crumb.hidden = true; return keep; }
    const n = byId(picked);
    crumb.hidden = false;
    const shown = (keep.size === 2 ? tr("{n} note shown") : tr("{n} notes shown")).replace("{n}", keep.size - 1);
    crumb.innerHTML = `<span class="x">${tr("Board")}</span> › <b>${esc(n.title)}</b><span class="x">${esc(n.count_text)}</span>`
      + `<span class="x">· ${tr("Show up to")}</span>${degrees()}`
      + `<span class="x">${shown}</span>`
      + `<button type="button" class="clear" title="${tr("Show the whole board")}" aria-label="${tr("Show the whole board")}">✕</button>`;
    return keep;
  }
  const degrees = () => `<span class="deg">${[1, 2, 3, Infinity].map(d =>
    `<button type="button" data-degree="${d}" class="${d === degree ? "on" : ""}">${d === Infinity ? tr("All") : d}</button>`).join("")}</span>`;
  function pick(id, d = degree) {
    picked = id; degree = d;
    const keep = shade();
    fit([...keep]);
  }
  function unpick() { picked = null; shade(); closePop(); }

  // ---------- preview ----------
  function showPop(id) {
    const n = byId(id);
    if (!n || !pop) return;
    opened = id;
    if (n.kind === "mission") {
      pop.innerHTML = `<div class="pop-row"><span class="tag ${esc(n.paper)}">${esc(n.stage)}</span>`
        + (n.epic ? `<span class="epicpill">${esc(n.epic)}</span>` : "") + `</div>`
        + `<h3>${esc(n.title)}</h3><div class="lbl">${esc(n.thesis_label)}</div><p>${esc(n.thesis)}</p>`
        + `<div class="pop-row"><span class="meta">${esc(n.meta)}</span>`
        + `<a class="open" href="${esc(n.url)}">${esc(n.open)} ↗</a></div>`;
    } else {
      pop.innerHTML = `<div class="pop-row"><span class="tag">${n.kind === "epic" ? (n.proposed ? tr("Proposed epic") : tr("Epic")) : tr("No epic")}</span></div>`
        + `<h3>${esc(n.title)}</h3><p class="meta">${esc(n.count_text)}</p>`
        + `<div class="lbl">${tr("Show missions up to")}</div>${degrees()}`
        + `<p class="meta">${tr("1: this epic's missions. Each step adds one more string away.")}</p>`;
    }
    pop.hidden = false;
    placePop();
  }
  function closePop() { opened = null; if (pop) pop.hidden = true; }
  function placePop() {
    if (!pop || pop.hidden || !opened) return;
    const n = byId(opened), el = els.get(opened);
    if (!n || !el) return closePop();
    const w = el.offsetWidth * view.z / 2, sx = n.x * view.z + view.x, sy = n.y * view.z + view.y;
    const pw = pop.offsetWidth, ph = pop.offsetHeight, W = root.clientWidth, H = root.clientHeight;
    let left = sx + w + 16, side = "right";
    if (left + pw > W - 12) { left = sx - w - 16 - pw; side = "left"; }
    left = Math.max(12, Math.min(W - pw - 12, left));
    pop.style.left = left + "px";
    pop.style.top = Math.max(12, Math.min(H - ph - 12, sy - 60)) + "px";
    pop.dataset.side = side;
  }

  // ---------- pointer: pan, pinch, drag, resize, click ----------
  const pointers = new Map();
  let dragEl = null, gesture = null;
  const local = e => { const r = root.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  root.addEventListener("pointerdown", e => {
    if (e.target.closest("a, .board-pop, .board-tools, .board-crumb, .board-legend, .board-style") || e.button > 0) return;
    glideId++;                                    // a touch stops any glide
    pointers.set(e.pointerId, local(e));
    root.setPointerCapture(e.pointerId);
    if (pointers.size === 2) {                    // a second finger: pinch
      const [a, b] = [...pointers.values()];
      gesture = { kind: "pinch", d: Math.hypot(a[0] - b[0], a[1] - b[1]), mid: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2] };
      dragEl = null;
      return;
    }
    const noteEl = e.target.closest(".note");
    const start = local(e);
    if (noteEl && !noteEl.classList.contains("unsorted")) {
      const n = byId(noteEl.dataset.id);
      gesture = { kind: e.target.closest(".grip") ? "resize" : "note", id: n.id, start, x: n.x, y: n.y, size: n.size, moved: false };
    } else {
      gesture = { kind: "pan", start, x: view.x, y: view.y, moved: false };
    }
  });
  root.addEventListener("pointermove", e => {
    if (!pointers.has(e.pointerId) || !gesture) return;
    pointers.set(e.pointerId, local(e));
    const [px, py] = local(e);
    if (gesture.kind === "pinch" && pointers.size === 2) {
      const [a, b] = [...pointers.values()], d = Math.hypot(a[0] - b[0], a[1] - b[1]);
      const mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
      view.x += mid[0] - gesture.mid[0]; view.y += mid[1] - gesture.mid[1];
      zoomAt(mid[0], mid[1], d / gesture.d);
      gesture.d = d; gesture.mid = mid;
      return;
    }
    const dx = px - gesture.start[0], dy = py - gesture.start[1];
    if (!gesture.moved && Math.hypot(dx, dy) < CLICK_PX) return;
    gesture.moved = true;
    if (gesture.kind === "pan") {
      view.x = gesture.x + dx; view.y = gesture.y + dy; apply();
      return;
    }
    const n = byId(gesture.id), el = els.get(gesture.id);
    dragEl = el;
    el.classList.add("lifted");
    if (gesture.kind === "note") {
      n.x = gesture.x + dx / view.z; n.y = gesture.y + dy / view.z;
      el.style.left = n.x + "px"; el.style.top = n.y + "px";
    } else {
      n.size = Math.round(Math.min(data.size_max, Math.max(data.size_min, gesture.size + dx / view.z / BASE)) * 100) / 100;
      el.style.setProperty("--s", n.size);
      el.pinH = null;                               // a bigger note puts its pin higher
    }
    strings(n.id); placePop();                      // only its own strings move; what is faded stays faded
  });
  function end(e) {
    if (!pointers.has(e.pointerId)) return;
    pointers.delete(e.pointerId);
    const g = gesture;
    if (pointers.size > 0) return;                // one finger of a pinch lifted: wait for the other
    gesture = null;
    if (dragEl) dragEl.classList.remove("lifted");
    dragEl = null;
    if (!g || g.kind === "pinch") return;
    if (g.moved && g.kind !== "pan") return save(byId(g.id), g.kind === "resize");
    if (g.moved) return;
    if (g.kind === "pan") return unpick();        // a click on the empty board clears everything
    const n = byId(g.id);
    if (n.kind === "epic") { closePop(); pick(n.id); }
    else showPop(n.id);
  }
  root.addEventListener("pointerup", end);
  root.addEventListener("pointercancel", end);
  root.addEventListener("wheel", e => {
    if (e.target.closest(".board-pop, .board-style")) return;
    e.preventDefault();
    glideId++;
    const [px, py] = local(e);
    zoomAt(px, py, Math.exp(-e.deltaY * (e.ctrlKey ? 0.012 : 0.0016)));
  }, { passive: false });
  root.addEventListener("click", e => {
    const b = e.target.closest("[data-degree]");
    if (b && picked) return pick(picked, Number(b.dataset.degree));
    if (b && opened) return pick(opened, Number(b.dataset.degree));
    if (e.target.closest(".clear")) return unpick();
    const tool = e.target.closest("[data-tool]");
    if (!tool) return;
    const w = root.clientWidth / 2, h = root.clientHeight / 2, t = tool.dataset.tool;
    if (t === "in") glide({ ...zoomed(w, h, 1.4) }, 260);
    if (t === "out") glide({ ...zoomed(w, h, 1 / 1.4) }, 260);
    if (t === "fit") picked ? fit([...within(picked, degree)]) : fit([...els.keys()]);
    if (t === "style") toggleStyle();
  });
  function zoomed(px, py, factor) {
    const z = clampZ(view.z * factor), k = z / view.z;
    return { z, x: px - (px - view.x) * k, y: py - (py - view.y) * k };
  }
  document.addEventListener("keydown", e => {
    if (e.target.closest("input, textarea, select, [contenteditable]")) return;
    const w = root.clientWidth / 2, h = root.clientHeight / 2, k = {
      "Escape": () => { toggleStyle(false); unpick(); }, "+": () => glide(zoomed(w, h, 1.4), 220), "=": () => glide(zoomed(w, h, 1.4), 220),
      "-": () => glide(zoomed(w, h, 1 / 1.4), 220), "0": () => fit([...els.keys()]),
      "ArrowLeft": () => glide({ ...view, x: view.x + 160 }, 180), "ArrowRight": () => glide({ ...view, x: view.x - 160 }, 180),
      "ArrowUp": () => glide({ ...view, y: view.y + 160 }, 180), "ArrowDown": () => glide({ ...view, y: view.y - 160 }, 180),
    }[e.key];
    if (k) { e.preventDefault(); k(); }
  });

  // ---------- the board's look: the gear ----------
  const panel = root.querySelector(".board-style"), gear = root.querySelector('[data-tool="style"]');
  let shown = JSON.stringify(data.style);          // the look on screen; the page was drawn with it
  function look(style) {                           // true when the look changed (and the board was drawn again)
    data.style = style;
    const key = JSON.stringify(style);
    if (key === shown) return false;               // the save's echo and the next poll change nothing: no redraw
    shown = key;
    for (const c of [...root.classList]) if (/^(surface|cards|str)-/.test(c)) root.classList.remove(c);
    root.classList.add("surface-" + style.surface, "cards-" + style.cards, "str-" + style.string);
    root.style.setProperty("--string", style.color);
    if (panel) {
      panel.querySelectorAll(".seg").forEach(g => g.querySelectorAll("button").forEach(b =>
        b.classList.toggle("on", b.dataset.value === style[g.dataset.key])));
      panel.querySelectorAll(".dot[data-kind]").forEach(b => {
        b.hidden = b.dataset.kind !== style.string;
        b.classList.toggle("on", b.dataset.value === style.color);
      });
      const own = panel.querySelector('input[type="color"]');
      if (own && document.activeElement !== own) own.value = style.color;
    }
    render();
    apply();                                        // a moving surface needs the view's offsets at once
    return true;
  }
  async function restyle(change) {
    look({ ...data.style, ...change, ...(change.string && change.string !== data.style.string && !change.color
      ? { color: (panel.querySelector(`.dot[data-kind="${change.string}"]`) || {}).dataset?.value || data.style.color } : {}) });
    try {
      const res = await fetch("/board/style", { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify(change) });
      if (res.ok) { const saved = await res.json(); if (!saved.error) look(saved); }
    } catch (_) { /* kept on screen; the next poll brings the saved look back */ }
  }
  if (panel) {
    panel.addEventListener("click", e => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.classList.contains("dot")) return restyle({ color: b.dataset.value });
      const g = b.closest(".seg");
      if (g) restyle({ [g.dataset.key]: b.dataset.value });
    });
    const own = panel.querySelector('input[type="color"]');
    if (own) {
      own.addEventListener("input", () => { root.style.setProperty("--string", own.value); });
      own.addEventListener("change", () => restyle({ color: own.value }));
    }
  }
  function toggleStyle(open = panel.hidden) {
    if (!panel) return;
    panel.hidden = !open;
    gear.setAttribute("aria-expanded", String(open));
  }

  async function save(n, resized) {
    // a plain drag sends no size, so the note keeps growing with its stage; only a resize fixes it
    const move = resized ? { id: n.id, x: n.x, y: n.y, size: n.size } : { id: n.id, x: n.x, y: n.y };
    try {
      await fetch("/board/move", { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify(move) });
    } catch (_) { /* the next poll puts it back where the server has it */ }
  }

  // ---------- live ----------
  async function poll() {
    if (document.hidden || gesture) return;
    try {
      const res = await fetch("/api/board?sig=" + encodeURIComponent(sig), { cache: "no-store" });
      const next = await res.json();
      if (next.same) return;
      data = next; sig = next.sig;
      root.classList.toggle("animate", !!next.animations);   // the Settings switch takes effect without a reload
      if (!look(next.style)) render();
      if (opened) byId(opened) ? showPop(opened) : closePop();
    } catch (_) { /* the server is restarting; try again next time */ }
  }

  function fill() {                               // the board reaches the bottom of the window, whatever is above it
    const top = root.getBoundingClientRect().top + window.scrollY;
    root.style.height = Math.max(420, window.innerHeight - top - 16) + "px";
    // and spans the window less a 1rem gutter each side: clientWidth leaves out a scrollbar, which 100vw counts,
    // so the tools and the gear's panel are never cut off at the right edge
    const gutter = 16, margin = parseFloat(getComputedStyle(root).marginLeft) || 0;
    root.style.width = (document.documentElement.clientWidth - 2 * gutter) + "px";
    root.style.marginLeft = (margin + gutter - root.getBoundingClientRect().left) + "px";
  }
  fill();
  render();
  const focus = root.dataset.focus && els.has("m:" + root.dataset.focus) ? "m:" + root.dataset.focus : null;
  fit([...els.keys()], 0, 1);
  if (focus) {
    const n = byId(focus), w = root.clientWidth, h = root.clientHeight, z = 1.25;
    setTimeout(() => { glide({ z, x: w / 2 - n.x * z, y: h / 2 - n.y * z }); setTimeout(() => showPop(focus), GLIDE_MS); }, 250);
  }
  window.addEventListener("resize", () => { fill(); placePop(); });
  window.addEventListener("load", () => { if (window.Researchers) window.Researchers.rescan(); });
  if (document.fonts) document.fonts.ready.then(() => {   // a late font changes the notes' heights, so their pins
    for (const el of els.values()) el.pinH = null;
    strings();
  });
  setInterval(poll, data.poll || 4000);
})();
