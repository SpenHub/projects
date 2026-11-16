"use strict";

const REFRESH_MS = 5000;
let data = null, who = { can_write: false, reason: "checking…" };
let sortBy = ["name", "cpu", "mem", "uptime"].includes(store.get("gd-dsort")) ? store.get("gd-dsort") : "name";
const open = new Set();
const fmtCount = (n) => (n == null ? "—" : n.toLocaleString());
const fmtCpu = (v) => (v == null ? "—" : v < 10 ? v.toFixed(1) + "%" : v.toFixed(0) + "%");
const isRunning = (c) => String(c.state).toUpperCase() === "RUNNING";

/* ------------------------------------------------------------------ api */
async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}
async function postJSON(url, d) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "X-Girthy-Dash": "1" },
    body: JSON.stringify(d || {}) });
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

/* ------------------------------------------------------------------ helpers */
function healthKind(c) {
  if (!isRunning(c)) return String(c.state).toUpperCase() === "PAUSED" ? ["warn", "Paused"] : ["crit", "Stopped"];
  if (c.health === "unhealthy") return ["crit", "Unhealthy"];
  if (c.health === "starting") return ["warn", "Starting"];
  if (c.health === "healthy") return ["good", "Healthy"];
  return ["good", "Running"];
}
function uptimeText(c) {
  const u = c.uptime || {};
  if (!u.since) return isRunning(c) ? (c.status || "").replace(/\s*\(.*\)$/, "") : "—";
  const d = fmtDur(data.now - u.since);
  if (isRunning(c) && !u.since_exact) return (c.status || "").replace(/^Up\s+/i, "").replace(/\s*\(.*\)$/, "") || d;
  return isRunning(c) ? d : `down ${d}`;
}
function cellClass(cell) {
  if (!cell.tracked) return "";
  const f = cell.up / cell.tracked;
  return f >= 0.999 ? "good" : f <= 0.001 ? "down" : "partial";
}
function sparkline(points, idx, maxFloor) {
  const W = 84, H = 22;
  const s = sv("svg", { class: "spark", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", "aria-hidden": "true" });
  const vals = points.map((p) => p[idx]).filter((v) => v != null);
  if (vals.length < 2) return s;
  const max = Math.max(maxFloor, ...vals) * 1.1;
  const t0 = data.now - 3600;
  const xy = points.filter((p) => p[idx] != null).map((p) => [((p[0] - t0) / 3600) * W, H - 1 - (p[idx] / max) * (H - 2)]);
  const d = xy.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join("");
  sv("path", { class: "area", d: `${d}L${xy[xy.length - 1][0].toFixed(1)},${H}L${xy[0][0].toFixed(1)},${H}Z` }, s);
  sv("path", { class: "line", d }, s);
  return s;
}
function sparkTip(p, c, idx, label, fmt) {
  const pts = (c.spark || []).map((x) => x[idx]).filter((v) => v != null);
  tipTitle(p, `${c.name} · ${label}, last hour`);
  if (!pts.length) { tipRow(p, null, "No samples yet", ""); return; }
  tipRow(p, null, fmt(pts[pts.length - 1]), "now");
  tipRow(p, null, fmt(pts.reduce((a, b) => a + b, 0) / pts.length), "average");
  tipRow(p, null, fmt(Math.max(...pts)), "peak");
}
function headRow(table, cols) {
  const tr = el("tr", {}, el("thead", {}, table));
  for (const [label, cls] of cols) el("th", { text: label, class: cls || null }, tr);
  return el("tbody", {}, table);
}

/* ------------------------------------------------------------------ tiles */
function renderTiles() {
  const host = $("tiles");
  host.replaceChildren();
  const cs = data.containers;
  const tile = (label, value, sub, kind) => {
    const d = el("div", { class: "tile" }, host);
    const l = el("div", { class: "label" }, d);
    el("span", { text: label }, l);
    el("div", { class: "value", text: value, style: kind === "crit" ? "color:var(--crit)" : kind === "warn" ? "color:var(--warn)" : null }, d);
    el("div", { class: "sub", text: sub || " ", title: sub || "" }, d);
  };
  const running = cs.filter(isRunning).length;
  tile("Running", `${running}/${cs.length}`, running === cs.length ? "all containers up" : `${cs.length - running} stopped`, running === cs.length ? null : "warn");
  const unhealthy = cs.filter((c) => isRunning(c) && c.health === "unhealthy").length;
  const withHc = cs.filter((c) => c.health).length;
  tile("Health", unhealthy ? `${unhealthy} bad` : "OK", `${cs.filter((c) => c.health === "healthy").length} of ${withHc} health checks passing`, unhealthy ? "crit" : null);
  const cpu = cs.reduce((s, c) => s + ((c.stats && c.stats.cpu) || 0), 0);
  tile("CPU", fmtCpu(data.cores ? cpu / data.cores : cpu), data.cores ? `of all ${data.cores} threads · ${fmtCpu(cpu)} summed` : "summed across containers");
  const mem = cs.reduce((s, c) => s + ((c.stats && c.stats.mem) || 0), 0);
  tile("Memory", fmtBytes(mem), data.mem_total ? `${fmtPct((mem / data.mem_total) * 100, 1)} of ${fmtBytes(data.mem_total)} RAM` : "used by containers");
  const restarts = cs.reduce((s, c) => s + ((c.uptime && c.uptime.restarts_7d) || 0), 0);
  tile("Restarts", String(restarts), "in the last 7 days");
  const di = data.docker_img;
  tile("docker.img", di ? fmtPct(di.pct) : "—", di ? `${fmtBytes(di.used)} of ${fmtBytes(di.total)}` : "", di && di.pct >= 90 ? "crit" : di && di.pct >= 75 ? "warn" : null);
  const upd = cs.filter((c) => c.update).length;
  if (upd) tile("Updates", String(upd), cs.filter((c) => c.update).map((c) => c.name).join(", "), "warn");
}

/* ------------------------------------------------------------------ table */
function sorted() {
  const q = $("filter").value.trim().toLowerCase();
  const rows = data.containers.filter((c) => !q || c.name.toLowerCase().includes(q) || (c.image || "").toLowerCase().includes(q));
  const cpu = (c) => (c.stats && c.stats.cpu) || 0, mem = (c) => (c.stats && c.stats.mem) || 0;
  const up = (c) => (isRunning(c) && c.uptime && c.uptime.since ? data.now - c.uptime.since : -1);
  const by = { name: (a, b) => a.name.localeCompare(b.name), cpu: (a, b) => cpu(b) - cpu(a), mem: (a, b) => mem(b) - mem(a), uptime: (a, b) => up(b) - up(a) };
  return rows.sort((a, b) => (isRunning(b) - isRunning(a)) || by[sortBy](a, b) || a.name.localeCompare(b.name));
}

function renderTable() {
  const t = $("ctable");
  t.replaceChildren();
  const body = headRow(t, [["Container"], ["Status"], ["Uptime"], ["CPU · 1h", "num"], ["Memory · 1h", "num"],
    ["Network ↓ ↑", "num hide-sm"], ["Disk read / write", "num hide-sm"], ["Last 24h"], [""]]);
  const rows = sorted();
  for (const c of rows) {
    const isOpen = open.has(c.name);
    const tr = el("tr", { class: "crow", tabindex: "0", "aria-expanded": String(isOpen) }, body);
    const toggle = () => { isOpen ? open.delete(c.name) : open.add(c.name); renderTable(); };
    tr.addEventListener("click", (e) => { if (!e.target.closest("button, a")) toggle(); });
    tr.addEventListener("keydown", (e) => { if ((e.key === "Enter" || e.key === " ") && e.target === tr) { e.preventDefault(); toggle(); } });

    const nm = el("div", { class: "cname" }, el("td", {}, tr));
    const strong = el("strong", {}, nm);
    el("span", { class: "caret", text: isOpen ? "▾" : "▸", "aria-hidden": "true" }, strong);
    strong.append(c.name);
    if (c.update) el("span", { class: "badge", text: "Update" }, strong);
    el("small", { text: c.image || "", title: c.image || "" }, nm);

    const [k, label] = healthKind(c);
    pill(k, label, el("td", {}, tr));
    el("td", { class: "dim", text: uptimeText(c), title: c.status || "", style: "white-space:nowrap" }, tr);

    const st = c.stats;
    const cpuTd = el("td", {}, tr);
    const cm = el("div", { class: "metric" }, cpuTd);
    const cs = sparkline(c.spark || [], 1, 5);
    cm.appendChild(cs);
    el("span", { class: "v", text: st ? fmtCpu(st.cpu) : isRunning(c) ? "…" : "—" }, cm);
    hoverable(cm, (p) => sparkTip(p, c, 1, "CPU", fmtCpu));

    const memTd = el("td", {}, tr);
    const mm = el("div", { class: "metric" }, memTd);
    mm.appendChild(sparkline(c.spark || [], 2, 64e6));
    el("span", { class: "v", text: st && st.mem != null ? fmtBytes(st.mem) : isRunning(c) ? "…" : "—" }, mm);
    hoverable(mm, (p) => sparkTip(p, c, 2, "memory", fmtBytes));

    const net = el("td", { class: "num hide-sm" }, tr);
    if (c.network === "host") el("div", { class: "io", text: "host network", title: "Shares the server's network, so Docker can't count its traffic separately." }, net);
    else if (st) { const d = el("div", { class: "io" }, net); el("span", { text: "↓ " }, d); d.append(fmtBytes(st.net_rx)); el("br", {}, d); el("span", { text: "↑ " }, d); d.append(fmtBytes(st.net_tx)); }
    else net.textContent = "—";
    const io = el("td", { class: "num hide-sm" }, tr);
    if (st) { const d = el("div", { class: "io" }, io); el("span", { text: "R " }, d); d.append(fmtBytes(st.blk_r)); el("br", {}, d); el("span", { text: "W " }, d); d.append(fmtBytes(st.blk_w)); }
    else io.textContent = "—";

    const u = c.uptime || {};
    const upTd = el("td", {}, tr);
    const uc = el("div", { class: "upcell" }, upTd);
    const ms = el("div", { class: "mini-strip" }, uc);
    for (const cell of u.strip || []) el("i", { class: cellClass(cell) }, ms);
    el("small", { text: u.pct_24h == null ? "tracking…" : fmtUptimePct(u.pct_24h) }, uc);
    hoverable(uc, (p) => {
      tipTitle(p, `${c.name} · uptime`);
      for (const [l, key] of [["24 hours", "24h"], ["7 days", "7d"], ["30 days", "30d"]]) {
        const v = u[`pct_${key}`], cov = u[`coverage_${key}`];
        tipRow(p, null, v == null ? "—" : fmtUptimePct(v), l + (cov != null && cov < 0.98 ? ` (tracked ${Math.round(cov * 100)}%)` : ""));
      }
      tipRow(p, null, String(u.restarts_7d || 0), "restarts in 7 days");
    });

    restartButton(el("td", {}, tr), c, true);
    if (isOpen) renderDetail(body, c);
  }
  if (!rows.length) el("td", { colspan: "9", class: "dim", text: "No containers match." }, el("tr", {}, body));
  const running = data.containers.filter(isRunning).length;
  $("c-meta").textContent = `${running} running · ${data.containers.length - running} stopped`;
}

function restartButton(parent, c, small) {
  const b = el("button", { class: "btn" + (small ? " small" : ""), type: "button", text: "Restart" }, parent);
  const why = c.is_self ? "The dashboard can't restart itself. Use the Unraid Docker tab."
    : !who.can_write ? who.reason || "Read-only"
    : !data.can_restart ? "Restarts need a Docker control key on the server (not set up yet)." : null;
  if (why) { b.disabled = true; b.title = why; }
  b.addEventListener("click", (e) => { e.stopPropagation(); openRestart(c); });
  return b;
}

function renderDetail(body, c) {
  const tr = el("tr", { class: "detail" }, body);
  const td = el("td", { colspan: "9" }, tr);
  const g = el("div", { class: "detail-grid" }, td);
  const left = el("div", {}, g);
  el("h4", { text: "Details" }, left);
  const st = c.stats || {};
  const ports = (c.ports || []).map(([pub, priv, proto]) => `${pub}→${priv}${proto && proto !== "tcp" ? "/" + proto : ""}`).join(", ");
  const facts = [
    ["Up since", c.uptime && c.uptime.since ? fmtWhen(c.uptime.since) + (c.uptime.since_exact ? "" : " (approx.)") : "—"],
    ["Created", c.created ? fmtWhen(c.created) : "—"],
    ["Network", c.network || "—"],
    ["Ports", ports || (c.network === "host" ? "host network" : "none published")],
    ["Autostart", c.autostart ? `Yes${c.autostart_order != null ? ` (#${c.autostart_order + 1})` : ""}` : "No"],
    ["Memory limit", st.mem_limit ? fmtBytes(st.mem_limit) : "—"],
    ["Image + data", c.sizeRootFs != null ? fmtBytes(c.sizeRootFs) : "…"],
    ["Writable layer", c.sizeRw != null ? fmtBytes(c.sizeRw) : "…"],
    ["Logs", c.sizeLog != null ? fmtBytes(c.sizeLog) : "…"],
    ["Uptime 7d", c.uptime && c.uptime.pct_7d != null ? fmtUptimePct(c.uptime.pct_7d) : "—"],
    ["Uptime 30d", c.uptime && c.uptime.pct_30d != null ? fmtUptimePct(c.uptime.pct_30d) : "—"],
  ];
  setFacts(el("dl", { class: "facts" }, left), facts);
  el("div", { class: "dim", style: "font-family:var(--mono);font-size:11.5px;margin-top:10px;word-break:break-all", text: c.image || "" }, left);
  const strip = el("div", { class: "strip", style: "grid-template-columns:repeat(48,minmax(0,1fr));margin-top:12px" }, left);
  for (const cell of (c.uptime && c.uptime.strip) || []) {
    const x = el("div", { class: "cell " + cellClass(cell), tabindex: "0" }, strip);
    hoverable(x, (p) => {
      tipTitle(p, `${fmtClock(cell.start)}–${fmtClock(cell.end)}`);
      if (!cell.tracked) tipRow(p, null, "Not tracked", "");
      else tipRow(p, null, fmtUptimePct((cell.up / cell.tracked) * 100), "running");
    });
  }
  el("div", { class: "strip-axis" }, left).append(el("span", { text: "24h ago" }), el("span", { text: "12h" }), el("span", { text: "now" }));
  const acts = el("div", { class: "detail-actions" }, left);
  restartButton(acts, c, false);
  if (c.webui) {
    const a = el("a", { class: "btn", href: c.webui.replace("[IP]", location.hostname), target: "_blank", rel: "noopener", text: "Open web UI ↗" }, acts);
    a.style.textDecoration = "none";
  }

  const right = el("div", {}, g);
  el("h4", { text: "History" }, right);
  const ul = el("ul", { class: "ev" }, right);
  eventItems(ul, c.events || [], false);
  if (!(c.events || []).length) el("li", { class: "dim", text: "Nothing recorded yet." }, ul);
}

function eventText(e) {
  if (e.kind === "seen") return ["idle", "Tracking started", e.detail];
  if (e.kind === "restart") return ["warn", "Restarted", e.detail];
  if (e.kind === "health") return e.health === "unhealthy" ? ["crit", "Became unhealthy", ""] : e.health === "healthy" ? ["good", "Healthy again", ""] : ["warn", "Health check starting", ""];
  if (e.state === "RUNNING") return ["good", "Started", e.detail];
  if (e.state === "GONE") return ["idle", "Removed", ""];
  if (e.state === "PAUSED") return ["warn", "Paused", ""];
  return ["crit", "Stopped", e.detail];
}
function eventItems(ul, events, withName) {
  for (const e of events) {
    const [k, label, detail] = eventText(e);
    const li = el("li", {}, ul);
    el("time", { text: fmtWhen(e.ts), datetime: new Date(e.ts * 1000).toISOString() }, li);
    const w = el("div", { class: "what" }, li);
    const line = el("span", { style: "display:inline-flex;gap:6px;align-items:center" }, w);
    statusIcon(k, line);
    line.append(withName ? `${e.name}: ${label.toLowerCase()}` : label);
    if (detail) el("small", { text: detail }, w);
  }
}

/* ------------------------------------------------------------------ bars + events */
function renderBars() {
  const run = data.containers.filter((c) => c.stats);
  const bars = (host, list, val, fmt, alt) => {
    host.replaceChildren();
    const max = Math.max(...list.map(val), 1e-9);
    for (const c of list) {
      const r = el("div", { class: "bar-row" + (alt ? " alt" : "") }, host);
      el("span", { class: "n", text: c.name, title: c.name }, r);
      el("div", { class: "track" }, r).appendChild(el("div", { style: `width:${Math.max(1, (val(c) / max) * 100)}%` }));
      el("span", { class: "v", text: fmt(val(c)) }, r);
    }
    if (!list.length) el("div", { class: "empty-note", text: data.stream.connected ? "No stats yet." : "Waiting for the stats stream…" }, host);
  };
  const cpu = (c) => c.stats.cpu || 0, mem = (c) => c.stats.mem || 0;
  bars($("cpu-bars"), run.slice().sort((a, b) => cpu(b) - cpu(a)).slice(0, 10), cpu, fmtCpu, false);
  bars($("mem-bars"), run.slice().sort((a, b) => mem(b) - mem(a)).slice(0, 10), mem, fmtBytes, true);
  const total = run.reduce((s, c) => s + mem(c), 0);
  $("mem-meta").textContent = `${fmtBytes(total)} across ${run.length} containers`;
}
function renderEvents() {
  const ul = $("events");
  ul.replaceChildren();
  eventItems(ul, data.events, true);
  if (!data.events.length) el("li", { class: "dim", text: "No changes recorded yet. Tracking started when this page was added." }, ul);
}
function renderLegend() {
  const lg = $("legend");
  if (lg.children.length) return;
  for (const [cls, label] of [["good", "Running"], ["partial", "Down part of the time"], ["down", "Down"], ["", "Not tracked yet"]]) {
    const s = el("span", {}, lg);
    el("i", { style: `width:10px;height:10px;border-radius:2px;display:inline-block;background:var(${cls ? { good: "--good", partial: "--serious", down: "--crit" }[cls] : "--grid"})` }, s);
    s.append(label);
  }
}
function renderNotices() {
  const n = $("notices");
  n.replaceChildren();
  const add = (kind, text) => { const d = el("div", { class: "notice" }, n); statusIcon(kind, d); el("span", { text }, d); };
  if (!data.stream.connected) add("warn", `Live stats are reconnecting${data.stream.error ? ` (${data.stream.error})` : ""}. CPU and memory may be stale.`);
  if (data.errors && data.errors.storage) add("warn", data.errors.storage);
  if (!who.can_write) add("idle", `Read-only here. ${who.reason || ""}`);
}

/* ------------------------------------------------------------------ restart */
const RESTART_WARN = {
  cloudflared: "Remote access through *.girthy.org drops for a few seconds. If you're using this page remotely, it will reconnect on its own.",
  "binhex-plex": "Anyone streaming from Plex will be cut off.",
  "binhex-sabnzbd": "Active downloads pause and resume after it comes back.",
};
function openRestart(c) {
  const dlg = $("dlg");
  dlg.replaceChildren();
  const head = el("div", { class: "dlg-head" }, dlg);
  el("h3", { id: "dlg-title", text: `Restart ${c.name}?` }, head);
  el("p", { text: isRunning(c) ? `Stops and starts the container. It's been up ${uptimeText(c)}.` : "The container is stopped. This starts it." }, head);
  const body = el("div", { class: "dlg-body" }, dlg);
  if (RESTART_WARN[c.name]) { const n = el("div", { class: "dlg-note" }, body); statusIcon("warn", n); el("span", { text: RESTART_WARN[c.name] }, n); }
  const err = el("div", { class: "dlg-error", role: "alert" }, body);
  const foot = el("div", { class: "dlg-foot" }, dlg);
  const cancel = el("button", { class: "btn", type: "button", text: "Cancel" }, foot);
  const go = el("button", { class: "btn warnish", type: "button", text: isRunning(c) ? "Restart" : "Start" }, foot);
  cancel.addEventListener("click", () => dlg.close());
  go.addEventListener("click", async () => {
    go.disabled = cancel.disabled = true;
    go.textContent = isRunning(c) ? "Restarting…" : "Starting…";
    err.textContent = "";
    try {
      const r = await postJSON(`api/docker/${encodeURIComponent(c.name)}/restart`);
      dlg.close();
      toast("good", r.message || "Done.");
      load();
    } catch (e) {
      if (c.name === "cloudflared" && /fetch|network/i.test(e.message)) {
        dlg.close();
        toast("warn", "Lost the connection while cloudflared restarted. Reload in a few seconds.");
        return;
      }
      err.textContent = e.message;
      go.disabled = cancel.disabled = false;
      go.textContent = "Restart";
    }
  });
  dlg.showModal();
  cancel.focus();
}

/* ------------------------------------------------------------------ loading */
let lastOk = 0;
function renderUpdated() {
  const u = $("updated");
  u.replaceChildren();
  if (!lastOk) return;
  if (data && data.stream.connected) statusIcon("good", u); else statusIcon("warn", u);
  el("span", { text: `Updated ${fmtDur(Math.max(0, (Date.now() - lastOk) / 1000))} ago` }, u);
}
async function load() {
  clearTimeout(load.timer);
  try {
    data = await getJSON("api/docker");
    lastOk = Date.now();
    renderNotices();
    renderTiles();
    renderTable();
    renderBars();
    renderEvents();
    renderLegend();
  } catch (e) {
    const n = $("notices");
    n.replaceChildren();
    const d = el("div", { class: "notice" }, n);
    statusIcon("crit", d);
    el("span", { text: `Can't reach the dashboard: ${e.message}` }, d);
  }
  renderUpdated();
  load.timer = setTimeout(load, document.hidden ? 30000 : REFRESH_MS);
}
setInterval(renderUpdated, 1000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });

for (const b of $("sort").children) {
  b.setAttribute("aria-pressed", String(b.dataset.sort === sortBy));
  b.addEventListener("click", () => {
    sortBy = b.dataset.sort;
    store.set("gd-dsort", sortBy);
    for (const x of $("sort").children) x.setAttribute("aria-pressed", String(x === b));
    if (data) renderTable();
  });
}
$("filter").addEventListener("input", () => { if (data) renderTable(); });

(async () => {
  try { who = await getJSON("api/whoami"); } catch (e) { who = { can_write: false, reason: e.message }; }
  load();
})();
