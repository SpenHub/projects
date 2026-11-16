"use strict";

/* ------------------------------------------------------------------ helpers */
const $ = (id) => document.getElementById(id);
const SVGNS = "http://www.w3.org/2000/svg";

function el(tag, attrs, parent) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null) continue;
    if (k === "text") e.textContent = v;
    else if (k === "class") e.className = v;
    else if (k === "style") e.style.cssText = v;
    else e.setAttribute(k, v);
  }
  if (parent) parent.appendChild(e);
  return e;
}
function sv(tag, attrs, parent) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null) continue;
    if (k === "text") e.textContent = v; else e.setAttribute(k, v);
  }
  if (parent) parent.appendChild(e);
  return e;
}
const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage unavailable */ } },
};

function compact(n, units, base) {
  if (n == null || isNaN(n)) return "—";
  let i = 0;
  while (Math.abs(n) >= base && i < units.length - 1) { n /= base; i++; }
  const s = i === 0 || Math.abs(n) >= 100 ? n.toFixed(0) : Math.abs(n) >= 10 ? n.toFixed(1) : n.toFixed(2);
  return s + " " + units[i];
}
const fmtBytes = (n) => compact(n, ["B", "KB", "MB", "GB", "TB", "PB"], 1000);
const fmtBits = (n) => compact(n, ["b/s", "Kb/s", "Mb/s", "Gb/s"], 1000);
const fmtPct = (v, d = 0) => (v == null ? "—" : v.toFixed(d) + "%");
const fmtMs = (v) => (v == null ? "—" : (v < 10 ? v.toFixed(1) : v.toFixed(0)) + " ms");
function fmtUptimePct(v) {
  if (v == null) return "—";
  if (v >= 99.995) return "100%";
  return (v >= 99 ? v.toFixed(2) : v.toFixed(1)) + "%";
}
function fmtDur(s) {
  s = Math.max(0, Math.round(s));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${sec}s`;
  return `${sec}s`;
}
const fmtClock = (t, sec) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", ...(sec ? { second: "2-digit" } : {}) });
const fmtWhen = (t) => new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const fmtDay = (t) => new Date(t * 1000).toLocaleDateString([], { month: "short", day: "numeric" });
function fmtTick(t, span) {
  const d = new Date(t * 1000);
  if (span > 2 * 86400) return d.toLocaleDateString([], { weekday: "short", day: "numeric" });
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", ...(span <= 600 ? { second: "2-digit" } : {}) });
}
function niceMax(v, floor) {
  v = Math.max(v, floor || 0, 1e-9);
  const exp = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * exp >= v * 1.08) return m * exp;
  return 10 * exp;
}
function sev(pct, warn = 85, crit = 95) {
  if (pct == null) return "";
  return pct >= crit ? "crit" : pct >= warn ? "warn" : "";
}

/* status icon + label: color never carries meaning alone */
const ICON = { good: "✓", warn: "!", serious: "!", crit: "✕", idle: "–" };
function statusIcon(kind, parent, large) {
  return el("span", { class: `ico ${kind}${large ? " lg" : ""}`, text: ICON[kind], "aria-hidden": "true" }, parent);
}
function pill(kind, label, parent) {
  const p = el("span", { class: "pill" }, parent);
  statusIcon(kind, p);
  el("span", { text: label }, p);
  return p;
}
function meter(pct, opts = {}) {
  const m = el("div", { class: "meter" + (opts.lg ? " lg" : "") + (pct == null ? " empty" : ""), role: "meter",
    "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": pct == null ? "0" : pct.toFixed(0),
    "aria-label": opts.label || "usage" });
  const f = el("div", { class: "fill " + sev(pct, opts.warn, opts.crit) }, m);
  f.style.width = Math.max(0, Math.min(100, pct || 0)) + "%";
  return m;
}
function setFacts(dl, pairs) {
  dl.replaceChildren();
  for (const [k, v] of pairs) {
    const d = el("div", {}, dl);
    el("dt", { text: k }, d);
    el("dd", { text: v, title: v }, d);
  }
}
function usageRow(host, what, used, total, pct, opts = {}) {
  host.replaceChildren();
  const r = el("div", { class: "usage-row" }, host);
  el("span", { class: "what", text: what }, r);
  const n = el("span", { class: "num" }, r);
  el("strong", { text: fmtPct(pct) }, n);
  n.append(` · ${fmtBytes(used)} of ${fmtBytes(total)}`);
  r.appendChild(meter(pct, { lg: true, label: what, ...opts }));
}

/* ------------------------------------------------------------------ tooltip */
const tip = $("tip");
function showTip(x, y, build) {
  tip.replaceChildren();
  build(tip);
  tip.hidden = false;
  const r = tip.getBoundingClientRect();
  let left = x + 14, top = y + 14;
  if (left + r.width > window.innerWidth - 8) left = x - 14 - r.width;
  if (top + r.height > window.innerHeight - 8) top = y - 14 - r.height;
  tip.style.left = Math.max(8, left) + "px";
  tip.style.top = Math.max(8, top) + "px";
}
function hideTip() { tip.hidden = true; }
function tipTitle(p, text) { el("div", { class: "tip-title", text }, p); }
function tipRow(p, color, value, label) {
  const r = el("div", { class: "tip-row" }, p);
  if (color) el("span", { class: "tip-key", style: `background: var(${color})` }, r);
  el("strong", { text: value }, r);
  if (label) el("span", { class: "tl", text: label }, r);
}
function hoverable(node, build) {
  node.addEventListener("pointermove", (e) => showTip(e.clientX, e.clientY, build));
  node.addEventListener("pointerleave", hideTip);
  node.addEventListener("focus", () => { const r = node.getBoundingClientRect(); showTip(r.right, r.top, build); });
  node.addEventListener("blur", hideTip);
}

/* ------------------------------------------------------------------ accent */
/* accent picker: each accent carries a high-contrast partner for the second chart series */
const ACCENTS = [
  { name: "Matrix Green", value: "#39ff14", partner: "#ff2d95" },
  { name: "Hotline Pink", value: "#ff2d95", partner: "#00fff5" },
  { name: "Synth Cyan", value: "#00fff5", partner: "#ff6b00" },
  { name: "Vapor Purple", value: "#b537f2", partner: "#ffde00" },
  { name: "Sunset Orange", value: "#ff6b00", partner: "#00fff5" },
  { name: "Grid Yellow", value: "#ffde00", partner: "#b537f2" },
  { name: "Laser Red", value: "#ff2a6d", partner: "#00fff5" },
];
function applyAccent(value) {
  const a = ACCENTS.find((x) => x.value === value) || ACCENTS[0];
  const root = document.documentElement.style;
  root.setProperty("--accent", a.value);
  root.setProperty("--accent-2", a.partner);
  for (const b of $("swatches").children) {
    const on = b.dataset.color === a.value;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", String(on));
  }
  store.set("gd-accent", a.value);
}
for (const a of ACCENTS) {
  const b = el("button", { type: "button", class: "swatch", title: a.name, "aria-label": a.name, "data-color": a.value }, $("swatches"));
  b.style.background = a.value;
  b.style.color = a.value;
  b.addEventListener("click", () => applyAccent(a.value));
}
applyAccent(store.get("gd-accent") || ACCENTS[0].value);
