// Contact sheets of the researcher sprites, drawn in node by the real renderer (assets/researchers.js) on a tiny
// canvas of its own: no browser, no packages. For reviewing a change to a sprite, a pose or a bit.
//   node tools/sprite-sheet.js out.png                       every built-in, standing
//   node tools/sprite-sheet.js out.png read,dig,typing       those bits, 8 moments each, a different researcher per bit
//   node tools/sprite-sheet.js out.png walk ben,mei          one bit for the named sprites (add --cyborgs for that crew)
const fs = require("fs"), zlib = require("zlib");
function parse(c) {
  if (c[0] === "#") { const n = parseInt(c.length === 4 ? c.slice(1).replace(/./g, "$&$&") : c.slice(1, 7), 16); return [n >> 16, (n >> 8) & 255, n & 255, 255]; }
  const m = c.match(/rgba?\(([^)]+)\)/); if (m) { const v = m[1].split(",").map(Number); return [v[0], v[1], v[2], Math.round((v[3] ?? 1) * 255)]; }
  return [0, 0, 0, 255];
}
class Ctx {
  constructor(canvas) { this.canvas = canvas; this.m = [1, 0, 0, 1, 0, 0]; this.stack = []; this.globalAlpha = 1; this.fillStyle = "#000"; }
  get d() { const c = this.canvas; if (!c._d || c._d.length !== c.width * c.height * 4) c._d = new Uint8ClampedArray(c.width * c.height * 4); return c._d; }
  save() { this.stack.push([this.m.slice(), this.globalAlpha]); } restore() { const s = this.stack.pop(); if (s) [this.m, this.globalAlpha] = s; }
  mul(a, b, c, d, e, f) { const m = this.m; this.m = [m[0] * a + m[2] * b, m[1] * a + m[3] * b, m[0] * c + m[2] * d, m[1] * c + m[3] * d, m[0] * e + m[2] * f + m[4], m[1] * e + m[3] * f + m[5]]; }
  translate(x, y) { this.mul(1, 0, 0, 1, x, y); } scale(x, y) { this.mul(x, 0, 0, y, 0, 0); }
  rotate(a) { const c = Math.cos(a), s = Math.sin(a); this.mul(c, s, -s, c, 0, 0); }
  setTransform(a, b, c, d, e, f) { this.m = [a, b, c, d, e, f]; }
  put(x, y, col, al) {
    const c = this.canvas; if (x < 0 || y < 0 || x >= c.width || y >= c.height) return;
    const d = this.d, o = (y * c.width + x) * 4, a = col[3] / 255 * al, ia = d[o + 3] / 255, oa = a + ia * (1 - a);
    if (oa <= 0) return;
    for (let i = 0; i < 3; i++) d[o + i] = (col[i] * a + d[o + i] * ia * (1 - a)) / oa;
    d[o + 3] = oa * 255;
  }
  // paint the transformed rect by inverse mapping every destination pixel centre; src(u, v) gives a color or null
  rect(x, y, w, h, src) {
    const m = this.m, det = m[0] * m[3] - m[1] * m[2]; if (!det) return;
    const pts = [[x, y], [x + w, y], [x, y + h], [x + w, y + h]].map(([a, b]) => [m[0] * a + m[2] * b + m[4], m[1] * a + m[3] * b + m[5]]);
    const x0 = Math.floor(Math.min(...pts.map(p => p[0]))), x1 = Math.ceil(Math.max(...pts.map(p => p[0])));
    const y0 = Math.floor(Math.min(...pts.map(p => p[1]))), y1 = Math.ceil(Math.max(...pts.map(p => p[1])));
    for (let py = y0; py < y1; py++) for (let px = x0; px < x1; px++) {
      const dx = px + 0.5 - m[4], dy = py + 0.5 - m[5];
      const u = (m[3] * dx - m[2] * dy) / det, v = (-m[1] * dx + m[0] * dy) / det;
      if (u < x || v < y || u >= x + w || v >= y + h) continue;
      const col = src(u, v); if (col) this.put(px, py, col, this.globalAlpha);
    }
  }
  fillRect(x, y, w, h) { const col = parse(this.fillStyle); this.rect(x, y, w, h, () => col); }
  clearRect(x, y, w, h) { const c = this.canvas, d = this.d; for (let j = Math.max(0, y); j < Math.min(c.height, y + h); j++) d.fill(0, (j * c.width + Math.max(0, x)) * 4, (j * c.width + Math.min(c.width, x + w)) * 4); }
  getImageData(x, y, w, h) { const c = this.canvas, out = new Uint8ClampedArray(w * h * 4), d = this.d; for (let j = 0; j < h; j++) out.set(d.subarray(((y + j) * c.width + x) * 4, ((y + j) * c.width + x + w) * 4), j * w * 4); return { data: out, width: w, height: h }; }
  putImageData(img, x, y) { const c = this.canvas, d = this.d; for (let j = 0; j < img.height; j++) d.set(img.data.subarray(j * img.width * 4, (j + 1) * img.width * 4), ((y + j) * c.width + x) * 4); }
  drawImage(s, x, y, w, h) {
    const sc = s.canvas || s, sd = sc.getContext("2d").d; w = w ?? sc.width; h = h ?? sc.height;
    this.rect(x, y, w, h, (u, v) => { const sx = Math.floor((u - x) * sc.width / w), sy = Math.floor((v - y) * sc.height / h), o = (sy * sc.width + sx) * 4; return sd[o + 3] ? [sd[o], sd[o + 1], sd[o + 2], sd[o + 3]] : null; });
  }
  beginPath() {} moveTo() {} lineTo() {} stroke() {} fill() {} arc() {} closePath() {} fillText() {} measureText(t) { return { width: t.length * 5 }; }
}
class Canvas {
  constructor(w = 300, h = 150) { this.width = w; this.height = h; this.style = {}; }
  getContext() { return this._ctx || (this._ctx = new Ctx(this)); }
  png() {
    const { width: w, height: h } = this, d = this.getContext().d, raw = Buffer.alloc((w * 4 + 1) * h);
    for (let y = 0; y < h; y++) Buffer.from(d.buffer, y * w * 4, w * 4).copy(raw, y * (w * 4 + 1) + 1);
    const crcT = []; for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; crcT[n] = c >>> 0; }
    const crc = b => { let c = ~0; for (const x of b) c = crcT[(c ^ x) & 255] ^ (c >>> 8); return ~c >>> 0; };
    const chunk = (t, b) => { const l = Buffer.alloc(4); l.writeUInt32BE(b.length); const td = Buffer.concat([Buffer.from(t), b]); const c = Buffer.alloc(4); c.writeUInt32BE(crc(td)); return Buffer.concat([l, td, c]); };
    const ih = Buffer.alloc(13); ih.writeUInt32BE(w, 0); ih.writeUInt32BE(h, 4); ih[8] = 8; ih[9] = 6;
    return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk("IHDR", ih), chunk("IDAT", zlib.deflateSync(raw)), chunk("IEND", Buffer.alloc(0))]);
  }
}
function load(script, config = {}) {
  globalThis.window = globalThis;
  globalThis.matchMedia = () => ({ matches: false });
  const kept = {};
  globalThis.sessionStorage = { getItem: k => kept[k] ?? null, setItem: (k, v) => { kept[k] = String(v); }, removeItem: k => { delete kept[k]; } };
  globalThis.document = { readyState: "complete", addEventListener() {}, querySelectorAll: () => [], querySelector: () => null, documentElement: {}, createElement: () => new Canvas() };
  globalThis.addEventListener = () => {}; globalThis.requestAnimationFrame = () => {}; globalThis.CSS = { escape: s => s };
  window.RESEARCHER_SPRITES = config;
  window.RESEARCHER_BACKS = JSON.parse(fs.readFileSync(require("path").join(require("path").dirname(script), "backs.json"), "utf8"));
  (0, eval)(fs.readFileSync(script, "utf8"));
  return window.Researchers;
}
// cells: [[sprite, action, t, seed, face]]; one PNG, `cols` across, on a floor-colored ground with a tick under each
function sheet(R, cells, file, { cols = 8, scale = 4, W = 64, H = 64, bg = "#efece5", effects = false } = {}) {
  const rows = Math.ceil(cells.length / cols), out = new Canvas(cols * W * scale, rows * H * scale), ctx = out.getContext("2d");
  ctx.fillStyle = bg; ctx.fillRect(0, 0, out.width, out.height);
  cells.forEach(([k, a, t, seed, face], i) => {
    const c = new Canvas(), x = (i % cols) * W * scale, y = Math.floor(i / cols) * H * scale;
    const res = R.renderSprite(c, k, { action: a, t, seed: seed ?? 11, face: face || 1, scale, width: W, height: H, effects });
    if (!res.ok) throw new Error(res.errors.join("; "));
    ctx.fillStyle = "#d8d3c8"; ctx.fillRect(x, y, 1, H * scale); ctx.fillRect(x, y, W * scale, 1);
    ctx.drawImage(c, x, y);
  });
  fs.writeFileSync(file, out.png());
}
module.exports = { Canvas, load, sheet };
if (require.main === module) {
  const path = require("path"), root = path.join(__dirname, ".."), args = process.argv.slice(2).filter(a => a !== "--cyborgs");
  const borg = process.argv.includes("--cyborgs");
  const R = load(path.join(root, "assets", "researchers.js"), borg ? { cyborgs: JSON.parse(fs.readFileSync(path.join(root, "assets", "cyborgs.json"), "utf8")) } : {});
  const [out, bits, who] = args, cast = who ? who.split(",") : R.CAST.filter(k => borg === k.endsWith("-borg")), T = [0.3, 0.8, 1.3, 1.8, 2.4, 3.0, 3.8, 4.6];
  if (!out) { console.error("usage: node tools/sprite-sheet.js out.png [bit,bit...] [sprite,sprite...] [--cyborgs]"); process.exit(1); }
  const cells = !bits ? cast.map(k => [k, "rest", 0.5]) : who ? bits.split(",").flatMap(b => cast.flatMap(k => T.map(t => [k, b, t])))
    : bits.split(",").flatMap((b, i) => T.map(t => [cast[i % cast.length], b, t]));
  sheet(R, cells, out, { cols: bits ? 8 : 6, scale: 4, W: 60, H: 62, effects: !!bits });
  console.log(`${cells.length} drawn to ${out}`);
}
