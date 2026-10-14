"use strict";
// AgentPatch dashboard. Trace data is always inserted as text (never as HTML).

const $app = document.getElementById("app");
const state = { runs: null, cmp: null, cmpKey: null, exp: null, expKey: null, traceFilter: { q: "", status: "all", kase: "" }, expFilter: { q: "", show: "all", io: true }, drawerNav: null, caseTx: {} };
const SVGNS = "http://www.w3.org/2000/svg";

// ---------- DOM helpers ----------
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v; // CSSOM, allowed by the page's CSP (inline style attributes aren't)
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  add(el, kids);
  return el;
}
function add(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.appendChild(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}
function sv(tag, attrs, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, v);
  add(el, kids);
  return el;
}
const ICONS = {
  home: "M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z",
  eye: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
  flask: "M9 3h6 M10 3v6L4.5 19a1.5 1.5 0 0 0 1.3 2h12.4a1.5 1.5 0 0 0 1.3-2L14 9V3 M7 15h10",
  list: "M8 6h13 M8 12h13 M8 18h13 M3.5 6h.01 M3.5 12h.01 M3.5 18h.01",
  db: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3z M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6 M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M12 7v5l3 2",
  x: "M6 6l12 12 M18 6 6 18", up: "M12 19V5 M6 11l6-6 6 6", down: "M12 5v14 M6 13l6 6 6-6",
  search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z M20 20l-4-4", copy: "M9 9h11v11H9z M5 15H4V4h11v1",
  arrow: "M5 12h14 M13 6l6 6-6 6",
};
function icon(name, size = 16) {
  return sv("svg", { width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 1.8, "stroke-linecap": "round", "stroke-linejoin": "round", class: "ico" }, sv("path", { d: ICONS[name] }));
}
// Inline `code` spans from plain text, safely.
function md(text) {
  const parts = String(text ?? "").split("`");
  return parts.map((p, i) => (i % 2 ? h("code", null, p) : p));
}
const badge = (cls, text, title) => h("span", { class: `badge ${cls}`, title }, text ?? cls);
const fmtMs = (v) => (v == null ? "n/a" : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${v >= 10 ? Math.round(v) : (+v).toFixed(1)} ms`);
const pct = (v) => (v == null ? "n/a" : `${Math.round(v * 100)}%`);
const dur = (s) => (s.end_ms != null && s.start_ms != null ? s.end_ms - s.start_ms : null);
const rate = (s) => (s && s.trials ? `${s.passed}/${s.trials}` : "—");
const num = (v, d = 0) => (v == null ? "—" : (+v).toFixed(d));
function pretty(v) {
  if (v == null) return "null";
  if (typeof v === "string") return v;
  try { return JSON.stringify(v, null, 2); } catch { return String(v); }
}
function oneLine(v, max = 160) {
  let s = v == null ? "" : typeof v === "string" ? v : JSON.stringify(v);
  return s.length > max ? s.slice(0, max) + "…" : s;
}
function when(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${d.toLocaleString(undefined, { month: "short" })} ${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function copyText(text) { if (navigator.clipboard) navigator.clipboard.writeText(text).catch(() => {}); }

async function api(path) {
  const r = await fetch(path);
  const body = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}
const qs = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v != null && v !== "")).toString();

// ---------- routing ----------
function parseHash() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, q] = raw.split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  return { view: parts[0] || "overview", arg: parts[1], q: Object.fromEntries(new URLSearchParams(q || "")) };
}
function link(view, arg, q = {}) {
  const r = parseHash();
  const keep = {};
  if (r.q.b) keep.b = r.q.b;
  if (r.q.c) keep.c = r.q.c;
  const params = new URLSearchParams({ ...keep, ...q });
  for (const [k, v] of [...params]) if (v === "" || v == null) params.delete(k);
  const path = view === "overview" ? "/" : `/${view}${arg != null ? "/" + encodeURIComponent(arg) : ""}`;
  const s = params.toString();
  return `#${path}${s ? "?" + s : ""}`;
}
const go = (view, arg, q) => { location.hash = link(view, arg, q); };
window.addEventListener("hashchange", () => { closeDrawer(); render(); });

function pairFromUrl() {
  const { q } = parseHash();
  const d = state.runs.default || {};
  return { baseline: q.b || d.baseline, candidate: q.c || d.candidate };
}
const runById = (id) => state.runs.runs.find((r) => r.run_id === id);

async function loadCompare() {
  const p = pairFromUrl();
  if (!p.baseline || !p.candidate) return null;
  const key = `${p.baseline}|${p.candidate}`;
  if (state.cmpKey !== key) {
    state.cmp = await api(`/api/compare?${qs(p)}`);
    state.cmpKey = key;
  }
  return state.cmp;
}
async function loadExperiment() {
  const p = pairFromUrl();
  const key = `${p.baseline}|${p.candidate}`;
  if (state.expKey !== key) {
    state.exp = await api(`/api/experiment?${qs(p)}`);
    state.expKey = key;
  }
  return state.exp;
}

// ---------- chrome ----------
const NAV = [
  [null, [["overview", "Home", "home"]]],
  ["Observability", [["observability", "Coverage & drift", "eye"], ["experiments", "Experiments", "flask"], ["traces", "Traces", "list"], ["datasets", "Datasets", "db"]]],
  ["Store", [["runs", "Runs", "clock"]]],
];
const TITLES = { overview: "Home", case: "Case", observability: "Coverage & drift", experiments: "Experiments", traces: "Traces", datasets: "Datasets", runs: "Runs" };
const PAIR_VIEWS = new Set(["overview", "case", "observability", "experiments"]);

function renderNav(view) {
  const active = view === "case" ? "overview" : view;
  const nav = document.getElementById("nav");
  nav.replaceChildren(...NAV.flatMap(([head, items]) => [
    head ? h("div", { class: "nav-h" }, head) : null,
    ...items.map(([v, t, ic]) => h("a", { href: link(v), class: active === v ? "active" : "" }, icon(ic), t)),
  ]).filter(Boolean));
  document.getElementById("side-foot").textContent = state.runs ? state.runs.home : "";
}
function renderCrumbs(view, arg, extra) {
  const el = document.getElementById("crumbs");
  const parts = [];
  if (view === "case") parts.push(h("a", { href: link("overview") }, "Home"), h("span", { class: "sep" }, "/"), h("span", { class: "cur" }, arg));
  else if (view === "datasets" && arg) parts.push(h("a", { href: link("datasets") }, "Datasets"), h("span", { class: "sep" }, "/"), h("span", { class: "cur" }, arg));
  else {
    if (["observability", "experiments", "traces", "datasets"].includes(view)) parts.push(h("span", null, "Observability"), h("span", { class: "sep" }, "/"));
    parts.push(h("span", { class: "cur" }, TITLES[view] || view));
  }
  if (extra) parts.push(extra);
  el.replaceChildren(...parts);
}
function runOption(r, selected) {
  const m = r.run_id.match(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/);
  const w = m ? `${m[2]}-${m[3]} ${m[4]}:${m[5]}` : r.run_id;
  const o = h("option", { value: r.run_id, title: r.run_id }, `${r.label} · ${w} · ${r.passed}/${r.trials}`);
  if (selected) o.selected = true;
  return o;
}
function renderPair(view) {
  const el = document.getElementById("pair");
  const runs = state.runs.runs;
  if (runs.length < 2 || !PAIR_VIEWS.has(view)) { el.replaceChildren(); return; }
  const p = pairFromUrl();
  const sel = (which) => h("label", { class: "pick", title: which === "baseline" ? "A: baseline run" : "B: candidate run" },
    h("span", { class: `ab ${which === "baseline" ? "a" : "b"}` }, which === "baseline" ? "A" : "B"),
    h("select", {
      onchange: (e) => {
        const r = parseHash();
        Object.assign(r.q, choosePair(p, which === "baseline" ? "b" : "c", e.target.value));
        location.hash = link(r.view, r.arg, r.q);
      },
    }, runs.map((r) => runOption(r, r.run_id === p[which]))));
  el.replaceChildren(sel("baseline"), h("button", {
    title: "swap baseline and candidate", onclick: () => { const r = parseHash(); r.q.b = p.candidate; r.q.c = p.baseline; location.hash = link(r.view, r.arg, r.q); },
  }, "⇄"), sel("candidate"));
}

// Picking the run that's currently on the other side swaps the two instead of comparing a run with itself.
function choosePair(p, side, id) {
  const other = side === "b" ? p.candidate : p.baseline;
  const mine = side === "b" ? p.baseline : p.candidate;
  const q = { [side]: id, [side === "b" ? "c" : "b"]: other };
  if (id === other) q[side === "b" ? "c" : "b"] = mine;
  return q;
}

let lastPage = null;
async function render() {
  const { view, arg, q } = parseHash();
  const page = `${view}/${arg || ""}/${q.tab || ""}`;
  if (page !== lastPage) { lastPage = page; window.scrollTo(0, 0); }
  try {
    state.runs = await api("/api/runs");
    renderNav(view);
    renderPair(view);
    renderCrumbs(view, arg);
    $app.classList.toggle("full", view === "experiments" || view === "traces");
    if (view === "runs") return renderRuns();
    if (!state.runs.runs.length) return renderEmpty();
    if (view === "traces") return await renderTraces(q);
    if (view === "datasets") return await renderDatasets(arg, q);
    if (state.runs.runs.length < 2 && view !== "observability") return renderSingle();
    if (view === "case") return await renderCase(arg, q);
    if (view === "observability") return await renderObservability();
    if (view === "experiments") return await renderExperiments();
    return await renderOverview();
  } catch (e) {
    $app.replaceChildren(h("div", { class: "panel err" }, h("h2", null, "Something went wrong"), String(e.message || e)));
  }
}

function renderEmpty() {
  $app.replaceChildren(h("div", { class: "panel empty-state" },
    h("h1", null, "No runs yet"),
    h("p", { class: "muted" }, `AgentPatch stores runs in ${state.runs.home}. Create two (a baseline and a candidate) and they'll be compared here.`),
    h("pre", null, "# see it work in 10 seconds (offline LangGraph demo)\nagentpatch demo\n\n# or with your own agent\nagentpatch init          # writes agentpatch.toml, detects your LangGraph graph\nagentpatch run --label baseline\n# ...change the agent...\nagentpatch run --label candidate\nagentpatch ui")));
}

async function renderSingle() {
  const r = state.runs.runs[0];
  const data = await api(`/api/run?run=${encodeURIComponent(r.run_id)}`);
  $app.replaceChildren(
    h("div", { class: "panel callout note" }, h("b", null, "Only one run so far. "),
      "Record a second run (e.g. after changing your agent) to see what changed: ", h("code", null, "agentpatch run --label candidate")),
    runPanel(data));
}

function runPanel(data) {
  return h("div", { class: "panel" }, h("h2", null, `Run ${data.run.label} `, h("span", { class: "muted small" }, data.run.run_id)),
    h("table", null, h("tr", null, h("th", null, "case"), h("th", null, "trials"), h("th", null, "failed checks")),
      data.cases.map((c) => h("tr", null, h("td", null, h("code", null, c.case_id)),
        h("td", null, h("div", { class: "trials" }, c.trials.map((t) => h("span", { class: `trial ${t.outcome}` }, t.outcome === "pass" ? "✓" : t.outcome === "fail" ? "✗" : "?")))),
        h("td", { class: "small" }, [...new Set(c.trials.flatMap((t) => t.failed))].map((x) => h("code", null, x)), " ")))),
    h("p", { class: "small" }, h("a", { href: link("traces", null, { run: data.run.run_id }) }, "Browse this run's traces →")));
}

// ---------- drawer ----------
function openDrawer(content, { wide = false, nav = null } = {}) {
  const root = document.getElementById("drawer-root");
  root.replaceChildren(h("div", { class: "scrim", onclick: closeDrawer }), h("div", { class: `drawer ${wide ? "wide" : ""}`, role: "dialog" }, content));
  document.body.classList.add("noscroll");
  state.drawerNav = nav;
}
function closeDrawer() {
  document.getElementById("drawer-root").replaceChildren();
  document.body.classList.remove("noscroll");
  state.drawerNav = null;
}
document.addEventListener("keydown", (e) => {
  if (!state.drawerNav && e.key !== "Escape") return;
  if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement && document.activeElement.tagName)) return;
  if (e.key === "Escape") closeDrawer();
  else if ((e.key === "ArrowDown" || e.key === "j") && state.drawerNav.next) { e.preventDefault(); state.drawerNav.next(); }
  else if ((e.key === "ArrowUp" || e.key === "k") && state.drawerNav.prev) { e.preventDefault(); state.drawerNav.prev(); }
});
function drawerHead(title, sub, { prev, next, actions } = {}) {
  return h("div", { class: "dw-head" },
    prev !== undefined ? h("div", { style: "display:flex;gap:4px" },
      h("button", { class: "icon-btn", title: "previous (↑)", disabled: !prev || null, onclick: prev || null }, icon("up", 14)),
      h("button", { class: "icon-btn", title: "next (↓)", disabled: !next || null, onclick: next || null }, icon("down", 14))) : null,
    h("div", { class: "grow" }, h("div", { class: "dw-title" }, title), sub ? h("div", { class: "dw-sub" }, sub) : null),
    actions || null,
    h("button", { class: "icon-btn", title: "close (Esc)", onclick: closeDrawer }, icon("x", 14)));
}

// ---------- JSON / IO viewers ----------
function maybeJson(x) {
  if (typeof x !== "string") return undefined;
  const t = x.trim();
  if (!(t.startsWith("{") || t.startsWith("["))) return undefined;
  try { const p = JSON.parse(t); return p && typeof p === "object" ? p : undefined; } catch { return undefined; }
}
function scalar(v) {
  if (v === null || v === undefined) return h("span", { class: "j-null" }, "null");
  if (typeof v === "string") return h("span", { class: "j-str" }, v === "" ? '""' : v);
  if (typeof v === "boolean") return h("span", { class: "j-bool" }, String(v));
  if (typeof v === "number") return h("span", { class: "j-num" }, String(v));
  return String(v);
}
function jsonView(v, depth = 0, openDepth = 2) {
  if (v === null || typeof v !== "object") return scalar(v);
  const entries = Array.isArray(v) ? v.map((x, i) => [i, x]) : Object.entries(v);
  if (!entries.length) return h("span", { class: "j-empty" }, Array.isArray(v) ? "[]" : "{}");
  return h("div", { class: "jtree" }, entries.map(([k, x]) => jsonRow(k, x, depth, openDepth)));
}
function jsonRow(k, x, depth, openDepth) {
  const parsed = maybeJson(x);
  const val = parsed !== undefined ? parsed : x;
  const n = val && typeof val === "object" ? (Array.isArray(val) ? val.length : Object.keys(val).length) : 0;
  if (!n) return h("div", { class: "jrow" }, h("span", { class: "jkey" }, String(k)), h("span", { class: "jval" }, val && typeof val === "object" ? jsonView(val) : scalar(val)));
  const d = h("details", { class: "jrow jnest", open: depth < openDepth ? true : null },
    h("summary", null, h("span", { class: "jkey" }, String(k)),
      h("span", { class: "jmeta" }, Array.isArray(val) ? `[${n} item${n === 1 ? "" : "s"}]` : `{${n} key${n === 1 ? "" : "s"}}`, parsed !== undefined ? " · JSON string" : "")));
  let filled = false;
  const fill = () => { if (!filled && d.open) { filled = true; d.appendChild(jsonView(val, depth + 1, openDepth)); } };
  d.addEventListener("toggle", fill);
  fill();
  return d;
}
function valueView(v) {
  const parsed = maybeJson(v);
  if (parsed !== undefined) return jsonView(parsed);
  if (typeof v === "string") return h("div", { class: "msg-text" }, v);
  return jsonView(v);
}
const ROLE_LABEL = { system: "System", human: "User", user: "User", ai: "AI", assistant: "AI", tool: "Tool" };
const roleClass = (r) => ({ human: "user", user: "user", ai: "ai", assistant: "ai", system: "system", tool: "tool" })[r] || "other";
function chatMessages(v) {
  const m = Array.isArray(v) ? v : v && typeof v === "object" ? (Array.isArray(v.messages) ? v.messages : v.role ? [v] : null) : null;
  if (!m || !m.length || !m.every((x) => x && typeof x === "object" && ("role" in x || "type" in x))) return null;
  return m;
}
function chatView(msgs, v) {
  const extra = v && typeof v === "object" && !Array.isArray(v) && !v.role ? Object.fromEntries(Object.entries(v).filter(([k]) => k !== "messages")) : null;
  return h("div", { class: "chat" }, msgs.map((m) => {
    const role = m.role || m.type;
    const calls = m.tool_calls || [];
    return h("div", { class: `msg role-${roleClass(role)}` },
      h("div", { class: "msg-role" }, ROLE_LABEL[role] || role, m.name ? h("span", { class: "muted" }, `· ${m.name}`) : null, m.status && m.status !== "success" ? badge("error", m.status) : null),
      m.content ? valueView(m.content) : null,
      calls.map((tc) => h("div", { class: "toolcall" }, h("div", { class: "tc-name" }, `${tc.name}()`), jsonView(tc.args || {}))),
      !m.content && !calls.length ? h("span", { class: "muted small" }, "(empty)") : null);
  }), extra && Object.keys(extra).length ? h("div", { class: "msg" }, h("div", { class: "msg-role" }, "Other fields"), jsonView(extra)) : null);
}
function ioView(v) {
  const msgs = chatMessages(v);
  const modes = msgs ? ["Chat", "Fields", "Raw"] : ["Fields", "Raw"];
  let mode = modes[0];
  const body = h("div");
  const seg = h("div", { class: "seg small" });
  const draw = () => {
    seg.replaceChildren(...modes.map((m) => h("button", { class: m === mode ? "on" : "", onclick: () => { mode = m; draw(); } }, m)));
    body.replaceChildren(v == null ? h("span", { class: "j-null" }, "null") : mode === "Chat" ? chatView(msgs, v) : mode === "Fields" ? valueView(v) : h("pre", { class: "raw" }, pretty(v)));
  };
  draw();
  return h("div", null, h("div", { class: "io-bar" }, seg, h("span", { class: "grow" }),
    h("button", { class: "icon-btn", title: "copy as JSON", onclick: () => copyText(pretty(v)) }, icon("copy", 13))), body);
}

// ---------- trace explorer ----------
const KIND_GLYPH = { agent: "A", node: "N", llm: "AI", tool: "T", attempt: "↻", router: "R", retriever: "D", other: "·" };
const kindIcon = (kind, title) => h("span", { class: `ki ${kind}`, title: title || kind }, KIND_GLYPH[kind] || "·");

/** Tree of steps on the left, the selected step's checks/input/output/attributes on the right. */
function traceExplorer(T, { select = null, compact = false, waterfall = !compact, focus = null } = {}) {
  const trace = T.trace;
  const steps = trace.steps;
  const byId = Object.fromEntries(steps.map((s) => [s.id, s]));
  const kids = {};
  for (const s of steps) (kids[s.parent_id] = kids[s.parent_id] || []).push(s.id);
  const depth = {};
  for (const s of steps) depth[s.id] = s.parent_id && depth[s.parent_id] != null ? depth[s.parent_id] + 1 : s.parent_id && s.parent_id.startsWith("missing:") ? 1 : 0;
  const checks = (T.result && T.result.checks) || [];
  const gaps = T.gaps || [];
  const root = steps.find((s) => !s.parent_id) || steps[0];
  const total = trace.duration_ms || (root && dur(root)) || 1;
  let sel = select && byId[select] ? select : root && root.id;
  let tab = "feedback";
  const collapsed = new Set();

  const citedBy = (id) => checks.filter((c) => (c.evidence || []).includes(id));
  const gapsFor = (id) => gaps.filter((g) => g.step === id);
  const rows = {};
  const tree = h("div", { class: "tx-tree" });
  for (const s of steps) {
    const hasKids = (kids[s.id] || []).length > 0;
    const caret = h("span", { class: "caret" }, hasKids ? "▾" : "");
    if (hasKids) caret.addEventListener("click", (e) => { e.stopPropagation(); collapsed.has(s.id) ? collapsed.delete(s.id) : collapsed.add(s.id); caret.textContent = collapsed.has(s.id) ? "▸" : "▾"; applyCollapse(); });
    const g = gapsFor(s.id);
    const lvl = g.some((x) => x.level === "verified") ? "verified" : g.some((x) => x.level === "possible") ? "possible" : null;
    const failed = citedBy(s.id).some((c) => c.passed === false) && s !== root;
    const left = Math.max(0, (s.start_ms || 0) / total * 100), width = Math.max(0.4, ((s.end_ms ?? s.start_ms) - s.start_ms) / total * 100);
    const row = h("div", { class: `tx-row ${s.status === "error" ? "err" : ""} ${s.parent_id && s.parent_id.startsWith("missing:") ? "missing" : ""}`, title: s.id, onclick: () => selectStep(s.id) },
      h("span", { style: `width:${depth[s.id] * 12}px;flex:none` }), caret, kindIcon(s.kind),
      h("span", { class: "nm" }, s.name, s.kind === "llm" && s.attributes && (s.attributes.requested_tools || []).length && !compact ? h("span", { class: "muted" }, ` → ${s.attributes.requested_tools.join(", ")}`) : null),
      focus === s.id ? h("span", { class: "marker", title: "likely failure point" }, "◎") : null,
      failed ? h("span", { class: "badge fail", title: "a failed check cites this step" }, "✗") : null,
      lvl ? h("span", { class: `flag ${lvl}`, title: `${lvl} instrumentation gap` }) : null,
      h("span", { class: "du" }, fmtMs(dur(s))),
      waterfall ? h("span", { class: "wf" }, h("span", { style: `left:${left.toFixed(2)}%;width:${Math.min(100 - left, width).toFixed(2)}%` })) : null);
    rows[s.id] = row;
    tree.appendChild(row);
  }
  function applyCollapse() {
    const hidden = new Set();
    for (const s of steps) if (hidden.has(s.parent_id) || collapsed.has(s.parent_id)) hidden.add(s.id);
    for (const s of steps) rows[s.id].classList.toggle("hidden", hidden.has(s.id));
  }
  const detail = h("div", { class: "tx-detail" });
  function drawDetail() {
    const s = byId[sel];
    if (!s) { detail.replaceChildren(h("div", { class: "tx-body muted" }, "No steps recorded.")); return; }
    const isRoot = s === root;
    const fb = isRoot ? checks : citedBy(s.id);
    const gp = isRoot ? gaps : gapsFor(s.id);
    const attrs = Object.entries(s.attributes || {});
    const TABS = [["feedback", "Feedback", fb.length + gp.length], ["input", "Input"], ["output", "Output"], ["attributes", "Attributes", attrs.length]];
    const tabs = h("div", { class: "tabs" }, TABS.map(([k, t, n]) => h("button", { class: tab === k ? "on" : "", onclick: () => { tab = k; drawDetail(); } }, t, n ? h("span", { class: "n" }, n) : null)));
    let body;
    if (tab === "feedback") {
      body = [
        fb.length ? h("div", { class: "sect" }, h("div", { class: "sect-h" }, isRoot ? "Checks on this trace" : "Checks citing this step", h("span", { class: "grow" }),
          h("span", { class: "muted small" }, `${fb.filter((c) => c.passed === true).length}/${fb.length} pass`)),
          h("div", { class: "sect-b" }, fb.map((c) => h("div", { class: "fb" },
            h("div", null, badge(c.passed === true ? "pass" : c.passed === false ? "fail" : "unknown", c.passed === true ? "pass" : c.passed === false ? "fail" : "can't verify")),
            h("div", null, h("div", null, md(c.description)), h("div", { class: "d" }, md(c.detail)),
              !isRoot ? null : (c.evidence || []).filter((e) => e !== root.id).slice(0, 4).map((e) => h("button", { class: "cite", onclick: () => selectStep(e) }, e))))))) : null,
        gp.length ? h("div", { class: "sect" }, h("div", { class: "sect-h" }, "Instrumentation gaps"),
          h("div", { class: "sect-b" }, gp.map((g) => h("div", { class: "fb" }, h("div", null, badge(g.level)),
            h("div", null, md(g.message), isRoot && g.step && byId[g.step] ? h("button", { class: "cite", onclick: () => selectStep(g.step) }, g.step) : null))))) : null,
        !fb.length && !gp.length ? h("p", { class: "muted small" }, isRoot ? "This trace has no check results." : "No check or instrumentation gap cites this step. Select the root step to see every check.") : null,
      ];
    } else if (tab === "input") body = h("div", { class: "sect" }, h("div", { class: "sect-h" }, "Input"), h("div", { class: "sect-b" }, ioView(s.input)));
    else if (tab === "output") body = [s.error ? h("div", { class: "callout note errline" }, s.error) : null, h("div", { class: "sect" }, h("div", { class: "sect-h" }, "Output"), h("div", { class: "sect-b" }, ioView(s.output)))];
    else body = [
      h("div", { class: "sect" }, h("div", { class: "sect-h" }, "Step"), h("div", { class: "sect-b" },
        [["id", s.id], ["parent", s.parent_id || "—"], ["kind", s.kind], ["status", s.status], ["start", `${num(s.start_ms, 1)} ms after trace start`],
          ["end", s.end_ms == null ? "never ended" : `${num(s.end_ms, 1)} ms`], ["duration", fmtMs(dur(s))]].map(([k, v]) => h("div", { class: "attr" }, h("span", { class: "k" }, k), h("span", { class: "mono" }, v))))),
      attrs.length ? h("div", { class: "sect" }, h("div", { class: "sect-h" }, "Attributes"), h("div", { class: "sect-b" }, jsonView(s.attributes, 0, 3))) : null,
      isRoot ? h("div", { class: "sect" }, h("div", { class: "sect-h" }, "Trace"), h("div", { class: "sect-b" },
        [["trace id", trace.trace_id], ["source", trace.source], ["started", when(trace.started_at)], ["status", trace.status]].map(([k, v]) => h("div", { class: "attr" }, h("span", { class: "k" }, k), h("span", { class: "mono" }, v ?? "—"))),
        trace.meta && Object.keys(trace.meta).length ? jsonView(trace.meta) : null)) : null,
    ];
    detail.replaceChildren(h("div", { class: "tx-head" },
      h("div", { class: "tx-title" }, kindIcon(s.kind), s.name, s.status === "error" ? badge("error", "error") : null),
      h("div", { class: "tx-sub" }, h("span", { class: "mono" }, s.id), h("span", null, fmtMs(dur(s))),
        s.kind === "llm" && s.attributes && s.attributes.usage ? h("span", null, `${s.attributes.usage.input_tokens ?? "?"} in / ${s.attributes.usage.output_tokens ?? "?"} out tokens`) : null,
        s.attributes && s.attributes.model ? h("span", null, s.attributes.model) : null),
      tabs), h("div", { class: "tx-body" }, body));
  }
  function selectStep(id) {
    if (!byId[id]) return;
    if (rows[sel]) rows[sel].classList.remove("sel");
    sel = id;
    let p = byId[id].parent_id;
    while (p && byId[p]) { if (collapsed.has(p)) { collapsed.delete(p); } p = byId[p].parent_id; }
    applyCollapse();
    rows[id].classList.add("sel");
    const r = rows[id];
    if (r.offsetTop < tree.scrollTop || r.offsetTop > tree.scrollTop + tree.clientHeight - 28) tree.scrollTop = r.offsetTop - tree.clientHeight / 2;
    if (tab === "feedback" && byId[id] !== root && !citedBy(id).length && !gapsFor(id).length) tab = "output";
    drawDetail();
  }
  const el = h("div", { class: `tx ${compact ? "compact" : ""}` }, tree, detail);
  el.selectStep = selectStep;
  if (sel) { rows[sel].classList.add("sel"); if (sel !== (root && root.id)) tab = citedBy(sel).length || gapsFor(sel).length ? "feedback" : "output"; }
  drawDetail();
  return el;
}

// ---------- charts ----------
function barChart(cats, series, { max = null, fmt = (v) => v, height = 150, rotate = false, width = 520 } = {}) {
  const W = width, H = height, padL = 46, padB = rotate ? 44 : 22, padT = 8;
  const vals = series.flatMap((s) => s.values).filter((v) => v != null);
  const top = max ?? (Math.max(1e-9, ...vals) * 1.15);
  const g = sv("g");
  for (let i = 0; i <= 4; i++) {
    const y = padT + (H - padT - padB) * (1 - i / 4);
    g.appendChild(sv("line", { x1: padL, x2: W, y1: y, y2: y, stroke: "#efefee" }));
    g.appendChild(sv("text", { x: padL - 6, y: y + 3, "text-anchor": "end", "font-size": 9.5, fill: "#a3a3a3" }, fmt(top * i / 4)));
  }
  const bw = (W - padL) / cats.length;
  const inner = Math.min(26, (bw * 0.7) / series.length);
  cats.forEach((c, i) => {
    const x0 = padL + bw * i + (bw - inner * series.length) / 2;
    series.forEach((s, j) => {
      const v = s.values[i];
      const hgt = v == null ? 0 : (H - padT - padB) * Math.min(1, v / top);
      const r = sv("rect", { x: x0 + j * inner + 1, y: H - padB - hgt, width: inner - 2, height: Math.max(v == null ? 0 : 1, hgt), rx: 2, fill: s.color },
        sv("title", null, `${s.name} · ${c}: ${v == null ? "n/a" : fmt(v)}`));
      g.appendChild(r);
    });
    const tx = padL + bw * i + bw / 2;
    g.appendChild(rotate
      ? sv("text", { x: tx, y: H - padB + 10, "text-anchor": "end", "font-size": 9.5, fill: "#737373", transform: `rotate(-28 ${tx} ${H - padB + 10})` }, c)
      : sv("text", { x: tx, y: H - 6, "text-anchor": "middle", "font-size": 10, fill: "#737373" }, c));
  });
  return sv("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" }, g);
}
function stackedChart(cats, stacks, { fmt = (v) => v, height = 150 } = {}) {
  const W = 260, H = height, padL = 38, padB = 22, padT = 8;
  const totals = cats.map((_, i) => stacks.reduce((a, s) => a + (s.values[i] || 0), 0));
  const top = Math.max(1, ...totals) * 1.15;
  const g = sv("g");
  for (let i = 0; i <= 4; i++) {
    const y = padT + (H - padT - padB) * (1 - i / 4);
    g.appendChild(sv("line", { x1: padL, x2: W, y1: y, y2: y, stroke: "#efefee" }));
    g.appendChild(sv("text", { x: padL - 6, y: y + 3, "text-anchor": "end", "font-size": 9.5, fill: "#a3a3a3" }, fmt(top * i / 4)));
  }
  const bw = (W - padL) / cats.length;
  cats.forEach((c, i) => {
    let y = H - padB;
    const x = padL + bw * i + bw * 0.3;
    for (const s of stacks) {
      const v = s.values[i] || 0;
      const hgt = (H - padT - padB) * v / top;
      y -= hgt;
      g.appendChild(sv("rect", { x, y, width: bw * 0.4, height: hgt, fill: s.color }, sv("title", null, `${c} · ${s.name}: ${fmt(v)}`)));
    }
    g.appendChild(sv("text", { x: x + bw * 0.2, y: H - 6, "text-anchor": "middle", "font-size": 10, fill: "#737373" }, c));
  });
  return sv("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" }, g);
}
const legend = (items) => h("div", { class: "legend small" }, items.map(([name, color]) => h("span", null, h("span", { class: "leg-sw", style: `background:${color}` }), name)));
const chartCard = (title, sub, svgEl, leg) => h("div", { class: "chart" }, h("div", { class: "ct" }, title), h("div", { class: "cs" }, sub), svgEl, leg || null);
const COLOR_A = "#14b8a6", COLOR_B = "#8b5cf6";

// ---------- overview ----------
const CHANGE_LABEL = { regressed: "regressed", intermittent: "intermittent", fixed: "fixed", still_failing: "still failing", unchanged: "unchanged", added: "new case", removed: "removed" };
const PLAIN_CHANGE = { regressed: "got worse", intermittent: "sometimes", fixed: "fixed", still_failing: "still broken", unchanged: "same", added: "new", removed: "removed" };
const OBS_LABEL = { ok: "Observable", check: "Check coverage", gaps: "Gaps found" };
const PLAIN_OBS = { ok: "Recorded", check: "Worth a look", gaps: "Gaps" };

function deTick(s) { return String(s || "").replace(/`/g, ""); }

function plainHeadline(cmp) {
  const n = (ch) => cmp.cases.filter((r) => r.change === ch).length;
  const worse = n("regressed") + n("intermittent");
  const parts = [];
  if (worse) parts.push(`${worse} scenario${worse === 1 ? "" : "s"} got worse`);
  if (n("fixed")) parts.push(`${n("fixed")} fixed`);
  if (n("still_failing")) parts.push(`${n("still_failing")} still broken`);
  if (cmp.observability.status === "gaps") parts.push("recording has gaps");
  else if (cmp.observability.status === "check") parts.push("recording needs a look");
  return (parts.join(", ") || "Nothing got worse") + ".";
}

function attentionLine(r) {
  const bad = r.checks.filter((c) => c.change === "regressed" || c.change === "failing");
  const text = bad.slice(0, 2).map((c) => deTick(c.description)).join(" · ");
  if (text) return text;
  if (r.change === "intermittent") return "Fails on some runs, not every run.";
  if (r.change === "still_failing") return "This was already failing before the change.";
  return "This scenario got worse.";
}

function recordingBlurb(cmp) {
  const ob = cmp.observability;
  const lead = ob.status === "ok"
    ? "Recording kept up with this change. The traces still cover what the checks rely on."
    : ob.status === "gaps"
      ? "Part of the new behavior is not in the trace, so a pass rate alone can miss it."
      : "Nothing is clearly missing, but a few spots are worth a look before you trust the numbers.";
  const extra = cmp.findings
    .filter((f) => f.severity !== "info" && ["observability", "drift", "coverage"].includes(f.category))
    .slice(0, 2)
    .map((f) => deTick(f.title).replace(/^(Verified gap|Possible gap):\s*/i, ""));
  return { lead, extra };
}

async function renderOverview() {
  const cmp = await loadCompare();
  if (!cmp) return renderSingle();
  const count = (ch) => cmp.cases.filter((r) => r.change === ch).length;
  const worse = count("regressed") + count("intermittent");
  const attention = sortCases(cmp.cases.filter((r) => ["regressed", "intermittent", "still_failing"].includes(r.change)));
  const fixed = cmp.cases.filter((r) => r.change === "fixed");
  const ob = cmp.observability;
  const rec = recordingBlurb(cmp);
  const stat = (k, v, bad) => h("div", { class: "stat" }, h("div", { class: "k" }, k), h("div", { class: `v ${bad ? "bad" : ""}` }, v));

  const verdict = h("div", { class: `panel verdict ${cmp.verdict}` },
    h("span", { class: "label" }, cmp.verdict.replace("_", " ").toUpperCase()),
    h("div", null, h("div", { class: "headline" }, plainHeadline(cmp)),
      h("div", { class: "muted small" }, "The short version is below. Traces, scores, and step-by-step evidence are one click away."),
      h("div", { class: "chips" }, runChip("a", cmp.baseline), h("span", { class: "muted" }, "→"), runChip("b", cmp.candidate))));

  const numbers = h("div", { class: "stats" },
    stat("Got worse", worse, worse > 0),
    stat("Fixed", count("fixed"), false),
    stat("Still broken", count("still_failing"), count("still_failing") > 0),
    stat("Recording", PLAIN_OBS[ob.status], ob.status === "gaps"));

  const list = h("div", { class: "panel" },
    h("h2", null, "What needs a look"),
    h("p", { class: "muted small" }, "Each row is a scenario, not a trace. Open it when you want the step that failed."),
    attention.length ? attention.map((r) => h("a", { class: "glance", href: link("case", r.case_id) },
      h("div", null, h("div", { class: "what" }, r.description || r.case_id),
        r.description ? h("div", { class: "small muted" }, r.case_id) : null,
        h("div", { class: "why small" }, attentionLine(r))),
      badge(r.change, PLAIN_CHANGE[r.change]),
      h("span", { class: "rate" }, `${rate(r.baseline)} → ${rate(r.candidate)}`)))
      : h("p", null, "Nothing got worse, and nothing was already failing."),
    fixed.length ? h("p", { class: "small muted" }, "Fixed: ", fixed.map((r, i) => [i ? ", " : "", h("a", { href: link("case", r.case_id) }, r.description || r.case_id)])) : null);

  const recording = h("div", { class: `panel callout ${ob.status === "ok" ? "okay" : ob.status === "gaps" ? "point" : "note"}` },
    h("h2", null, "Still being recorded? ", badge(ob.status, PLAIN_OBS[ob.status])),
    h("p", null, rec.lead),
    rec.extra.length ? h("ul", null, rec.extra.map((t) => h("li", null, t))) : null,
    h("p", { class: "small muted" }, "Detected from the traces themselves. When the agent gains a step or a tool, this updates without rewiring the dashboard. Work that never emits a trace still can't be seen."),
    h("div", { class: "small" }, h("a", { href: link("observability") }, "What isn't recorded"), "  ·  ",
      h("a", { href: link("traces", null, { run: cmp.candidate.run_id }) }, "Browse traces"), "  ·  ",
      h("a", { href: link("experiments") }, "Scores by scenario")));

  const all = cmp.findings;
  const engineer = h("details", { class: "panel" },
    h("summary", null, `Engineer details (${all.length} findings, evidence and per-scenario checks)`),
    h("p", { class: "small muted" }, cmp.headline),
    all.map(findingEl),
    cmp.notes.map((n) => h("div", { class: "callout note small" }, n)),
    h("h3", null, "Every scenario"),
    h("table", null, h("tr", null, h("th", null, "scenario"), h("th", null, "result"), h("th", { class: "num" }, "before"), h("th", { class: "num" }, "after"), h("th", { class: "num" }, "speed")),
      sortCases(cmp.cases).map((r) => h("tr", { class: "clickable", onclick: () => go("case", r.case_id) },
        h("td", null, r.description ? h("div", null, r.description) : null, h("code", null, r.case_id)),
        h("td", null, badge(r.change, PLAIN_CHANGE[r.change])),
        h("td", { class: "num" }, rate(r.baseline)), h("td", { class: "num" }, rate(r.candidate)),
        h("td", { class: "num" }, latencyCell(r.latency))))));

  $app.replaceChildren(verdict, numbers, list, recording, engineer, metricsPanel(cmp));
}

function runChip(side, m) {
  const git = m.git && m.git.commit ? ` @ ${m.git.commit}${m.git.dirty ? "+dirty" : ""}` : "";
  return h("span", { class: "chip", title: m.run_id }, h("span", { class: `sw2 ${side}` }, side === "a" ? "A" : "B"), h("b", null, m.label),
    h("span", { class: "meta" }, `${m.run_id}${git}${m.repeats ? `, ${m.repeats}x per case` : ""}`));
}
function sortCases(cases) {
  const order = { regressed: 0, intermittent: 1, still_failing: 2, fixed: 3, added: 4, removed: 5, unchanged: 6 };
  return [...cases].sort((a, b) => (order[a.change] - order[b.change]) || a.case_id.localeCompare(b.case_id));
}
function latencyCell(l) {
  if (!l) return "—";
  const cls = l.delta_pct >= 25 && l.delta_ms >= 50 ? "delta-bad" : l.delta_pct <= -25 && l.delta_ms <= -50 ? "delta-good" : "";
  return h("span", null, `${fmtMs(l.baseline_p50)} → `, h("span", { class: cls }, fmtMs(l.candidate_p50)));
}
function findingEl(f) {
  const ev = (f.evidence || []).filter((e) => e && e.case_id != null);
  return h("div", { class: `finding ${f.severity}` },
    h("div", { class: "fid" }, f.id),
    h("div", null, h("div", { class: "title" }, md(f.title)),
      f.detail ? h("div", { class: "detail" }, md(f.detail)) : null,
      h("div", { class: "meta" }, badge(f.severity), h("span", { class: "muted" }, f.category),
        ev.slice(0, 2).map((e) => h("a", { href: link("case", e.case_id, { ct: e.trial, step: e.step || "" }) },
          `${e.case_id} trial ${e.trial}${e.step ? " → " + e.step : ""}`)),
        !ev.length && f.cases && f.cases.length ? f.cases.slice(0, 4).map((c) => h("a", { href: link("case", c) }, c)) : null)));
}

const METRICS = [["success_rate", "Success rate", pct], ["cases_all_pass", "Cases passing every trial", (v) => v],
  ["p50_ms", "Latency p50", fmtMs], ["p95_ms", "Latency p95", fmtMs], ["tool_calls_per_run", "Tool calls / run", (v) => v],
  ["tool_errors_per_run", "Tool errors / run", (v) => v], ["llm_calls_per_run", "Model calls / run", (v) => v],
  ["tokens_per_run", "Tokens / run", (v) => (v == null ? "n/a" : Math.round(v))], ["run_errors", "Runs that crashed", (v) => v]];
const HIGHER_IS_WORSE = new Set(["p50_ms", "p95_ms", "tool_errors_per_run", "run_errors", "tokens_per_run", "llm_calls_per_run"]);

function metricsPanel(cmp) {
  const b = cmp.overall.baseline, c = cmp.overall.candidate;
  return h("div", { class: "panel" }, h("details", null, h("summary", null, "Aggregate metrics (these can hide per-case changes; the findings above don't)"),
    h("table", null, h("tr", null, h("th", null, "metric"), h("th", { class: "num" }, "baseline"), h("th", { class: "num" }, "candidate")),
      METRICS.map(([k, label, f]) => {
        const worse = c[k] != null && b[k] != null && (HIGHER_IS_WORSE.has(k) ? c[k] > b[k] * 1.1 : k === "success_rate" || k === "cases_all_pass" ? c[k] < b[k] : false);
        const extra = k === "success_rate" ? ` (${b.passed}/${b.trials})` : k === "cases_all_pass" ? `/${b.cases}` : "";
        const extraC = k === "success_rate" ? ` (${c.passed}/${c.trials})` : k === "cases_all_pass" ? `/${c.cases}` : "";
        return h("tr", null, h("td", null, label), h("td", { class: "num" }, f(b[k]) + extra), h("td", { class: `num ${worse ? "delta-bad" : ""}` }, f(c[k]) + extraC));
      }))));
}

// ---------- case view ----------
async function renderCase(caseId, q) {
  const p = pairFromUrl();
  const params = { baseline: p.baseline, candidate: p.candidate, case: caseId, btrial: q.bt, ctrial: q.ct };
  const data = await api(`/api/case?${qs(params)}`);
  const { case: row, baseline: B, candidate: C, alignment, diagnosis: D } = data;
  const lpId = D && D.likely_failure_point && D.likely_failure_point.step.id;
  const focus = q.step || lpId || null;

  const header = h("div", { class: "panel" },
    h("div", { class: "page-head", style: "margin-bottom:6px" },
      h("div", null, h("h1", null, h("code", null, row.case_id), " ", badge(row.change, CHANGE_LABEL[row.change])),
        h("div", { class: "muted" }, `baseline ${rate(row.baseline)} trials pass → candidate ${rate(row.candidate)}`,
          row.latency ? ` · p50 ${fmtMs(row.latency.baseline_p50)} → ${fmtMs(row.latency.candidate_p50)}` : "")),
      h("div", { class: "head-actions" }, h("a", { class: "btn", href: link("experiments") }, icon("flask", 14), "All cases side by side"))),
    h("h3", null, "Checks"),
    h("table", null, h("tr", null, h("th", null, "check"), h("th", { class: "num" }, "baseline"), h("th", { class: "num" }, "candidate"), h("th", null, "")),
      row.checks.map((c) => h("tr", null, h("td", null, md(c.description)),
        h("td", { class: "num" }, c.baseline ? `${c.baseline.passed}/${row.baseline.trials}` : "—"),
        h("td", { class: "num" }, c.candidate ? `${c.candidate.passed}/${row.candidate.trials}` : "—"),
        h("td", null, ["regressed", "fixed", "failing"].includes(c.change) ? badge(c.change) : null)))));

  const picker = h("div", { class: "panel toolbar" },
    trialPicker("Baseline trial", B, "bt"), trialPicker("Candidate trial", C, "ct"),
    h("span", { class: "muted small" }, "Failing trials are marked ✗. Pick any pair to compare."));

  const diag = D ? diagnosisPanel(D, C, B) : h("div", { class: "panel callout okay" },
    C ? `Candidate trial ${C.trial} passed every check.` : "No candidate trial for this case.",
    C && C.result && C.result.checks.some((c) => c.passed === null) ? " Some checks couldn't be verified." : "");

  const diffPanel = h("div", { class: "panel" }, h("h2", null, "Step-by-step: baseline vs candidate"),
    h("div", { class: "legend" },
      h("span", null, h("span", { class: "sw", style: "background:var(--add)" }), "only in candidate"),
      h("span", null, h("span", { class: "sw", style: "background:var(--del)" }), "only in baseline"),
      h("span", null, h("span", { class: "sw", style: "background:var(--rep)" }), "different step"),
      h("span", null, h("span", { class: "marker" }, "◎"), " likely failure point"),
      h("span", null, "click a step for its input/output")),
    B && C && alignment ? diffTable(B, C, alignment, D, focus) : h("p", { class: "muted" }, "One side has no trace for this case."));

  state.caseTx = {};
  const explorers = h("div", { class: "panel", id: "full-traces" }, h("h2", null, "Full traces"),
    h("p", { class: "small muted" }, "Every recorded step, including routers and retries. Select a step for the checks that cite it, its input, output and attributes."),
    h("div", { class: "grid2" }, [["b", B, "a"], ["c", C, "b"]].map(([k, S, ab]) => {
      if (!S || !S.trace) return h("div", { class: "muted" }, "no trace");
      const tx = traceExplorer(S, { compact: true, select: k === "c" ? focus : null, focus: k === "c" ? lpId : null });
      state.caseTx[k] = tx;
      return h("div", null, h("div", { class: "side-h" }, h("span", { class: `ab ${ab}` }, ab.toUpperCase()), h("span", { class: "lbl" }, `${S.label} · trial ${S.trial}`),
        badge(S.trace.status === "ok" ? "ok" : "error", S.trace.status), h("span", { class: "muted small" }, fmtMs(S.trace.duration_ms))), tx);
    })));

  $app.replaceChildren(header, picker, diag, diffPanel, explorers);
  if (focus) highlight(focus, false, "c");
}

function trialPicker(label, S, key) {
  if (!S) return h("span", { class: "muted" }, `${label}: none`);
  return h("div", { class: "trials" }, h("span", { class: "small muted" }, label),
    S.trials.map((t) => h("button", {
      class: `trial ${t.outcome} ${t.trial === S.trial ? "sel" : ""}`, title: t.outcome,
      onclick: () => { const r = parseHash(); r.q[key] = t.trial; delete r.q.step; location.hash = link("case", r.arg, r.q); },
    }, `${t.trial} ${t.outcome === "pass" ? "✓" : t.outcome === "fail" ? "✗" : "?"}`)));
}

function citeBtn(id) {
  const base = id.startsWith("baseline:");
  const sid = base ? id.slice(9) : id;
  return h("button", { class: "cite", title: `highlight in ${base ? "baseline" : "candidate"}`, onclick: () => highlight(sid, true, base ? "b" : "c") }, (base ? "baseline " : "") + sid);
}

function diagnosisPanel(D, C, B) {
  const lp = D.likely_failure_point;
  return h("div", { class: "panel" },
    h("h2", null, `Why candidate trial ${D.trial} failed`, B ? h("span", { class: "muted small" }, ` (compared with baseline trial ${B.trial})`) : null),
    lp ? h("div", { class: "callout point" },
      h("div", { class: "small muted" }, "Likely failure point"),
      h("div", null, h("button", { class: "lnk big", onclick: () => highlight(lp.step.id, true, "c") }, lp.step.id), " ",
        badge(lp.confidence, lp.confidence === "evidence" ? "backed by check evidence" : "heuristic")),
      h("div", { class: "small" }, "Picked because it was ", lp.rule, "."),
      lp.step.error ? h("div", { class: "errline" }, lp.step.error, lp.error_hint ? `  (${lp.error_hint})` : "") : null,
      D.decision_step ? h("div", { class: "small", style: "margin-top:4px" }, "Decided by ",
        h("button", { class: "cite", onclick: () => highlight(D.decision_step.id, true, "c") }, D.decision_step.id),
        D.decision_step.requested_tools && D.decision_step.requested_tools.length ? `, which requested ${D.decision_step.requested_tools.join(", ")}` : "") : null)
      : h("div", { class: "callout note" }, "No single step explains this failure."),
    h("div", { class: "grid2" },
      h("div", null, h("h3", null, "What the traces show"), factsList(D.facts)),
      h("div", null, h("h3", null, "Suggested next steps"), D.next_steps.length ? h("ol", null, D.next_steps.map((s) => h("li", null, md(s)))) : h("p", { class: "muted" }, "—"),
        D.unverified_checks.length ? h("div", { class: "callout note small" }, "Unverified: ", D.unverified_checks.map((c) => [md(c.detail), " "])) : null)),
    h("p", { class: "small muted" }, "Facts cite recorded step ids. The failure point is where to start reading, not a proven root cause."));
}

function factsList(facts) {
  const li = (f) => h("li", null, md(f.text), f.cites.filter((c) => !/^(baseline:)?agent:/.test(c)).slice(0, 3).map(citeBtn));
  const checkFacts = facts.filter((f) => /^Check `/.test(f.text));
  const rest = facts.filter((f) => !/^Check `/.test(f.text));
  return h("div", null, h("ul", { class: "facts" }, rest.map(li)),
    checkFacts.length ? h("details", null, h("summary", null, `Failed check details (${checkFacts.length})`), h("ul", { class: "facts" }, checkFacts.map(li))) : null);
}

function stepExtra(s) {
  if (s.kind !== "llm") return "";
  const req = (s.attributes && s.attributes.requested_tools) || [];
  return req.length ? `→ ${req.join(", ")}` : "→ answer";
}
function stepCell(S, id, D, side) {
  const s = S.trace.steps.find((x) => x.id === id);
  if (!s) return h("td", { class: `${side} empty` });
  const total = S.trace.duration_ms || 1;
  const lp = side === "c" && D && D.likely_failure_point && D.likely_failure_point.step.id === id;
  const left = Math.max(0, (s.start_ms || 0) / total * 100), width = Math.max(0.5, ((s.end_ms ?? s.start_ms) - s.start_ms) / total * 100);
  return h("td", { class: side, "data-step": `${side}:${id}` },
    h("div", { class: `step ${s.status === "error" ? "error" : ""}` },
      kindIcon(s.kind), h("span", { class: "name", title: s.id }, s.name, s.status === "error" ? " ✗" : ""),
      h("span", { class: "extra" }, stepExtra(s)), lp ? h("span", { class: "marker", title: "likely failure point" }, "◎") : null,
      h("span", { class: "dur" }, fmtMs(dur(s)))),
    s.status === "error" && s.error ? h("div", { class: "errline" }, s.error) : null,
    h("div", { class: "bar" }, h("span", { style: `left:${left.toFixed(1)}%;width:${Math.min(100 - left, width).toFixed(1)}%` })));
}
function stepDetail(S, id) {
  if (!S || !id) return h("div", { class: "muted small" }, "—");
  const s = S.trace.steps.find((x) => x.id === id);
  if (!s) return h("div", { class: "muted small" }, "—");
  return h("div", { class: "kv" },
    h("div", { style: "margin-bottom:6px" }, h("code", null, s.id), " ", badge(s.status === "error" ? "error" : "ok", s.status),
      " ", h("button", { class: "lnk small", onclick: (e) => { e.stopPropagation(); openInExplorer(S === state.caseB ? "b" : null, s.id); } }, "open in full trace ↓")),
    s.error ? h("div", { class: "errline" }, s.error) : null,
    h("div", { class: "grid2" }, h("div", null, h("h3", null, "Input"), ioView(s.input)), h("div", null, h("h3", null, "Output"), ioView(s.output))));
}
function openInExplorer(side, id) {
  const tx = (side && state.caseTx[side]) || state.caseTx.c;
  if (!tx) return;
  tx.selectStep(id);
  tx.scrollIntoView({ block: "start", behavior: "smooth" });
}

function diffTable(B, C, alignment, D, focus) {
  state.caseB = B;
  const rows = alignment.rows;
  const keep = new Set();
  const important = (r) => r.tag !== "equal" || [r.baseline, r.candidate].includes(focus);
  rows.forEach((r, i) => { if (important(r)) for (let k = i - 2; k <= i + 2; k++) keep.add(k); });
  const table = h("table", { class: "diff" }, h("colgroup", null, h("col", { style: "width:28px" }), h("col"), h("col"), h("col", { style: "width:28px" })),
    h("tr", null, h("th", null, ""), h("th", null, h("span", { class: "ab a" }, "A"), ` Baseline · trial ${B.trial} · ${B.trace.status} · ${fmtMs(B.trace.duration_ms)}`),
      h("th", null, h("span", { class: "ab b" }, "B"), ` Candidate · trial ${C.trial} · ${C.trace.status} · ${fmtMs(C.trace.duration_ms)}`), h("th", null, "")));
  const SYM = { equal: "", insert: "+", delete: "−", replace: "~" };
  let i = 0;
  while (i < rows.length) {
    let j = i;
    while (j < rows.length && !keep.has(j)) j++;
    if (j - i >= 3) {
      const hidden = rows.slice(i, j);
      const tr = h("tr", { class: "collapsed" }, h("td", { colspan: 4 }, `… ${hidden.length} identical step(s), click to show`));
      tr.addEventListener("click", () => { const frag = hidden.map((r) => diffRow(r)); tr.replaceWith(...frag); });
      table.appendChild(tr);
      i = j;
      continue;
    }
    table.appendChild(diffRow(rows[i]));
    i++;
  }
  function diffRow(r) {
    const tr = h("tr", { class: r.tag }, h("td", { class: "gutter" }, SYM[r.tag]),
      r.baseline ? stepCell(B, r.baseline, D, "b") : h("td", { class: "b empty" }),
      r.candidate ? stepCell(C, r.candidate, D, "c") : h("td", { class: "c empty" }), h("td", { class: "gutter" }, SYM[r.tag]));
    tr.addEventListener("click", () => {
      const next = tr.nextSibling;
      if (next && next.classList && next.classList.contains("detailrow")) { next.remove(); return; }
      tr.after(h("tr", { class: "detailrow" }, h("td"), h("td", null, stepDetail(B, r.baseline)), h("td", null, stepDetail(C, r.candidate)), h("td")));
    });
    return tr;
  }
  if (alignment.identical) table.appendChild(h("tr", null, h("td", { colspan: 4, class: "muted small" }, "Both runs took the same steps; any difference is in the data. Click a step to compare inputs and outputs.")));
  return table;
}

function highlight(id, scroll = true, side = null) {
  document.querySelectorAll("tr.hl").forEach((x) => x.classList.remove("hl"));
  const sel = side ? `[data-step="${side}:${CSS.escape(id)}"]` : `[data-step="c:${CSS.escape(id)}"], [data-step="b:${CSS.escape(id)}"]`;
  const cell = document.querySelector(sel);
  const tx = state.caseTx[side || "c"];
  if (tx) tx.selectStep(id);
  if (!cell) {
    if (tx && scroll) tx.scrollIntoView({ block: "start", behavior: "smooth" });
    return;
  }
  const tr = cell.closest("tr");
  tr.classList.add("hl");
  if (scroll) tr.scrollIntoView({ block: "center", behavior: "smooth" });
}

// ---------- experiments (LangSmith-style comparison) ----------
const CHECK_LABEL = { completes: "completes", output: "output matches", tool_called: "tool called", tool_not_called: "tool not called", tool_order: "tool order",
  no_tool_errors: "no tool errors", max_latency_ms: "latency limit", max_tool_calls: "tool-call limit", max_llm_calls: "model-call limit" };

async function renderExperiments() {
  const E = await loadExperiment();
  const A = E.baseline, B = E.candidate, types = E.check_types;
  const F = state.expFilter;
  const head = h("div", { class: "page-head" },
    h("div", null, h("h1", null, "Comparing 2 experiments"), h("div", { class: "chips" }, runChip("a", A), runChip("b", B))),
    h("div", { class: "head-actions" }, h("a", { class: "btn", href: link("overview") }, "Regression analysis", icon("arrow", 14))));

  const charts = h("div", { class: "charts" },
    chartCard("Check scores", "pass rate by check type, averaged over cases", barChart(types.map((t) => CHECK_LABEL[t] || t),
      [{ name: A.label, color: COLOR_A, values: types.map((t) => A.scores[t]) }, { name: B.label, color: COLOR_B, values: types.map((t) => B.scores[t]) }],
      { max: 1, fmt: (v) => (+v).toFixed(1), rotate: types.length > 5 }), legend([[`A ${A.label}`, COLOR_A], [`B ${B.label}`, COLOR_B]])),
    chartCard("Latency", "per trace", barChart(["P50", "P99"], [{ name: A.label, color: COLOR_A, values: [A.p50_ms, A.p99_ms] }, { name: B.label, color: COLOR_B, values: [B.p50_ms, B.p99_ms] }],
      { fmt: (v) => fmtMs(v), width: 260, max: Math.max(A.p99_ms || 0, B.p99_ms || 0) * 1.15 || null }), legend([[`A`, COLOR_A], [`B`, COLOR_B]])),
    chartCard("Tokens", "average per trace", stackedChart(["A", "B"], [{ name: "input", color: "#93c5fd", values: [A.tokens_in, B.tokens_in] }, { name: "output", color: "#2563eb", values: [A.tokens_out, B.tokens_out] }],
      { fmt: (v) => Math.round(v) }), legend([["input", "#93c5fd"], ["output", "#2563eb"]])));

  const tableHost = h("div");
  const counts = { all: E.rows.length, changed: E.rows.filter((r) => r.change && r.change !== "unchanged").length, regressed: E.rows.filter((r) => ["regressed", "intermittent"].includes(r.change)).length };
  const seg = h("div", { class: "seg" });
  const drawSeg = () => seg.replaceChildren(...[["all", "All"], ["changed", "Changed"], ["regressed", "Regressed"]].map(([k, t]) =>
    h("button", { class: F.show === k ? "on" : "", onclick: () => { F.show = k; drawSeg(); drawTable(); } }, t, h("span", { class: "muted" }, counts[k]))));
  drawSeg();
  const search = h("input", { placeholder: "Search cases, inputs, outputs…", value: F.q, oninput: (e) => { F.q = e.target.value; drawTable(); } });
  const toolbar = h("div", { class: "toolbar" }, h("label", { class: "search" }, icon("search", 14), search), seg,
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: F.io || null, onchange: (e) => { F.io = e.target.checked; drawTable(); } }), "Inputs & reference"),
    h("span", { class: "grow" }), h("span", { class: "muted small" }, "B cells are red when the candidate scores lower than the baseline, green when higher. Click a row to compare traces."));

  function visibleRows() {
    const needle = F.q.trim().toLowerCase();
    return E.rows.map((r, i) => ({ r, i })).filter(({ r }) =>
      (F.show === "all" || (F.show === "changed" ? r.change && r.change !== "unchanged" : ["regressed", "intermittent"].includes(r.change))) &&
      (!needle || [r.case_id, r.description, JSON.stringify(r.input), JSON.stringify(r.runs.a && r.runs.a.output), JSON.stringify(r.runs.b && r.runs.b.output)].join(" ").toLowerCase().includes(needle)));
  }
  function drawTable() {
    const vis = visibleRows();
    const scoreHead = (t) => {
      const d = E.deltas[t];
      return [h("th", { class: "sub num" }, h("span", { class: "ab a" }, "A"), h("span", { class: "avg" }, num(A.scores[t], 2)), h("span", { class: "delta" }, "avg")),
        h("th", { class: "sub num" }, h("span", { class: "ab b" }, "B"), h("span", { class: "avg" }, num(B.scores[t], 2)),
          h("span", { class: "delta" }, h("span", { class: "up" }, `↑${d.improved}`), " ", h("span", { class: "down" }, `↓${d.regressed}`)))];
    };
    const table = h("table", { class: "exp" },
      h("thead", null,
        h("tr", null, h("th", { rowspan: 2 }, "#"), h("th", { rowspan: 2 }, "Case"), F.io ? [h("th", { rowspan: 2 }, "Input"), h("th", { rowspan: 2 }, "Reference")] : null,
          h("th", { rowspan: 2 }, h("span", { class: "ab a" }, "A"), " Output"), h("th", { rowspan: 2 }, h("span", { class: "ab b" }, "B"), " Output"),
          h("th", { class: "grp", colspan: 2 }, "trials passing"),
          types.map((t) => h("th", { class: "grp", colspan: 2, title: t }, CHECK_LABEL[t] || t)),
          h("th", { class: "grp", colspan: 2 }, "p50 latency"), h("th", { class: "grp", colspan: 2 }, "tokens")),
        h("tr", null, h("th", { class: "sub num" }, h("span", { class: "ab a" }, "A"), h("span", { class: "avg" }, `${A.passed}/${A.trials}`)),
          h("th", { class: "sub num" }, h("span", { class: "ab b" }, "B"), h("span", { class: "avg" }, `${B.passed}/${B.trials}`)),
          types.map(scoreHead),
          h("th", { class: "sub num" }, h("span", { class: "ab a" }, "A"), h("span", { class: "avg" }, fmtMs(A.p50_ms))), h("th", { class: "sub num" }, h("span", { class: "ab b" }, "B"), h("span", { class: "avg" }, fmtMs(B.p50_ms))),
          h("th", { class: "sub num" }, h("span", { class: "ab a" }, "A")), h("th", { class: "sub num" }, h("span", { class: "ab b" }, "B")))),
      h("tbody", null, vis.map(({ r, i }, k) => {
        const a = r.runs.a, b = r.runs.b;
        const outCell = (x) => !x ? h("td", { class: "muted small" }, "not in run") :
          h("td", null, h("div", { class: `cell-json ${x.output && x.output.error ? "err" : ""}`, title: oneLine(x.output, 2000) }, x.output && x.output.error ? `error: ${x.output.error}` : oneLine(x.output)));
        const pair = (va, vb, { fmt = (v) => num(v, 2), higherBetter = true, title = [] } = {}) => {
          const cls = va == null || vb == null || va === vb ? "" : (vb > va) === higherBetter ? "better" : "worse";
          return [h("td", { class: `score ${va == null ? "na" : ""}`, title: title[0] }, va == null ? "—" : fmt(va)),
            h("td", { class: `score ${vb == null ? "na" : cls}`, title: title[1] }, vb == null ? "—" : fmt(vb))];
        };
        const sc = (x, t) => x && x.scores[t];
        const tip = (s) => s ? `${s.passed}/${s.total} checks passed${s.unknown ? `, ${s.unknown} couldn't be verified` : ""}` : "no check of this type";
        return h("tr", { class: "clickable", "data-row": i, onclick: () => openCompareDrawer(E, vis, k) },
          h("td", { class: "idx" }, i + 1),
          h("td", null, h("div", null, h("code", null, r.case_id), " ", r.change && r.change !== "unchanged" ? badge(r.change, CHANGE_LABEL[r.change]) : null),
            r.description ? h("div", { class: "small muted cell-text" }, r.description) : null),
          F.io ? [h("td", null, h("div", { class: "cell-json", title: oneLine(r.input, 2000) }, oneLine(r.input))),
            h("td", { class: "small" }, r.reference.length ? r.reference.map((x) => h("div", { class: "cell-text" }, md(x.replace(/^output /, "")))) : h("span", { class: "muted" }, "—"))] : null,
          outCell(a), outCell(b),
          pair(a && a.trials.length ? a.passed / a.trials.length : null, b && b.trials.length ? b.passed / b.trials.length : null,
            { fmt: (v) => v.toFixed(2), title: [a ? `${a.passed}/${a.trials.length} trials` : "", b ? `${b.passed}/${b.trials.length} trials` : ""] }),
          types.map((t) => pair(sc(a, t) ? sc(a, t).score : null, sc(b, t) ? sc(b, t).score : null, { title: [tip(sc(a, t)), tip(sc(b, t))] })),
          pair(a && a.p50_ms, b && b.p50_ms, { fmt: fmtMs, higherBetter: false }),
          pair(a && a.tokens, b && b.tokens, { fmt: (v) => Math.round(v), higherBetter: false }));
      })));
    tableHost.replaceChildren(vis.length ? h("div", { class: "tablewrap" }, table) : h("div", { class: "panel muted" }, "No rows match."));
  }
  drawTable();
  $app.replaceChildren(head, charts, toolbar, tableHost);
}

async function openCompareDrawer(E, vis, k) {
  const { r, i } = vis[k];
  document.querySelectorAll("table.exp tr.sel").forEach((x) => x.classList.remove("sel"));
  const tr = document.querySelector(`table.exp tr[data-row="${i}"]`);
  if (tr) { tr.classList.add("sel"); tr.scrollIntoView({ block: "nearest" }); }
  const prev = k > 0 ? () => openCompareDrawer(E, vis, k - 1) : null;
  const next = k < vis.length - 1 ? () => openCompareDrawer(E, vis, k + 1) : null;
  const sides = [["a", E.baseline], ["b", E.candidate]];
  const cols = h("div", { class: "dw-cols" }, sides.map(([side, run]) => {
    const x = r.runs[side];
    const host = h("div", null, h("p", { class: "muted small" }, "Loading trace…"));
    const col = h("div", null);
    if (!x) { col.append(h("div", { class: "side-h" }, h("span", { class: `ab ${side}` }, side.toUpperCase()), h("span", { class: "lbl" }, run.label)), h("p", { class: "muted" }, "This case isn't in this run.")); return col; }
    let trial = side === "b" ? (x.trials.find((t) => t.outcome !== "pass") || x.trials[0]).trial : x.trials[0].trial;
    const tabs = h("div", { class: "trials" });
    const load = async () => {
      tabs.replaceChildren(h("span", { class: "small muted" }, "trial"), ...x.trials.map((t) => h("button", { class: `trial ${t.outcome} ${t.trial === trial ? "sel" : ""}`, title: `${t.outcome} · ${fmtMs(t.duration_ms)}`,
        onclick: () => { trial = t.trial; load(); } }, `${t.trial} ${t.outcome === "pass" ? "✓" : t.outcome === "fail" ? "✗" : "?"}`)));
      try {
        const T = await api(`/api/trace?${qs({ run: run.run_id, case: r.case_id, trial })}`);
        host.replaceChildren(traceExplorer(T, { compact: true }));
      } catch (e) { host.replaceChildren(h("p", { class: "err" }, String(e.message || e))); }
    };
    col.append(h("div", { class: "side-h" }, h("span", { class: `ab ${side}` }, side.toUpperCase()), h("span", { class: "lbl" }, run.label),
      h("span", { class: "muted small" }, `${x.passed}/${x.trials.length} trials pass`), h("span", { class: "grow" }), tabs), host);
    load();
    return col;
  }));
  const content = h("div", { style: "display:flex;flex-direction:column;height:100%" },
    drawerHead([h("span", { class: "muted" }, `Row ${i + 1}`), h("code", null, r.case_id), r.change ? badge(r.change, CHANGE_LABEL[r.change]) : null],
      r.description || null, { prev, next, actions: h("a", { class: "btn", href: link("case", r.case_id), onclick: closeDrawer }, "Diagnosis", icon("arrow", 14)) }),
    h("div", { class: "dw-body" }, cols));
  openDrawer(content, { wide: true, nav: { prev, next } });
}

// ---------- traces (Raindrop-style event table) ----------
async function renderTraces(q) {
  const runs = state.runs.runs;
  const p = runs.length >= 2 ? pairFromUrl() : {};
  const runId = q.run || p.candidate || runs[0].run_id;
  const data = await api(`/api/traces?${qs({ run: runId })}`);
  const T = data.traces, run = data.run;
  const F = state.traceFilter;
  const failing = T.filter((t) => t.outcome !== "pass").length;
  const errored = T.filter((t) => t.status === "error").length;
  const withGaps = T.filter((t) => t.gaps.verified + t.gaps.possible > 0).length;
  const durs = T.map((t) => t.duration_ms).filter((v) => v != null).sort((a, b) => a - b);
  const p50 = durs.length ? durs[Math.floor((durs.length - 1) / 2)] : null;
  const tokens = T.length ? T.reduce((a, t) => a + t.tokens_in + t.tokens_out, 0) / T.length : null;
  const maxDur = Math.max(1, ...durs);

  const head = h("div", { class: "page-head" },
    h("div", null, h("h1", null, "Traces"), h("div", { class: "sub" }, `Every recorded execution in a run. Select one to inspect its steps, inputs, outputs and the checks that cite them.`)),
    h("div", { class: "head-actions" }, h("select", { class: "sel", onchange: (e) => go("traces", null, { run: e.target.value }) }, runs.map((r) => runOption(r, r.run_id === runId)))));
  const stat = (k, v, bad) => h("div", { class: "stat" }, h("div", { class: "k" }, k), h("div", { class: `v ${bad ? "bad" : ""}` }, v));
  const stats = h("div", { class: "stats" }, stat("Traces", T.length), stat("Failing a check", failing, failing > 0), stat("Crashed", errored, errored > 0),
    stat("With instrumentation gaps", withGaps, withGaps > 0), stat("Latency p50", fmtMs(p50)), stat("Tokens / trace", tokens == null ? "—" : Math.round(tokens)));

  const cases = [...new Set(T.map((t) => t.case_id))];
  const host = h("div");
  const seg = h("div", { class: "seg" });
  const STATUS = [["all", "All", T.length], ["failing", "Failing", failing], ["error", "Crashed", errored], ["gaps", "Gaps", withGaps]];
  const drawSeg = () => seg.replaceChildren(...STATUS.map(([k, t, n]) => h("button", { class: F.status === k ? "on" : "", onclick: () => { F.status = k; drawSeg(); draw(); } }, t, h("span", { class: "muted" }, n))));
  drawSeg();
  if (F.kase && !cases.includes(F.kase)) F.kase = "";
  const toolbar = h("div", { class: "toolbar" },
    h("label", { class: "search" }, icon("search", 14), h("input", { placeholder: "Search inputs, outputs, errors…", value: F.q, oninput: (e) => { F.q = e.target.value; draw(); } })),
    seg, h("select", { class: "sel", onchange: (e) => { F.kase = e.target.value; draw(); } }, h("option", { value: "" }, "All cases"), cases.map((c) => { const o = h("option", { value: c }, c); if (c === F.kase) o.selected = true; return o; })),
    h("span", { class: "grow" }), runs.length >= 2 && inPair(runId) ? h("a", { class: "btn", href: link("experiments") }, icon("flask", 14), "Compare with the other run") : null);

  function visible() {
    const needle = F.q.trim().toLowerCase();
    return T.filter((t) => (F.status === "all" || (F.status === "failing" ? t.outcome !== "pass" : F.status === "error" ? t.status === "error" : t.gaps.verified + t.gaps.possible > 0)) &&
      (!F.kase || t.case_id === F.kase) &&
      (!needle || [t.case_id, t.error, JSON.stringify(t.input), JSON.stringify(t.output), t.failed.join(" ")].join(" ").toLowerCase().includes(needle)));
  }
  function draw() {
    const vis = visible();
    const table = h("table", null,
      h("thead", null, h("tr", null, h("th", null, ""), h("th", null, "Case"), h("th", null, "Input"), h("th", null, "Output"), h("th", null, "Started"),
        h("th", { class: "num" }, "Duration"), h("th", { class: "num" }, "Model calls"), h("th", { class: "num" }, "Tool calls"), h("th", { class: "num" }, "Tokens"), h("th", null, "Signals"))),
      h("tbody", null, vis.map((t, k) => h("tr", { class: "clickable", "data-trace": `${t.case_id}:${t.trial}`, onclick: () => openTraceDrawer(run, vis, k) },
        h("td", null, h("span", { class: `dot ${t.status === "error" ? "error" : t.outcome}`, title: t.status === "error" ? "crashed" : t.outcome })),
        h("td", null, h("code", null, t.case_id), h("span", { class: "muted small" }, ` #${t.trial}`)),
        h("td", null, h("div", { class: "cell-json", title: oneLine(t.input, 2000) }, oneLine(t.input, 90))),
        h("td", null, h("div", { class: `cell-json ${t.status === "error" ? "err" : ""}`, title: oneLine(t.status === "error" ? t.error : t.output, 2000) }, t.status === "error" ? `error: ${t.error}` : oneLine(t.output, 90))),
        h("td", { class: "small muted nowrap" }, when(t.started_at)),
        h("td", { class: "num" }, h("div", null, fmtMs(t.duration_ms)), h("div", { class: "bar" }, h("span", { style: `left:0;width:${((t.duration_ms || 0) / maxDur * 100).toFixed(1)}%` }))),
        h("td", { class: "num" }, t.llm_calls), h("td", { class: "num" }, t.tool_calls, t.tool_errors ? h("span", { class: "delta-bad" }, ` (${t.tool_errors} err)`) : null),
        h("td", { class: "num" }, t.tokens_in + t.tokens_out || "—"),
        h("td", null, h("div", { class: "trials" },
          t.outcome !== "pass" ? badge(t.outcome === "fail" ? "fail" : "unknown", t.outcome === "fail" ? `${t.failed.length} check${t.failed.length === 1 ? "" : "s"} failed` : t.outcome, t.failed.join(", ")) : badge("pass", "checks pass"),
          t.gaps.verified ? badge("verified", `${t.gaps.verified} verified gap`) : null, t.gaps.possible ? badge("possible", `${t.gaps.possible} possible gap`) : null))))));
    host.replaceChildren(vis.length ? h("div", { class: "tablewrap" }, table) : h("div", { class: "panel muted" }, "No traces match."));
  }
  draw();
  renderCrumbs("traces", null, h("span", { class: "muted" }, ` · ${run.label}`));
  $app.replaceChildren(head, stats, toolbar, host);
}
function inPair(runId) {
  const p = pairFromUrl();
  return p.baseline === runId || p.candidate === runId;
}

async function openTraceDrawer(run, vis, k) {
  const t = vis[k];
  document.querySelectorAll("tr.hl").forEach((x) => x.classList.remove("hl"));
  const tr = document.querySelector(`tr[data-trace="${CSS.escape(`${t.case_id}:${t.trial}`)}"]`);
  if (tr) { tr.classList.add("hl"); tr.scrollIntoView({ block: "nearest" }); }
  const prev = k > 0 ? () => openTraceDrawer(run, vis, k - 1) : null;
  const next = k < vis.length - 1 ? () => openTraceDrawer(run, vis, k + 1) : null;
  const p = state.runs.runs.length >= 2 ? pairFromUrl() : {};
  const inPair = p.baseline === run.run_id || p.candidate === run.run_id;
  const body = h("div", { class: "dw-body" }, h("p", { class: "muted" }, "Loading…"));
  openDrawer(h("div", { style: "display:flex;flex-direction:column;height:100%" },
    drawerHead([h("span", { class: `dot ${t.status === "error" ? "error" : t.outcome}` }), h("code", null, t.case_id), h("span", { class: "muted" }, `trial ${t.trial}`), h("span", { class: "muted small" }, `· ${run.label}`)],
      `${fmtMs(t.duration_ms)} · ${t.steps} steps · ${t.llm_calls} model calls · ${t.tool_calls} tool calls${t.tool_errors ? ` (${t.tool_errors} errored)` : ""} · ${t.tokens_in + t.tokens_out} tokens`,
      { prev, next, actions: inPair ? h("a", { class: "btn", href: link("case", t.case_id, p.candidate === run.run_id ? { ct: t.trial } : { bt: t.trial }), onclick: closeDrawer }, "Compare with the other run", icon("arrow", 14)) : null }),
    body), { wide: true, nav: { prev, next } });
  try {
    const T = await api(`/api/trace?${qs({ run: run.run_id, case: t.case_id, trial: t.trial })}`);
    body.replaceChildren(traceExplorer(T));
  } catch (e) { body.replaceChildren(h("p", { class: "err" }, String(e.message || e))); }
}

// ---------- datasets ----------
async function renderDatasets(name, q) {
  if (!name) {
    const { datasets } = await api("/api/datasets");
    $app.replaceChildren(h("div", { class: "page-head" }, h("div", null, h("h1", null, "Datasets"),
      h("div", { class: "sub" }, "A suite is a dataset: its cases are the examples, and every run of it is an experiment."))),
    h("div", { class: "tablewrap" }, h("table", null, h("thead", null, h("tr", null, h("th", null, "Name"), h("th", { class: "num" }, "Examples"), h("th", { class: "num" }, "Experiments"), h("th", null, "Latest experiment"), h("th", null, "Last run"))),
      h("tbody", null, datasets.map((d) => h("tr", { class: "clickable", onclick: () => go("datasets", d.name) },
        h("td", null, icon("db", 14), " ", h("b", null, d.name)), h("td", { class: "num" }, d.cases), h("td", { class: "num" }, d.runs), h("td", null, d.last_run), h("td", { class: "small muted" }, when(d.last_created))))))));
    return;
  }
  const D = await api(`/api/dataset?${qs({ name })}`);
  const tab = q.tab === "experiments" ? "experiments" : "examples";
  const tabs = h("div", { class: "tabs", style: "border-bottom:1px solid var(--line);margin-bottom:14px" },
    [["examples", "Examples", D.examples.length], ["experiments", "Experiments", D.experiments.length]].map(([k, t, n]) =>
      h("button", { class: tab === k ? "on" : "", onclick: () => go("datasets", name, { tab: k }) }, t, h("span", { class: "n" }, n))));
  const head = h("div", { class: "page-head" }, h("div", null, h("h1", null, D.name), h("div", { class: "sub" }, `${D.examples.length} examples · ${D.experiments.length} experiments`)));
  let body;
  if (tab === "examples") {
    body = h("div", { class: "tablewrap" }, h("table", null,
      h("thead", null, h("tr", null, h("th", null, "#"), h("th", null, "Example"), h("th", null, "Input"), h("th", null, "Reference"), h("th", { class: "num" }, "Checks"), h("th", null, "Recent results (newest first)"))),
      h("tbody", null, D.examples.map((ex, k) => h("tr", { class: "clickable", "data-ex": ex.case_id, onclick: () => openExampleDrawer(D, k) },
        h("td", { class: "muted" }, k + 1),
        h("td", null, h("code", null, ex.case_id), ex.description ? h("div", { class: "small muted cell-text" }, ex.description) : null),
        h("td", null, h("div", { class: "cell-json", title: oneLine(ex.input, 2000) }, oneLine(ex.input, 100))),
        h("td", { class: "small" }, ex.checks.filter((c) => c.type === "output").map((c) => h("div", { class: "cell-text" }, md(c.description.replace(/^output /, ""))))),
        h("td", { class: "num" }, ex.checks.length),
        h("td", null, h("div", { class: "trials" }, ex.history.slice(0, 6).map((r) => h("span", { class: `badge ${r.passed === r.trials ? "pass" : r.passed === 0 ? "fail" : "unknown"}`, title: `${r.label} (${r.run_id})` }, `${r.label} ${r.passed}/${r.trials}`)))))))));
  } else {
    const chosen = new Set();
    const btn = h("button", { class: "btn primary", disabled: true, onclick: () => {
      const [x, y] = D.experiments.filter((e) => chosen.has(e.run_id)).sort((a, b) => (a.created_at || "").localeCompare(b.created_at || ""));
      go("experiments", null, { b: x.run_id, c: y.run_id });
    } }, icon("flask", 14), "Compare selected");
    const hint = h("span", { class: "muted small" }, "Select two experiments to compare them (the older one becomes the baseline).");
    const refresh = () => { btn.disabled = chosen.size !== 2; };
    body = h("div", null, h("div", { class: "toolbar" }, btn, hint),
      h("div", { class: "tablewrap" }, h("table", null,
        h("thead", null, h("tr", null, h("th", null, ""), h("th", null, "Experiment"), h("th", null, "Run id"), h("th", null, "Created"), h("th", { class: "num" }, "Trials passing"), h("th", { class: "num" }, "p50 latency"), h("th", null, "Target"), h("th", null, "Commit"), h("th", null, ""))),
        h("tbody", null, D.experiments.map((e) => h("tr", null,
          h("td", null, h("input", { type: "checkbox", onchange: (ev) => { ev.target.checked ? chosen.add(e.run_id) : chosen.delete(e.run_id); refresh(); } })),
          h("td", null, h("b", null, e.label)), h("td", { class: "mono small" }, e.run_id), h("td", { class: "small muted" }, when(e.created_at)),
          h("td", { class: "num" }, `${e.passed}/${e.trials}`, h("div", { class: "bar" }, h("span", { style: `left:0;width:${e.trials ? (e.passed / e.trials * 100).toFixed(0) : 0}%` }))),
          h("td", { class: "num" }, fmtMs(e.p50_ms)), h("td", { class: "small" }, h("code", null, e.target || "—")),
          h("td", { class: "mono small" }, e.git && e.git.commit ? e.git.commit + (e.git.dirty ? "+dirty" : "") : "—"),
          h("td", null, h("a", { href: link("traces", null, { run: e.run_id }) }, "traces →"))))))));
  }
  $app.replaceChildren(head, tabs, body);
}

function openExampleDrawer(D, k) {
  const ex = D.examples[k];
  document.querySelectorAll("tr.hl").forEach((x) => x.classList.remove("hl"));
  const tr = document.querySelector(`tr[data-ex="${CSS.escape(ex.case_id)}"]`);
  if (tr) tr.classList.add("hl");
  const prev = k > 0 ? () => openExampleDrawer(D, k - 1) : null;
  const next = k < D.examples.length - 1 ? () => openExampleDrawer(D, k + 1) : null;
  openDrawer(h("div", { style: "display:flex;flex-direction:column;height:100%" },
    drawerHead([h("code", null, ex.case_id)], ex.description || null, { prev, next }),
    h("div", { class: "dw-body" },
      h("div", { class: "sect" }, h("div", { class: "sect-h" }, "Input"), h("div", { class: "sect-b" }, ioView(ex.input))),
      h("div", { class: "sect" }, h("div", { class: "sect-h" }, "Checks (what a correct run must do)"), h("div", { class: "sect-b" },
        ex.checks.map((c) => h("div", { class: "fb wide" }, h("div", null, badge("kind", c.type)), h("div", null, md(c.description)))))),
      h("div", { class: "sect" }, h("div", { class: "sect-h" }, "Results across experiments"), h("div", { class: "sect-b" },
        h("table", null, h("tr", null, h("th", null, "experiment"), h("th", null, "created"), h("th", { class: "num" }, "trials passing"), h("th", null, "")),
          ex.history.map((r) => h("tr", null, h("td", null, h("b", null, r.label), h("div", { class: "mono small muted" }, r.run_id)), h("td", { class: "small muted" }, when(r.created_at)),
            h("td", { class: "num" }, badge(r.passed === r.trials ? "pass" : r.passed === 0 ? "fail" : "unknown", `${r.passed}/${r.trials}`)),
            h("td", null, h("a", { href: link("traces", null, { run: r.run_id }), onclick: () => { state.traceFilter.kase = ex.case_id; closeDrawer(); } }, "traces →")))))))),
  ), { nav: { prev, next } });
}

// ---------- coverage & drift ----------
const GAP_HELP = {
  verified: "Verified: the trace itself proves something is missing (e.g. the model asked for a tool and no tool step was recorded).",
  possible: "Possible: a pattern that often means missing instrumentation but can be legitimate (e.g. a node's time isn't covered by child steps).",
  info: "Info: capture quality hints (missing token usage, missing tool input/output).",
};
const GAP_TITLES = {
  unrecorded_tool_call: "Model-requested tool call with no recorded tool step", orphan_step: "Step whose parent wasn't recorded",
  unclosed_step: "Step that never finished", error_without_message: "Error without a message", empty_trace: "Trace with no steps",
  untraced_time: "Node time not covered by any recorded step", declared_node_not_observed: "Declared graph node never observed",
  check_references_unseen_tool: "Check references a tool that never appeared", expected_tool_missing: "Expected tool never recorded",
  expected_node_missing: "Expected node never recorded", llm_usage_missing: "Model call without token usage", tool_io_missing: "Tool call without input/output",
};

async function renderObservability() {
  if (state.runs.runs.length < 2) {
    const r = state.runs.runs[0];
    const cov = await api(`/api/coverage?run=${encodeURIComponent(r.run_id)}`);
    $app.replaceChildren(gapsPanel(null, cov, r.label, r.run_id));
    return;
  }
  const cmp = await loadCompare();
  const ob = cmp.observability;
  const bc = cmp.coverage.baseline, cc = cmp.coverage.candidate;
  const head = h("div", { class: "page-head" }, h("div", null, h("h1", null, "Coverage & drift"),
    h("div", { class: "sub" }, "Is the candidate still observable, and did its structure change?")),
  h("div", { class: "head-actions" }, h("a", { class: "btn", href: link("traces", null, { run: cmp.candidate.run_id }) }, icon("list", 14), "Browse candidate traces")));
  const banner = h("div", { class: `panel callout ${ob.status === "ok" ? "okay" : ob.status === "gaps" ? "point" : "note"}` },
    h("h2", null, "Is the candidate still observable? ", badge(ob.status, OBS_LABEL[ob.status])), h("div", null, md(ob.summary)));
  const struct = (s) => [["nodes", s.nodes], ["tools", s.tools], ["models", s.models]].map(([k, v]) =>
    h("div", { class: "small" }, h("b", null, `${k}: `), v.length ? v.map((x, i) => [i ? ", " : "", h("code", null, x)]) : "—"));
  const capture = h("div", { class: "panel" }, h("h2", null, "What was captured"),
    h("div", { class: "grid2" }, [["A", "Baseline", bc, "a"], ["B", "Candidate", cc, "b"]].map(([ab, t, c, cls]) => h("div", null, h("h3", null, h("span", { class: `ab ${cls}` }, ab), ` ${t} · ${c.structure.traces} trace(s)`), struct(c.structure),
      h("div", { class: "small" }, `model calls with token usage: ${pct(c.structure.capture.llm_usage)} · tool calls with input/output: ${pct(c.structure.capture.tool_io)}`)))));
  const drift = h("div", { class: "panel" }, h("h2", null, "Structural changes (drift)"),
    cmp.coverage.drift.length ? h("ul", null, cmp.coverage.drift.map((d) => h("li", null, badge(d.severity), " ", md(d.message)))) : h("p", { class: "muted" }, "No structural changes between the two runs."));
  $app.replaceChildren(head, banner, gapsPanel(bc, cc, "candidate", cmp.candidate.run_id), drift, capture, limitsPanel());
}

function gapsPanel(bc, cc, label, runId) {
  const bItems = Object.fromEntries(((bc && bc.summary.items) || []).map((i) => [i.code, i]));
  const items = [...cc.summary.items];
  for (const code of Object.keys(bItems)) if (!items.find((i) => i.code === code)) items.push({ ...bItems[code], count: 0, cases: [], examples: [], gone: true });
  return h("div", { class: "panel" }, h("h2", null, `Instrumentation gaps (${label})`),
    h("div", { class: "legend" }, Object.entries(GAP_HELP).map(([k, v]) => h("span", { title: v }, badge(k), " ", k === "verified" ? "proven by the trace" : k === "possible" ? "worth checking" : "capture quality"))),
    items.length ? h("table", null, h("tr", null, h("th", null, "level"), h("th", null, "gap"), bc ? h("th", { class: "num" }, "baseline") : null, h("th", { class: "num" }, label), h("th", null, "example")),
      items.map((i) => {
        const ex = (i.examples || [])[0];
        return h("tr", null, h("td", null, badge(i.level, i.level, GAP_HELP[i.level])),
          h("td", null, h("div", null, GAP_TITLES[i.code] || i.code), h("div", { class: "small muted" }, (i.cases || []).length ? `cases: ${i.cases.join(", ")}` : i.gone ? "resolved in candidate" : "")),
          bc ? h("td", { class: "num" }, bItems[i.code] ? bItems[i.code].count : 0) : null,
          h("td", { class: "num" }, i.count),
          h("td", { class: "small" }, ex ? [md(ex.message), " ", ex.case_id != null ? h("button", { class: "lnk", onclick: () => openGapTrace(runId, ex) }, "open trace →") : null] : null));
      })) : h("p", { class: "muted" }, "No gaps detected in what was recorded."));
}
async function openGapTrace(runId, ex) {
  const run = runById(runId) || { run_id: runId, label: runId };
  const body = h("div", { class: "dw-body" }, h("p", { class: "muted" }, "Loading…"));
  openDrawer(h("div", { style: "display:flex;flex-direction:column;height:100%" },
    drawerHead([h("code", null, ex.case_id), h("span", { class: "muted" }, `trial ${ex.trial}`), h("span", { class: "muted small" }, `· ${run.label}`)], md(ex.message)), body), { wide: true });
  try {
    const T = await api(`/api/trace?${qs({ run: runId, case: ex.case_id, trial: ex.trial })}`);
    body.replaceChildren(traceExplorer(T, { select: ex.step || null }));
  } catch (e) { body.replaceChildren(h("p", { class: "err" }, String(e.message || e))); }
}

function limitsPanel() {
  return h("div", { class: "panel small muted" }, h("h2", null, "What this can and can't tell you"),
    h("ul", null,
      h("li", null, "Verified gaps are contradictions inside a trace (a requested tool with no tool step, a step whose parent is missing, a step that never ended)."),
      h("li", null, "Possible gaps are hints: a node whose time isn't covered by children may call an API directly, or may just be computing."),
      h("li", null, "Work that never touches the instrumentation (no callback, no span, no model request) is invisible to any trace-based check, including this one."),
      h("li", null, "Declare what must be observed in ", h("code", null, "[coverage] expected_tools / expected_nodes"), " to turn silent loss into a verified gap.")));
}

// ---------- runs ----------
function renderRuns() {
  const runs = state.runs.runs;
  const p = runs.length >= 2 ? pairFromUrl() : {};
  const setPair = (which, id) => { const r = parseHash(); Object.assign(r.q, choosePair(p, which, id)); location.hash = link("overview", null, r.q); };
  $app.replaceChildren(h("div", { class: "page-head" }, h("div", null, h("h1", null, "Runs"), h("div", { class: "sub" }, `Stored in ${state.runs.home}`))),
    runs.length ? h("div", { class: "tablewrap" }, h("table", null, h("thead", null, h("tr", null, h("th", null, "label"), h("th", null, "run id"), h("th", null, "suite"), h("th", { class: "num" }, "passed"), h("th", null, "target / source"), h("th", null, "commit"), h("th", null, ""))),
      h("tbody", null, runs.map((r) => h("tr", null, h("td", null, h("b", null, r.label)), h("td", { class: "mono small" }, r.run_id), h("td", { class: "small" }, r.suite || "—"),
        h("td", { class: "num" }, `${r.passed}/${r.trials}`), h("td", { class: "small" }, h("code", null, r.target || r.source || "—")),
        h("td", { class: "small mono" }, r.git && r.git.commit ? r.git.commit + (r.git.dirty ? "+dirty" : "") : "—"),
        h("td", null, h("div", { class: "trials" },
          h("a", { class: "btn", href: link("traces", null, { run: r.run_id }) }, "traces"),
          runs.length >= 2 ? [
            h("button", { class: "btn", disabled: p.baseline === r.run_id || null, onclick: () => setPair("b", r.run_id) }, p.baseline === r.run_id ? "A baseline ✓" : "use as A (baseline)"),
            h("button", { class: "btn", disabled: p.candidate === r.run_id || null, onclick: () => setPair("c", r.run_id) }, p.candidate === r.run_id ? "B candidate ✓" : "use as B (candidate)")] : null)))))))
      : h("p", { class: "muted" }, "No runs yet. Try ", h("code", null, "agentpatch demo"), "."));
}

render();
