"use strict";

/* ------------------------------------------------------------------ constants */
const KIND_LABEL = { video: "Video", image: "Images", audio: "Audio", archive: "Archives & disk images", other: "Other" };
const KIND_VAR = { video: "--accent", image: "--accent-2", audio: "--k-audio", archive: "--k-archive", other: "--k-other" };
const kindColor = (k) => `var(${KIND_VAR[k] || KIND_VAR.other})`;
const fmtDate = (t) => (t ? new Date(t * 1000).toLocaleDateString([], { year: "numeric", month: "short", day: "numeric" }) : "—");
const fmtCount = (n) => (n == null ? "—" : n.toLocaleString());

let ROOT = "/mnt/user";
let node = null, media = null, status = null, lib = null, who = { can_write: false, reason: "checking…" };
let libTab = store.get("gd-libtab") === "series" ? "series" : "movies";
const expanded = new Set();

/* ------------------------------------------------------------------ api */
async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(body.error || `HTTP ${r.status}`); e.status = r.status; throw e; }
  return body;
}
async function postJSON(url, data) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "X-Girthy-Dash": "1" },
    body: JSON.stringify(data || {}) });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}

function toast(kind, text) {
  const t = $("toast");
  t.replaceChildren();
  statusIcon(kind, t);
  el("span", { text }, t);
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, kind === "crit" ? 12000 : 7000);
}

/* ------------------------------------------------------------------ navigation */
function currentPath() {
  const h = decodeURIComponent(location.hash.replace(/^#/, ""));
  return h && (h === ROOT || h.startsWith(ROOT + "/")) ? h : ROOT;
}
function go(path) {
  if (path === currentPath()) loadNode();
  else location.hash = encodeURIComponent(path).replace(/%2F/g, "/");
}
const join = (a, b) => a.replace(/\/$/, "") + "/" + b;
const parentOf = (p) => (p === ROOT ? null : p.slice(0, p.lastIndexOf("/")) || ROOT);

function renderCrumbs(path) {
  const host = $("crumbs");
  host.replaceChildren();
  const rel = path.slice(ROOT.length).split("/").filter(Boolean);
  const parts = [[ROOT, ROOT]];
  let acc = ROOT;
  for (const p of rel) { acc = join(acc, p); parts.push([p, acc]); }
  parts.forEach(([label, p], i) => {
    if (i) el("span", { class: "sep", text: "›", "aria-hidden": "true" }, host);
    const b = el("button", { type: "button", text: label }, host);
    if (i === parts.length - 1) b.setAttribute("aria-current", "location");
    else b.addEventListener("click", () => go(p));
  });
}

/* ------------------------------------------------------------------ treemap */
function squarify(items, x, y, w, h) {
  const out = [];
  const total = items.reduce((s, it) => s + it.size, 0);
  if (!total || w <= 0 || h <= 0) return out;
  const scale = (w * h) / total;
  const rest = items.map((it) => ({ it, area: it.size * scale }));
  const worst = (row, side) => {
    let s = 0, mx = 0, mn = Infinity;
    for (const r of row) { s += r.area; mx = Math.max(mx, r.area); mn = Math.min(mn, r.area); }
    return Math.max((side * side * mx) / (s * s), (s * s) / (side * side * mn));
  };
  while (rest.length) {
    const side = Math.min(w, h);
    let row = [], best = Infinity;
    while (rest.length) {
      const cand = row.concat(rest[0]);
      const r = worst(cand, side);
      if (row.length && r > best) break;
      row = cand; best = r; rest.shift();
    }
    const area = row.reduce((s, r) => s + r.area, 0);
    if (w >= h) {
      const rw = area / h;
      let cy = y;
      for (const r of row) { const rh = r.area / rw; out.push({ it: r.it, x, y: cy, w: rw, h: rh }); cy += rh; }
      x += rw; w -= rw;
    } else {
      const rh = area / w;
      let cx = x;
      for (const r of row) { const cw = r.area / rh; out.push({ it: r.it, x: cx, y, w: cw, h: rh }); cx += cw; }
      y += rh; h -= rh;
    }
  }
  return out;
}

/* ------------------------------------------------------------------ media map */
let mapTab = ["all", "movies", "shows", "other"].includes(store.get("gd-maptab")) ? store.get("gd-maptab") : "all";
const SHOW_GROUPS = new Set(["shows", "tv", "series"]);
const groupKind = (g) => { const n = g.name.toLowerCase(); return n === "movies" ? "movies" : SHOW_GROUPS.has(n) ? "shows" : "other"; };
const OTHER_SHADES = ["#e6e6e6", "#b5b5b5", "#8a8a8a", "#6b6b6b"];
function groupColor(g, i) {
  const k = groupKind(g);
  return k === "movies" ? "var(--accent)" : k === "shows" ? "var(--accent-2)" : OTHER_SHADES[i % OTHER_SHADES.length];
}
const groupLabel = (g) => (g.name === "loose files" ? "Loose files" : g.name.replace(/_/g, " "));
function itemTitle(c) {
  if (c.arr && c.arr.title) return `${c.arr.title}${c.arr.year ? ` (${c.arr.year})` : ""}`;
  return c.type === "group" ? c.name : c.name;
}
function libEntry(a) {
  if (!lib || !a) return null;
  return (a.service === "radarr" ? lib.movies : lib.series).find((x) => x.id === a.id) || null;
}

function itemTip(p, g, c) {
  tipTitle(p, itemTitle(c));
  tipRow(p, null, fmtBytes(c.size), `${((c.size / g.size) * 100).toFixed(1)}% of ${groupLabel(g)}`);
  if (c.type === "dir") tipRow(p, null, fmtCount(c.files), c.files === 1 ? "file" : "files");
  if (c.type === "group") tipRow(p, null, fmtCount(c.count), "items too small to draw");
  if (c.arr) tipRow(p, null, c.arr.monitored === false ? "Unmonitored" : "Monitored", c.arr.service === "radarr" ? "in Radarr" : "in Sonarr");
  else if (c.type === "dir" && groupKind(g) !== "other") tipRow(p, null, "Not in " + (groupKind(g) === "movies" ? "Radarr" : "Sonarr"), "");
}

function renderMapTabs() {
  for (const b of $("map-tabs").children) b.setAttribute("aria-pressed", String(b.dataset.tab === mapTab));
}

function renderMap() {
  const host = $("map");
  host.replaceChildren();
  renderMapTabs();
  if (!media) { el("div", { class: "tm-empty", text: status && status.scanning ? "The first scan is running…" : "No scan of /data yet." }, host); return; }
  const groups = media.groups.map((g, i) => ({ g, i, color: groupColor(g, i) }))
    .filter(({ g }) => g.size > 0 && (mapTab === "all" || groupKind(g) === mapTab));
  const total = groups.reduce((s, x) => s + x.g.size, 0);
  $("map-meta").textContent = `${fmtBytes(total)} · ${mapTab === "all" ? media.path : groups.map((x) => groupLabel(x.g)).join(", ")}`;
  if (!groups.length) { el("div", { class: "tm-empty", text: "Nothing here." }, host); return; }
  const W = host.clientWidth, H = host.clientHeight, GAP = 3, HEAD = 20;
  for (const gr of squarify(groups.map((x) => ({ ...x, size: x.g.size })), 0, 0, W, H)) {
    const { g, color } = gr.it;
    const gw = gr.w - GAP, gh = gr.h - GAP;
    if (gw < 3 || gh < 3) continue;
    const box = el("div", { class: "tm-group" }, host);
    box.style.cssText = `left:${gr.x + 1}px;top:${gr.y + 1}px;width:${gw}px;height:${gh}px;border-color:color-mix(in srgb, ${color} 55%, black)`;
    const showHead = gw > 70 && gh > 44;
    if (showHead) {
      const h = el("button", { class: "tm-group-head", type: "button", title: `${g.path} · ${fmtBytes(g.size)}` }, box);
      h.append(groupLabel(g));
      el("span", { text: fmtBytes(g.size) }, h);
      h.addEventListener("click", () => {
        const k = groupKind(g);
        if (mapTab !== k) { mapTab = k; store.set("gd-maptab", k); renderMap(); }
        else openFolder(g.path);
      });
    }
    const top = showHead ? HEAD : 1;
    const items = g.items.filter((c) => c.size > 0);
    for (const r of squarify(items, 1, top, gw - 2, gh - top - 1)) {
      const w = r.w - 2, h = r.h - 2;
      if (w < 2 || h < 2) continue;
      const c = r.it;
      const managedGroup = groupKind(g) !== "other";
      const cls = ["tm-cell", "item", c.type === "group" ? "group" : "",
        c.arr && c.arr.monitored === false ? "unmon" : "",
        managedGroup && c.type === "dir" && !c.arr ? "orphan" : ""].filter(Boolean).join(" ");
      const dim = c.arr && c.arr.monitored === false ? 12 : 26;
      const cell = el("div", { class: cls, tabindex: "0", role: "button", "aria-label": `${itemTitle(c)}, ${fmtBytes(c.size)}` }, box);
      cell.style.cssText = `left:${r.x + 1}px;top:${r.y + 1}px;width:${w}px;height:${h}px;` +
        `background-color:color-mix(in srgb, ${color} ${dim}%, black);border-color:color-mix(in srgb, ${color} 75%, black);`;
      if (w > 36 && h > 16) {
        el("div", { class: "tm-name", text: c.arr ? c.arr.title : c.name }, cell);
        if (h > 30) el("div", { class: "tm-size", text: fmtBytes(c.size) }, cell);
      }
      hoverable(cell, (p) => itemTip(p, g, c));
      const open = () => { hideTip(); openItem(g, c); };
      cell.addEventListener("click", open);
      cell.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
    }
  }
  const lg = $("map-legend");
  lg.replaceChildren();
  const sw = (label, style) => { const s = el("span", {}, lg); el("i", { class: "lg-swatch", style }, s); s.append(label); };
  sw("Movies", "background:color-mix(in srgb, var(--accent) 26%, black);border-color:var(--accent)");
  sw("Shows", "background:color-mix(in srgb, var(--accent-2) 26%, black);border-color:var(--accent-2)");
  sw("Other folders", "background:#2a2a2a;border-color:#8a8a8a");
  sw("Unmonitored", "background:#111;border-style:dashed");
  sw("Not in Radarr/Sonarr", "background:#111 repeating-linear-gradient(135deg, transparent 0 3px, rgba(255,255,255,.35) 3px 4px)");
}
new ResizeObserver(() => { if (media) renderMap(); }).observe($("map"));
for (const b of $("map-tabs").children) {
  b.addEventListener("click", () => { mapTab = b.dataset.tab; store.set("gd-maptab", mapTab); renderMap(); });
}

function openItem(g, c) {
  const dlg = $("dlg");
  dlg.replaceChildren();
  const a = c.arr || null;
  const entry = libEntry(a);
  const path = c.type === "group" ? g.path : join(g.path, c.name);
  const svcName = a ? (a.service === "radarr" ? "Radarr" : "Sonarr") : null;

  const head = el("div", { class: "dlg-head" }, dlg);
  el("h3", { id: "dlg-title", text: itemTitle(c) }, head);
  el("p", { text: a ? (a.kind === "movie" ? "Movie in Radarr" : "Show in Sonarr")
    : c.type === "group" ? `${groupLabel(g)} · items too small to draw`
    : c.type === "file" ? `File in ${groupLabel(g)}`
    : groupKind(g) !== "other" ? `Folder in ${groupLabel(g)} · not in ${groupKind(g) === "movies" ? "Radarr" : "Sonarr"}` : `Folder in ${groupLabel(g)}` }, head);
  if (c.type !== "group") el("p", { class: "item-path", text: path }, head);

  const bodyEl = el("div", { class: "dlg-body" }, dlg);
  const facts = [["Size on disk", fmtBytes(c.size)], ["Share of " + groupLabel(g), `${((c.size / g.size) * 100).toFixed(1)}%`]];
  if (c.type === "dir") facts.push(["Files", fmtCount(c.files)]);
  if (c.type === "group") facts.push(["Items", fmtCount(c.count)]);
  if (entry && a.service === "radarr") facts.push(["Quality", entry.quality || (entry.hasFile ? "—" : "No file")]);
  if (entry && a.service === "sonarr") {
    const seasons = entry.seasons.filter((x) => x.files);
    facts.push(["Seasons on disk", fmtCount(seasons.length)], ["Episode files", fmtCount(entry.files)]);
    if (entry.status) facts.push(["Status", entry.status[0].toUpperCase() + entry.status.slice(1)]);
  }
  const dl = el("dl", { class: "facts item-facts" }, bodyEl);
  setFacts(dl, facts);
  if (a) {
    const m = el("div", {}, bodyEl);
    pill(a.monitored === false ? "idle" : "good", a.monitored === false ? `Unmonitored in ${svcName}` : `Monitored in ${svcName}`, m);
  }

  let excl = null, err = null;
  if (a) {
    const c2 = el("label", { class: "check" }, bodyEl);
    excl = el("input", { type: "checkbox", name: "exclusion" }, c2);
    c2.append(`Add to ${svcName}'s import exclusions, so lists can't re-add it`);
    const recycle = lib && lib.recycle ? lib.recycle[a.service] : undefined;
    const n = el("div", { class: "dlg-note" }, bodyEl);
    statusIcon(recycle ? "warn" : "crit", n);
    el("span", { text: recycle ? `Delete removes it from ${svcName} and moves its files to ${recycle}.`
      : `Delete removes it from ${svcName} and permanently deletes its files (no recycle bin is set).` }, n);
    if (!who.can_write) {
      const r = el("div", { class: "dlg-note" }, bodyEl);
      statusIcon("idle", r);
      el("span", { text: `Read-only here. ${who.reason || ""}` }, r);
    }
    err = el("div", { class: "dlg-error", role: "alert" }, bodyEl);
  } else if (c.type === "dir" && groupKind(g) !== "other") {
    const n = el("div", { class: "dlg-note" }, bodyEl);
    statusIcon("warn", n);
    el("span", { text: `${groupKind(g) === "movies" ? "Radarr" : "Sonarr"} doesn't know about this folder, so it can't be removed from here. Delete it from the share, or import it.` }, n);
  }

  const foot = el("div", { class: "dlg-foot" }, dlg);
  if (c.type === "dir" || c.type === "group") {
    const browse = el("button", { class: "btn", type: "button", text: "Browse folder" }, foot);
    browse.addEventListener("click", () => { dlg.close(); openFolder(c.type === "group" ? g.path : path); });
  }
  if (a) {
    const more = el("button", { class: "btn", type: "button", text: "More options…" }, foot);
    more.disabled = !who.can_write;
    more.addEventListener("click", () => { dlg.close(); openRemove({ ...a, size: c.size }); });
  }
  el("span", { class: "grow" }, foot);
  const close = el("button", { class: "btn", type: "button", text: a ? "Cancel" : "Close" }, foot);
  close.addEventListener("click", () => dlg.close());
  if (a) {
    const del = el("button", { class: "btn danger", type: "button", text: "Delete" }, foot);
    del.disabled = !who.can_write;
    if (!who.can_write) del.title = who.reason || "Read-only";
    let armed = false, armTimer = null;
    del.addEventListener("click", async () => {
      if (!armed) {
        armed = true;
        del.textContent = `Tap again: delete ${fmtBytes(c.size)}`;
        clearTimeout(armTimer);
        armTimer = setTimeout(() => { armed = false; del.textContent = "Delete"; }, 5000);
        return;
      }
      clearTimeout(armTimer);
      del.disabled = close.disabled = true;
      del.textContent = "Deleting…";
      err.textContent = "";
      const url = a.service === "radarr" ? `api/arr/radarr/movie/${a.id}/delete` : `api/arr/sonarr/series/${a.id}/delete`;
      try {
        const r = await postJSON(url, { mode: "remove", exclusion: !!excl.checked });
        dlg.close();
        toast("good", r.message || "Done.");
        await refreshAfterChange();
      } catch (e) {
        err.textContent = e.message;
        del.disabled = close.disabled = false;
        armed = false;
        del.textContent = "Delete";
      }
    });
  }
  dlg.showModal();
  close.focus();
}

async function loadMedia() {
  try { media = await getJSON("api/storage/media"); } catch (e) { media = null; }
  renderMap();
}
async function refreshAfterChange() {
  await loadLibrary(true);
  await Promise.all([loadMedia(), loadNode(), loadStatus()]);
}

/* ------------------------------------------------------------------ tables */
function headRow(table, cols) {
  const tr = el("tr", {}, el("thead", {}, table));
  for (const [label, cls] of cols) el("th", { text: label, class: cls }, tr);
  return el("tbody", {}, table);
}
function shareCell(tr, part, whole) {
  const td = el("td", {}, tr);
  const pct = whole ? (part / whole) * 100 : 0;
  const d = el("div", { class: "share" }, td);
  const m = meter(pct, { warn: 101, crit: 101, label: "share" });
  d.appendChild(m);
  el("span", { text: pct >= 10 ? pct.toFixed(0) + "%" : pct.toFixed(1) + "%" }, d);
}
function arrLabel(a) {
  if (a.kind === "season") return `Sonarr · ${a.title} · ${a.season === 0 ? "Specials" : "Season " + a.season}`;
  return `${a.service === "radarr" ? "Radarr" : "Sonarr"} · ${a.title}${a.year ? ` (${a.year})` : ""}`;
}
function arrTag(parent, a) {
  const t = el("span", { class: "tag" + (a.monitored === false ? " off" : ""), title: (a.monitored === false ? "Unmonitored · " : "Monitored · ") + arrLabel(a) }, parent);
  el("i", {}, t);
  t.append(a.kind === "season" ? (a.season === 0 ? "Specials" : `S${String(a.season).padStart(2, "0")}`) : a.service === "radarr" ? "Radarr" : "Sonarr");
  return t;
}
function removeButton(parent, target) {
  const b = el("button", { class: "btn small", type: "button", text: "Remove…" }, parent);
  if (!who.can_write) { b.disabled = true; b.title = who.reason || "Read-only"; }
  b.addEventListener("click", () => openRemove(target));
  return b;
}

function renderChildren() {
  const t = $("children");
  t.replaceChildren();
  const body = headRow(t, [["Name"], ["Size", "num"], ["Share"], ["Files", "num"], ["Modified"], ["Library"], [""]]);
  for (const c of node.children) {
    const tr = el("tr", {}, body);
    const nameTd = el("td", {}, tr);
    const wrap = el("span", { style: "display:inline-flex;align-items:center;gap:8px" }, nameTd);
    el("i", { class: "kdot", style: `background:${kindColor(c.kind)}` }, wrap);
    if (c.type === "dir") {
      const b = el("button", { class: "linkish ellip", type: "button", text: c.name + "/", title: c.name }, wrap);
      b.addEventListener("click", () => go(join(node.path, c.name)));
    } else el("span", { class: "ellip", text: c.name, title: c.name, style: c.type === "group" ? "color:var(--muted)" : "" }, wrap);
    el("td", { class: "num", text: fmtBytes(c.size) }, tr);
    shareCell(tr, c.size, node.size);
    el("td", { class: "num", text: c.type === "dir" ? fmtCount(c.files) : c.type === "group" ? fmtCount(c.count) : "1" }, tr);
    el("td", { class: "dim", text: c.mtime ? fmtDate(c.mtime) : "" }, tr);
    const lt = el("td", {}, tr);
    const at = el("td", {}, tr);
    if (c.arr) {
      arrTag(lt, c.arr);
      removeButton(at, c.arr);
    }
  }
}

function renderTypes() {
  const order = ["video", "image", "audio", "archive", "other"];
  const bar = $("kind-bar");
  bar.replaceChildren();
  const total = node.size || 1;
  for (const k of order) {
    const v = node.kinds[k];
    if (!v) continue;
    const d = el("div", { tabindex: "0", "aria-label": `${KIND_LABEL[k]}: ${fmtBytes(v)}` }, bar);
    d.style.cssText = `flex:${v / total} 1 0;background:${kindColor(k)}`;
    hoverable(d, (p) => { tipTitle(p, KIND_LABEL[k]); tipRow(p, null, fmtBytes(v), `${((v / total) * 100).toFixed(1)}%`); });
  }
  const lg = $("kind-legend");
  lg.replaceChildren();
  for (const k of order) {
    const s = el("span", {}, lg);
    el("i", { class: "kdot", style: `background:${kindColor(k)}` }, s);
    s.append(KIND_LABEL[k]);
  }
  const t = $("exts");
  t.replaceChildren();
  const body = headRow(t, [["Extension"], ["Type"], ["Files", "num"], ["Size", "num"], ["Share"]]);
  for (const e of node.exts) {
    const tr = el("tr", {}, body);
    const n = el("td", {}, tr);
    const w = el("span", { style: "display:inline-flex;align-items:center;gap:8px;font-family:var(--mono)" }, n);
    el("i", { class: "kdot", style: `background:${kindColor(e.kind)}` }, w);
    w.append(e.ext === "(none)" ? "(none)" : "." + e.ext);
    el("td", { class: "dim", text: KIND_LABEL[e.kind] }, tr);
    el("td", { class: "num", text: fmtCount(e.files) }, tr);
    el("td", { class: "num", text: fmtBytes(e.size) }, tr);
    shareCell(tr, e.size, node.size);
  }
  if (node.exts_other && node.exts_other.count) {
    const tr = el("tr", {}, body);
    el("td", { class: "dim", text: `${node.exts_other.count} more types` }, tr);
    el("td", {}, tr); el("td", {}, tr);
    el("td", { class: "num", text: fmtBytes(node.exts_other.size) }, tr);
    shareCell(tr, node.exts_other.size, node.size);
  }
}

function renderLargest() {
  const t = $("largest");
  t.replaceChildren();
  const body = headRow(t, [["File"], ["Size", "num"], ["Modified"]]);
  for (const f of node.largest) {
    const tr = el("tr", {}, body);
    const td = el("td", {}, tr);
    const rel = f.path.slice(node.path.length).replace(/^\//, "");
    const slash = rel.lastIndexOf("/");
    const w = el("span", { style: "display:inline-flex;align-items:center;gap:8px;min-width:0" }, td);
    el("i", { class: "kdot", style: `background:${kindColor(f.kind)}` }, w);
    const name = el("span", { class: "ellip", title: f.path }, w);
    if (slash > 0) {
      const dir = rel.slice(0, slash);
      const b = el("button", { class: "linkish", type: "button", text: dir + "/", style: "font-weight:400;color:var(--muted)" }, name);
      b.addEventListener("click", () => go(join(node.path, dir)));
    }
    name.append(rel.slice(slash + 1));
    el("td", { class: "num", text: fmtBytes(f.size) }, tr);
    el("td", { class: "dim", text: fmtDate(f.mtime) }, tr);
  }
  if (!node.largest.length) el("td", { class: "dim", text: "No files here.", colspan: "3" }, el("tr", {}, body));
}

function renderFolderArr() {
  const host = $("folder-arr");
  host.replaceChildren();
  if (!node.arr) return;
  const b = el("div", { class: "arr-banner" }, host);
  const s = el("span", {}, b);
  s.append("This folder is ");
  el("strong", { text: arrLabel(node.arr) }, s);
  s.append(node.arr.monitored === false ? " (unmonitored)." : " (monitored).");
  removeButton(b, node.arr);
}

/* ------------------------------------------------------------------ library */
function renderLibrary() {
  const t = $("library"), note = $("lib-note");
  t.replaceChildren();
  note.replaceChildren();
  for (const b of $("lib-tabs").children) b.setAttribute("aria-pressed", String(b.dataset.tab === libTab));
  if (!lib) { el("div", { class: "empty-note", text: "Loading library…" }, note); return; }
  if (lib.config_error) { el("div", { class: "empty-note", text: lib.config_error }, note); return; }
  const svc = libTab === "movies" ? "radarr" : "sonarr";
  if (lib.errors[svc]) {
    const n = el("div", { class: "notice" }, note);
    statusIcon("warn", n);
    el("span", { text: lib.errors[svc] }, n);
  }
  if (!who.can_write) {
    const n = el("div", { class: "notice", style: "margin-bottom:10px" }, note);
    statusIcon("idle", n);
    el("span", { text: `Read-only here. ${who.reason || ""}` }, n);
  }
  const q = $("lib-search").value.trim().toLowerCase();
  const match = (x) => !q || (x.title || "").toLowerCase().includes(q);

  if (libTab === "movies") {
    const rows = lib.movies.filter(match).sort((a, b) => b.size - a.size);
    const body = headRow(t, [["Movie"], ["Size", "num"], ["Quality"], ["Status"], [""]]);
    for (const m of rows) {
      const tr = el("tr", {}, body);
      const td = el("td", {}, tr);
      const b = el("button", { class: "linkish ellip", type: "button", text: `${m.title}${m.year ? ` (${m.year})` : ""}`, title: m.path }, td);
      b.addEventListener("click", () => openFolder(m.path));
      el("td", { class: "num", text: m.hasFile ? fmtBytes(m.size) : "—" }, tr);
      el("td", { class: "dim", text: m.quality || (m.hasFile ? "" : "No file") }, tr);
      pill(m.monitored ? "good" : "idle", m.monitored ? "Monitored" : "Unmonitored", el("td", {}, tr));
      removeButton(el("td", {}, tr), { service: "radarr", kind: "movie", id: m.id, title: m.title, year: m.year, size: m.size, monitored: m.monitored });
    }
    if (!rows.length) el("td", { class: "dim", colspan: "5", text: q ? "No movies match." : "No movies." }, el("tr", {}, body));
    $("lib-meta").textContent = `${lib.movies.length} movies · ${fmtBytes(lib.movies.reduce((s, m) => s + m.size, 0))}`;
  } else {
    const rows = lib.series.filter(match).sort((a, b) => b.size - a.size);
    const body = headRow(t, [["Show"], ["Size", "num"], ["Episode files", "num"], ["Status"], [""]]);
    for (const s of rows) {
      const tr = el("tr", {}, body);
      const td = el("td", {}, tr);
      const w = el("span", { style: "display:inline-flex;align-items:center;gap:8px" }, td);
      const open = expanded.has(s.id);
      const tg = el("button", { class: "btn small", type: "button", text: open ? "▾" : "▸", "aria-expanded": String(open),
        "aria-label": `${open ? "Hide" : "Show"} seasons of ${s.title}` }, w);
      tg.addEventListener("click", () => { open ? expanded.delete(s.id) : expanded.add(s.id); renderLibrary(); });
      const b = el("button", { class: "linkish ellip", type: "button", text: `${s.title}${s.year ? ` (${s.year})` : ""}`, title: s.path }, w);
      b.addEventListener("click", () => openFolder(s.path));
      el("td", { class: "num", text: fmtBytes(s.size) }, tr);
      el("td", { class: "num", text: fmtCount(s.files) }, tr);
      pill(s.monitored ? "good" : "idle", s.monitored ? "Monitored" : "Unmonitored", el("td", {}, tr));
      removeButton(el("td", {}, tr), { service: "sonarr", kind: "series", id: s.id, title: s.title, year: s.year, size: s.size, monitored: s.monitored });
      if (open) {
        for (const se of s.seasons.filter((x) => x.files || x.size).sort((a, b) => a.n - b.n)) {
          const r = el("tr", { class: "season" }, body);
          el("td", { text: se.n === 0 ? "Specials" : `Season ${se.n}` }, r);
          el("td", { class: "num", text: fmtBytes(se.size) }, r);
          el("td", { class: "num", text: fmtCount(se.files) }, r);
          pill(se.monitored ? "good" : "idle", se.monitored ? "Monitored" : "Unmonitored", el("td", {}, r));
          removeButton(el("td", {}, r), { service: "sonarr", kind: "season", id: s.id, title: s.title, season: se.n, size: se.size, monitored: se.monitored });
        }
      }
    }
    if (!rows.length) el("td", { class: "dim", colspan: "5", text: q ? "No shows match." : "No shows." }, el("tr", {}, body));
    $("lib-meta").textContent = `${lib.series.length} shows · ${fmtBytes(lib.series.reduce((s, x) => s + x.size, 0))}`;
  }
}
function openFolder(path) {
  if (path === ROOT || path.startsWith(ROOT + "/")) {
    go(path);
    $("folder-title").closest(".card").scrollIntoView({ behavior: "smooth", block: "start" });
  }
}
for (const b of $("lib-tabs").children) {
  b.addEventListener("click", () => { libTab = b.dataset.tab; store.set("gd-libtab", libTab); renderLibrary(); });
}
$("lib-search").addEventListener("input", renderLibrary);

/* ------------------------------------------------------------------ remove dialog */
function openRemove(target) {
  const dlg = $("dlg");
  dlg.replaceChildren();
  const svcName = target.service === "radarr" ? "Radarr" : "Sonarr";
  const what = target.kind === "season" ? `${target.season === 0 ? "Specials" : "Season " + target.season} of ${target.title}`
    : `${target.title}${target.year ? ` (${target.year})` : ""}`;

  const head = el("div", { class: "dlg-head" }, dlg);
  el("h3", { id: "dlg-title", text: `Remove ${what}` }, head);
  el("p", { text: target.size ? `Frees about ${fmtBytes(target.size)}. Handled by ${svcName}, so its records stay accurate.` : `Handled by ${svcName}, so its records stay accurate.` }, head);

  const form = el("form", { method: "dialog" }, dlg);
  const bodyEl = el("div", { class: "dlg-body" }, form);
  const opts = [];
  if (target.kind === "movie") {
    opts.push(["files", "Delete the file, keep the movie in Radarr", "Radarr keeps the entry and marks it unmonitored, so it won't download it again."]);
    opts.push(["remove", "Remove the movie from Radarr and delete its folder", "The movie disappears from Radarr entirely."]);
  } else if (target.kind === "series") {
    opts.push(["files", "Delete every episode file, keep the show in Sonarr", "Sonarr keeps the show and unmonitors it and all its seasons, so nothing re-downloads."]);
    opts.push(["remove", "Remove the show from Sonarr and delete its folder", "The show disappears from Sonarr entirely."]);
  } else {
    opts.push(["files", `Delete this season's episode files`, "Sonarr keeps the show and unmonitors this season, so it won't re-download."]);
  }
  opts.forEach(([value, label, desc], i) => {
    const o = el("label", { class: "opt" }, bodyEl);
    el("input", { type: "radio", name: "mode", value, ...(i === 0 ? { checked: "" } : {}) }, o);
    el("strong", { text: label }, o);
    el("small", { text: desc }, o);
  });
  let excl = null;
  if (target.kind !== "season") {
    const c = el("label", { class: "check" }, bodyEl);
    excl = el("input", { type: "checkbox", name: "exclusion" }, c);
    c.append(`Also add to ${svcName}'s import exclusions, so lists can't re-add it`);
    excl.disabled = true;
    form.addEventListener("change", () => {
      excl.disabled = form.mode.value !== "remove";
      if (excl.disabled) excl.checked = false;
      armed = false; go_btn.textContent = "Delete";
    });
  }
  const recycle = lib && lib.recycle ? lib.recycle[target.service] : undefined;
  const n = el("div", { class: "dlg-note" }, bodyEl);
  if (recycle) {
    statusIcon("warn", n);
    el("span", { text: `${svcName} moves deleted files to ${recycle}. Space is only freed once its recycle bin is emptied.` }, n);
  } else {
    statusIcon("crit", n);
    el("span", { text: `${svcName} has no recycle bin set, so the files are deleted permanently.` }, n);
  }
  const err = el("div", { class: "dlg-error", role: "alert" }, bodyEl);

  const foot = el("div", { class: "dlg-foot" }, form);
  const cancel = el("button", { class: "btn", type: "button", text: "Cancel" }, foot);
  const go_btn = el("button", { class: "btn danger", type: "button", text: "Delete" }, foot);
  let armed = false, armTimer = null;
  cancel.addEventListener("click", () => dlg.close());
  go_btn.addEventListener("click", async () => {
    if (!armed) {
      armed = true;
      go_btn.textContent = target.size ? `Click to delete ${fmtBytes(target.size)}` : "Click again to confirm";
      clearTimeout(armTimer);
      armTimer = setTimeout(() => { armed = false; go_btn.textContent = "Delete"; }, 5000);
      return;
    }
    clearTimeout(armTimer);
    go_btn.disabled = cancel.disabled = true;
    go_btn.textContent = "Deleting…";
    err.textContent = "";
    const mode = form.mode ? form.mode.value : "files";
    const payload = { mode, exclusion: !!(excl && excl.checked) };
    if (target.kind === "season") payload.season = target.season;
    const url = target.service === "radarr" ? `api/arr/radarr/movie/${target.id}/delete` : `api/arr/sonarr/series/${target.id}/delete`;
    try {
      const r = await postJSON(url, payload);
      dlg.close();
      toast("good", r.message || "Done.");
      await refreshAfterChange();
    } catch (e) {
      err.textContent = e.message;
      go_btn.disabled = cancel.disabled = false;
      armed = false;
      go_btn.textContent = "Delete";
    }
  });
  dlg.showModal();
  cancel.focus();
}

/* ------------------------------------------------------------------ status / tiles */
function renderStatus() {
  const s = $("scan-status");
  s.replaceChildren();
  const btn = $("rescan");
  if (!status) return;
  if (status.scanning) {
    const sc = status.scanning;
    statusIcon("idle", s);
    const t = el("span", {}, s);
    t.append("Scanning… ");
    el("strong", { text: `${fmtCount(sc.files)} files · ${fmtBytes(sc.bytes)}` }, t);
    t.append(` · ${fmtDur(sc.elapsed)}`);
    s.title = sc.current;
    btn.disabled = true;
    btn.textContent = "Scanning…";
  } else {
    if (status.info) {
      const t = el("span", {}, s);
      t.append("Scanned ");
      el("strong", { text: fmtDur(Date.now() / 1000 - status.info.finished) + " ago" }, t);
      t.append(` · took ${fmtDur(status.info.duration)}`);
    } else if (status.error) {
      statusIcon("crit", s);
      el("span", { text: `Scan failed: ${status.error}` }, s);
    }
    btn.disabled = !who.can_write;
    btn.title = who.can_write ? "Re-read every folder. This can spin up array disks." : who.reason || "Read-only";
    btn.textContent = "Rescan";
  }
}

function renderTiles() {
  const host = $("tiles");
  host.replaceChildren();
  const info = status && status.info;
  const t = (label, value, sub) => {
    const d = el("div", { class: "tile" }, host);
    el("div", { class: "label", text: label }, d);
    el("div", { class: "value", text: value }, d);
    el("div", { class: "sub", text: sub || " ", title: sub || "" }, d);
  };
  t("Scanned", info ? fmtBytes(info.bytes) : "—", status ? status.root : "");
  t("Files", info ? compact(info.files, ["", "K", "M"], 1000).replace(/ $/, "") : "—", info ? `${fmtCount(info.dirs)} folders` : "");
  t("This folder", node ? fmtBytes(node.size) : "—", node ? `${fmtCount(node.files)} files` : "");
  if (lib && lib.enabled.radarr) t("Movies", fmtBytes(lib.movies.reduce((s, m) => s + m.size, 0)), `${lib.movies.length} in Radarr`);
  if (lib && lib.enabled.sonarr) t("Shows", fmtBytes(lib.series.reduce((s, m) => s + m.size, 0)), `${lib.series.length} in Sonarr`);
}

/* ------------------------------------------------------------------ loading */
async function loadNode() {
  const path = currentPath();
  renderCrumbs(path);
  try {
    node = await getJSON("api/storage/node?path=" + encodeURIComponent(path));
    $("notices").replaceChildren();
  } catch (e) {
    node = null;
    const n = $("notices");
    n.replaceChildren();
    const d = el("div", { class: "notice" }, n);
    statusIcon(status && status.scanning ? "idle" : "warn", d);
    el("span", { text: status && status.scanning && !status.ready ? "The first scan is running. This page fills in when it finishes."
      : e.status === 404 && path !== ROOT ? `${path} isn't in the last scan.` : e.message }, d);
    if (e.status === 404 && path !== ROOT && status && status.ready) { go(parentOf(path) || ROOT); return; }
  }
  $("folder-title").textContent = node ? (node.path === ROOT ? "Disk usage" : node.path.slice(node.path.lastIndexOf("/") + 1)) : "Disk usage";
  $("folder-meta").textContent = node ? `${fmtBytes(node.size)} · ${fmtCount(node.files)} files · ${fmtCount(node.dirs)} folders` : "";
  if (node) {
    renderFolderArr();
    renderChildren();
    renderTypes();
    renderLargest();
  } else {
    for (const id of ["folder-arr", "children", "kind-bar", "exts", "largest"]) $(id).replaceChildren();
  }
  renderTiles();
}

async function loadLibrary(force) {
  try { lib = await getJSON("api/arr/library" + (force ? "?refresh=1" : "")); }
  catch (e) { lib = { movies: [], series: [], errors: { radarr: e.message, sonarr: e.message }, enabled: {}, recycle: {} }; }
  renderLibrary();
  renderTiles();
}

let statusTimer = null, wasScanning = false;
async function loadStatus() {
  clearTimeout(statusTimer);
  try {
    status = await getJSON("api/storage/status");
    ROOT = status.root || ROOT;
  } catch (e) { /* keep last status */ }
  const scanning = !!(status && status.scanning);
  if (wasScanning && !scanning) { loadNode(); loadMedia(); }
  wasScanning = scanning;
  renderStatus();
  statusTimer = setTimeout(loadStatus, scanning ? 1500 : 30000);
}

$("rescan").addEventListener("click", async () => {
  try {
    await postJSON("api/storage/scan");
    toast("good", "Rescan started. Array disks may spin up while it runs.");
  } catch (e) { toast("crit", e.message); }
  loadStatus();
});

window.addEventListener("hashchange", loadNode);

(async () => {
  try { who = await getJSON("api/whoami"); } catch (e) { who = { can_write: false, reason: e.message }; }
  await loadStatus();
  renderMap();
  await Promise.all([loadNode(), loadLibrary(false).then(loadMedia)]);
})();
