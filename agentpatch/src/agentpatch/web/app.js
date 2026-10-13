"use strict";
// AgentPatch dashboard. Trace data is always inserted as text (never as HTML).

const $app = document.getElementById("app");
const state = { runs: null, cmp: null, cmpKey: null };

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
function pretty(v) {
  if (v == null) return "null";
  if (typeof v === "string") return v;
  try { return JSON.stringify(v, null, 2); } catch { return String(v); }
}

async function api(path) {
  const r = await fetch(path);
  const body = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}

// ---------- routing ----------
function parseHash() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, qs] = raw.split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  return { view: parts[0] || "overview", arg: parts[1], q: Object.fromEntries(new URLSearchParams(qs || "")) };
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
window.addEventListener("hashchange", render);

function pairFromUrl() {
  const { q } = parseHash();
  const d = state.runs.default || {};
  return { baseline: q.b || d.baseline, candidate: q.c || d.candidate };
}

async function loadCompare() {
  const p = pairFromUrl();
  if (!p.baseline || !p.candidate) return null;
  const key = `${p.baseline}|${p.candidate}`;
  if (state.cmpKey !== key) {
    state.cmp = await api(`/api/compare?baseline=${encodeURIComponent(p.baseline)}&candidate=${encodeURIComponent(p.candidate)}`);
    state.cmpKey = key;
  }
  return state.cmp;
}

// ---------- chrome ----------
function renderNav(view) {
  const nav = document.getElementById("nav");
  nav.replaceChildren(...[["overview", "Overview"], ["observability", "Observability"], ["runs", "Runs"]].map(([v, t]) =>
    h("a", { href: link(v), class: view === v || (v === "overview" && view === "case") ? "active" : "" }, t)));
}
function renderPair() {
  const el = document.getElementById("pair");
  const runs = state.runs.runs;
  if (runs.length < 2) { el.replaceChildren(); return; }
  const p = pairFromUrl();
  const sel = (which) => h("select", {
    title: which, onchange: (e) => {
      const r = parseHash();
      Object.assign(r.q, choosePair(p, which === "baseline" ? "b" : "c", e.target.value));
      location.hash = link(r.view, r.arg, r.q);
    },
  }, runs.map((r) => {
    const o = h("option", { value: r.run_id }, `${r.label} · ${r.run_id.slice(0, 15)} · ${r.passed}/${r.trials} passed`);
    if (r.run_id === p[which]) o.selected = true;
    return o;
  }));
  el.replaceChildren("baseline", sel("baseline"), h("button", {
    title: "swap", onclick: () => { const r = parseHash(); r.q.b = p.candidate; r.q.c = p.baseline; location.hash = link(r.view, r.arg, r.q); },
  }, "⇄"), "candidate", sel("candidate"));
}

// Picking the run that's currently on the other side swaps the two instead of comparing a run with itself.
function choosePair(p, side, id) {
  const other = side === "b" ? p.candidate : p.baseline;
  const mine = side === "b" ? p.baseline : p.candidate;
  const q = { [side]: id, [side === "b" ? "c" : "b"]: other };
  if (id === other) q[side === "b" ? "c" : "b"] = mine;
  return q;
}

async function render() {
  const { view, arg, q } = parseHash();
  try {
    state.runs = await api("/api/runs");
    renderNav(view);
    renderPair();
    if (view === "runs") return renderRuns();
    if (!state.runs.runs.length) return renderEmpty();
    if (state.runs.runs.length < 2 && view !== "observability") return renderSingle();
    if (view === "case") return await renderCase(arg, q);
    if (view === "observability") return await renderObservability();
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
        h("td", { class: "small" }, [...new Set(c.trials.flatMap((t) => t.failed))].map((x) => h("code", null, x)), " ")))));
}

// ---------- overview ----------
const CHANGE_LABEL = { regressed: "regressed", intermittent: "intermittent", fixed: "fixed", still_failing: "still failing", unchanged: "unchanged", added: "new case", removed: "removed" };
const OBS_LABEL = { ok: "Observable", check: "Check coverage", gaps: "Gaps found" };

async function renderOverview() {
  const cmp = await loadCompare();
  if (!cmp) return renderSingle();
  const broke = cmp.cases.filter((r) => ["regressed", "intermittent"].includes(r.change));
  const caseLink = (r, extra = {}) => link("case", r.case_id, extra);

  const verdict = h("div", { class: `panel verdict ${cmp.verdict}` },
    h("span", { class: "label" }, cmp.verdict.replace("_", " ").toUpperCase()),
    h("div", null, h("div", { class: "headline" }, cmp.headline),
      h("div", { class: "muted small" }, runMeta(cmp.baseline), "  →  ", runMeta(cmp.candidate))));

  const q1 = h("div", { class: "panel q" }, h("h2", null, h("span", { class: "num" }, "1"), "What broke?"),
    broke.length ? h("ul", null, sortCases(broke).map((r) => h("li", null, h("a", { href: caseLink(r) }, h("code", null, r.case_id)), " ",
      badge(r.change, CHANGE_LABEL[r.change]), " ", h("span", { class: "muted" }, `${rate(r.baseline)} → ${rate(r.candidate)} trials pass`))))
      : h("p", { class: "muted" }, "No case got worse on its checks."),
    otherChanges(cmp));

  const q2 = h("div", { class: "panel q" }, h("h2", null, h("span", { class: "num" }, "2"), "Why? (where to look)"),
    broke.length ? h("ul", null, sortCases(broke).map((r) => {
      const lp = r.diagnosis && r.diagnosis.likely_failure_point;
      if (!lp) return h("li", null, h("code", null, r.case_id), ": no single step explains it; ", h("a", { href: caseLink(r) }, "compare outputs"));
      return h("li", null, h("code", null, r.case_id), " → ",
        h("a", { href: caseLink(r, { ct: r.diagnosis.trial, bt: r.diagnosis.baseline_trial, step: lp.step.id }) }, h("code", null, lp.step.id)), " ",
        badge(lp.confidence, lp.confidence === "evidence" ? "check evidence" : "heuristic", lp.rule),
        lp.error_hint ? h("div", { class: "small muted" }, lp.error_hint) : null);
    })) : h("p", { class: "muted" }, "Nothing to diagnose."),
    h("div", { class: "foot small muted" }, "Likely failure points are where to start reading, chosen by fixed rules, not proven root causes."));

  const ob = cmp.observability;
  const q3 = h("div", { class: "panel q" }, h("h2", null, h("span", { class: "num" }, "3"), "Still observable?"),
    h("div", null, badge(ob.status, OBS_LABEL[ob.status])),
    h("p", { class: "small" }, md(ob.summary)),
    h("div", { class: "foot small" }, h("a", { href: link("observability") }, "Observability details →")));

  const all = cmp.findings;
  const main = all.filter((f) => f.severity !== "info");
  const info = all.filter((f) => f.severity === "info");
  const findings = h("div", { class: "panel" }, h("h2", null, "What changed, most important first"),
    main.length ? main.map(findingEl) : h("p", { class: "muted" }, "No critical or warning findings."),
    info.length ? h("details", { open: main.length === 0 ? true : null }, h("summary", null, `${info.length} informational finding(s): fixes and neutral changes`), info.map(findingEl)) : null,
    cmp.notes.map((n) => h("div", { class: "callout note small" }, n)));

  const cases = h("div", { class: "panel" }, h("h2", null, "Cases"),
    h("table", null, h("tr", null, h("th", null, "case"), h("th", null, "change"), h("th", { class: "num" }, "baseline"), h("th", { class: "num" }, "candidate"),
      h("th", { class: "num" }, "p50 latency"), h("th", null, "checks that changed")),
    sortCases(cmp.cases).map((r) => h("tr", { class: "clickable", onclick: () => go("case", r.case_id) },
      h("td", null, h("code", null, r.case_id)), h("td", null, badge(r.change, CHANGE_LABEL[r.change])),
      h("td", { class: "num" }, rate(r.baseline)), h("td", { class: "num" }, rate(r.candidate)),
      h("td", { class: "num" }, latencyCell(r.latency)),
      h("td", { class: "small" }, r.checks.filter((c) => ["regressed", "fixed"].includes(c.change)).map((c) =>
        h("div", null, badge(c.change), " ", md(c.description))))))));

  $app.replaceChildren(verdict, h("div", { class: "grid3" }, q1, q2, q3), findings, cases, metricsPanel(cmp));
}

function otherChanges(cmp) {
  const fixed = cmp.cases.filter((r) => r.change === "fixed");
  return fixed.length ? h("p", { class: "small muted" }, "Fixed: ", fixed.map((r, i) => [i ? ", " : "", h("a", { href: link("case", r.case_id) }, h("code", null, r.case_id))])) : null;
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
function runMeta(m) {
  const git = m.git && m.git.commit ? ` @ ${m.git.commit}${m.git.dirty ? "+dirty" : ""}` : "";
  return `${m.label} (${m.run_id}${git}, ${m.repeats || 1}x per case)`;
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
  const qs = new URLSearchParams({ baseline: p.baseline, candidate: p.candidate, case: caseId });
  if (q.bt != null) qs.set("btrial", q.bt);
  if (q.ct != null) qs.set("ctrial", q.ct);
  const data = await api(`/api/case?${qs}`);
  const { case: row, baseline: B, candidate: C, alignment, diagnosis: D } = data;
  const focus = q.step || (D && D.likely_failure_point && D.likely_failure_point.step.id) || null;

  const header = h("div", { class: "panel" },
    h("div", { class: "small" }, h("a", { href: link("overview") }, "← Overview")),
    h("h1", null, h("code", null, row.case_id), " ", badge(row.change, CHANGE_LABEL[row.change])),
    h("div", { class: "muted" }, `baseline ${rate(row.baseline)} trials pass → candidate ${rate(row.candidate)}`,
      row.latency ? ` · p50 ${fmtMs(row.latency.baseline_p50)} → ${fmtMs(row.latency.candidate_p50)}` : ""),
    h("h3", null, "Checks"),
    h("table", null, h("tr", null, h("th", null, "check"), h("th", { class: "num" }, "baseline"), h("th", { class: "num" }, "candidate"), h("th", null, "")),
      row.checks.map((c) => h("tr", null, h("td", null, md(c.description)),
        h("td", { class: "num" }, c.baseline ? `${c.baseline.passed}/${row.baseline.trials}` : "—"),
        h("td", { class: "num" }, c.candidate ? `${c.candidate.passed}/${row.candidate.trials}` : "—"),
        h("td", null, ["regressed", "fixed", "failing"].includes(c.change) ? badge(c.change) : null)))));

  const picker = h("div", { class: "panel toolbar" },
    trialPicker("Baseline trial", B, "bt", q), trialPicker("Candidate trial", C, "ct", q),
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

  const full = h("div", { class: "panel" }, h("details", null, h("summary", null, "Full traces (every recorded step, including routers and retries) and final outputs"),
    h("div", { class: "grid2" }, [B, C].map((S) => S && S.trace ? h("div", null,
      h("h3", null, `${S.label} trial ${S.trial} · ${S.trace.status} · ${fmtMs(S.trace.duration_ms)}`),
      treeEl(S.trace, focus),
      h("h3", null, "Input"), h("pre", null, pretty(S.trace.input)),
      h("h3", null, "Output"), h("pre", null, S.trace.status === "error" ? `error: ${S.trace.error}` : pretty(S.trace.output)),
      S.gaps && S.gaps.length ? [h("h3", null, "Instrumentation gaps in this trace"), S.gaps.map((g) => h("div", { class: "small" }, badge(g.level), " ", md(g.message)))] : null,
    ) : h("div", { class: "muted" }, "no trace")))));

  $app.replaceChildren(header, picker, diag, diffPanel, full);
  if (focus) highlight(focus, false);
}

function trialPicker(label, S, key, q) {
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
      h("div", null, h("h3", null, "What the traces show"), h("ul", { class: "facts" }, D.facts.map((f) => h("li", null, md(f.text), f.cites.filter((c) => !/^(baseline:)?agent:/.test(c)).slice(0, 3).map(citeBtn))))),
      h("div", null, h("h3", null, "Suggested next steps"), D.next_steps.length ? h("ol", null, D.next_steps.map((s) => h("li", null, md(s)))) : h("p", { class: "muted" }, "—"),
        D.unverified_checks.length ? h("div", { class: "callout note small" }, "Unverified: ", D.unverified_checks.map((c) => [md(c.detail), " "])) : null)),
    h("p", { class: "small muted" }, "Facts cite recorded step ids. The failure point is where to start reading, not a proven root cause."));
}

function stepExtra(s) {
  const bits = [];
  if (s.kind === "llm") {
    const req = (s.attributes && s.attributes.requested_tools) || [];
    bits.push(req.length ? `→ ${req.join(", ")}` : "→ answer");
  }
  return bits.join(" ");
}
function stepCell(S, id, D, side) {
  const s = S.trace.steps.find((x) => x.id === id);
  if (!s) return h("td", { class: `${side} empty` });
  const total = S.trace.duration_ms || 1;
  const lp = side === "c" && D && D.likely_failure_point && D.likely_failure_point.step.id === id;
  const left = Math.max(0, (s.start_ms || 0) / total * 100), width = Math.max(0.5, ((s.end_ms ?? s.start_ms) - s.start_ms) / total * 100);
  return h("td", { class: side, "data-step": `${side}:${id}` },
    h("div", { class: `step ${s.status === "error" ? "error" : ""}` },
      badge("kind", s.kind), h("span", { class: "name", title: s.id }, s.name, s.status === "error" ? " ✗" : ""),
      h("span", { class: "extra" }, stepExtra(s)), lp ? h("span", { class: "marker", title: "likely failure point" }, "◎") : null,
      h("span", { class: "dur" }, fmtMs(dur(s)))),
    s.status === "error" && s.error ? h("div", { class: "errline" }, s.error) : null,
    h("div", { class: "bar" }, h("span", { style: `left:${left.toFixed(1)}%;width:${Math.min(100 - left, width).toFixed(1)}%` })));
}
function stepDetail(S, id) {
  if (!S || !id) return h("div", { class: "muted small" }, "—");
  const s = S.trace.steps.find((x) => x.id === id);
  if (!s) return h("div", { class: "muted small" }, "—");
  const attrs = Object.entries(s.attributes || {}).filter(([k]) => k !== "requested_tools");
  return h("div", { class: "kv" },
    h("div", null, h("b", null, "id "), h("code", null, s.id), h("b", null, "  parent "), h("code", null, s.parent_id || "—"), h("b", null, "  status "), s.status),
    s.error ? h("div", { class: "errline" }, s.error) : null,
    attrs.length ? h("div", null, h("b", null, "attributes "), h("code", null, attrs.map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join("  "))) : null,
    h("b", null, "input"), h("pre", null, pretty(s.input)), h("b", null, "output"), h("pre", null, pretty(s.output)));
}

function diffTable(B, C, alignment, D, focus) {
  const rows = alignment.rows;
  const keep = new Set();
  const important = (r) => r.tag !== "equal" || [r.baseline, r.candidate].includes(focus);
  rows.forEach((r, i) => { if (important(r)) for (let k = i - 2; k <= i + 2; k++) keep.add(k); });
  const table = h("table", { class: "diff" }, h("colgroup", null, h("col", { style: "width:28px" }), h("col"), h("col"), h("col", { style: "width:28px" })),
    h("tr", null, h("th", null, ""), h("th", null, `Baseline · trial ${B.trial} · ${B.trace.status} · ${fmtMs(B.trace.duration_ms)}`),
      h("th", null, `Candidate · trial ${C.trial} · ${C.trace.status} · ${fmtMs(C.trace.duration_ms)}`), h("th", null, "")));
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

function treeEl(trace, focus) {
  const depth = {};
  return h("div", { class: "tree" }, trace.steps.map((s) => {
    const d = (depth[s.parent_id] ?? -1) + 1;
    depth[s.id] = d;
    const det = h("details", { class: `node ${s.id === focus ? "hl" : ""}`, style: `margin-left:${d * 14}px`, "data-tree": s.id },
      h("summary", { style: "color:inherit" }, badge("kind", s.kind), " ", h("span", { class: `mono ${s.status === "error" ? "err" : ""}` }, s.id), " ",
        h("span", { class: "muted small" }, stepExtra(s), " ", fmtMs(dur(s)))),
      stepDetail({ trace }, s.id));
    return det;
  }));
}

function highlight(id, scroll = true, side = null) {
  document.querySelectorAll("tr.hl").forEach((x) => x.classList.remove("hl"));
  const sel = side ? `[data-step="${side}:${CSS.escape(id)}"]` : `[data-step="c:${CSS.escape(id)}"], [data-step="b:${CSS.escape(id)}"]`;
  let cell = document.querySelector(sel);
  if (!cell) {
    const tree = document.querySelector(`[data-tree="${CSS.escape(id)}"]`);
    if (tree) { tree.closest("details").open = true; tree.closest(".panel").querySelector("details").open = true; tree.open = true; if (scroll) tree.scrollIntoView({ block: "center", behavior: "smooth" }); }
    return;
  }
  const tr = cell.closest("tr");
  tr.classList.add("hl");
  if (scroll) tr.scrollIntoView({ block: "center", behavior: "smooth" });
}

// ---------- observability ----------
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
    $app.replaceChildren(gapsPanel(null, cov, r.label));
    return;
  }
  const cmp = await loadCompare();
  const ob = cmp.observability;
  const bc = cmp.coverage.baseline, cc = cmp.coverage.candidate;
  const banner = h("div", { class: `panel callout ${ob.status === "ok" ? "okay" : ob.status === "gaps" ? "point" : "note"}` },
    h("h2", null, "Is the candidate still observable? ", badge(ob.status, OBS_LABEL[ob.status])), h("div", null, md(ob.summary)));
  const struct = (s) => [["nodes", s.nodes], ["tools", s.tools], ["models", s.models]].map(([k, v]) =>
    h("div", { class: "small" }, h("b", null, `${k}: `), v.length ? v.map((x, i) => [i ? ", " : "", h("code", null, x)]) : "—"));
  const capture = h("div", { class: "panel" }, h("h2", null, "What was captured"),
    h("div", { class: "grid2" }, [["Baseline", bc], ["Candidate", cc]].map(([t, c]) => h("div", null, h("h3", null, `${t} · ${c.structure.traces} trace(s)`), struct(c.structure),
      h("div", { class: "small" }, `model calls with token usage: ${pct(c.structure.capture.llm_usage)} · tool calls with input/output: ${pct(c.structure.capture.tool_io)}`)))));
  const drift = h("div", { class: "panel" }, h("h2", null, "Structural changes (drift)"),
    cmp.coverage.drift.length ? h("ul", null, cmp.coverage.drift.map((d) => h("li", null, badge(d.severity), " ", md(d.message)))) : h("p", { class: "muted" }, "No structural changes between the two runs."));
  $app.replaceChildren(banner, gapsPanel(bc, cc, "candidate"), drift, capture, limitsPanel());
}

function gapsPanel(bc, cc, label) {
  const bItems = Object.fromEntries(((bc && bc.summary.items) || []).map((i) => [i.code, i]));
  const items = [...cc.summary.items];
  for (const code of Object.keys(bItems)) if (!items.find((i) => i.code === code)) items.push({ ...bItems[code], count: 0, cases: [], examples: [], gone: true });
  return h("div", { class: "panel" }, h("h2", null, `Instrumentation gaps (${label})`),
    h("div", { class: "legend" }, Object.entries(GAP_HELP).map(([k, v]) => h("span", { title: v }, badge(k), " ", v.split(":")[0] === "Verified" ? "proven by the trace" : k === "possible" ? "worth checking" : "capture quality"))),
    items.length ? h("table", null, h("tr", null, h("th", null, "level"), h("th", null, "gap"), bc ? h("th", { class: "num" }, "baseline") : null, h("th", { class: "num" }, label), h("th", null, "example")),
      items.map((i) => {
        const ex = (i.examples || [])[0];
        return h("tr", null, h("td", null, badge(i.level, i.level, GAP_HELP[i.level])),
          h("td", null, h("div", null, GAP_TITLES[i.code] || i.code), h("div", { class: "small muted" }, (i.cases || []).length ? `cases: ${i.cases.join(", ")}` : i.gone ? "resolved in candidate" : "")),
          bc ? h("td", { class: "num" }, bItems[i.code] ? bItems[i.code].count : 0) : null,
          h("td", { class: "num" }, i.count),
          h("td", { class: "small" }, ex ? [md(ex.message), " ", ex.case_id != null && bc ? h("a", { href: link("case", ex.case_id, { ct: ex.trial, step: ex.step || "" }) }, "open trace →") : null] : null));
      })) : h("p", { class: "muted" }, "No gaps detected in what was recorded."));
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
  $app.replaceChildren(h("div", { class: "panel" }, h("h2", null, "Runs"), h("p", { class: "muted small" }, `Stored in ${state.runs.home}`),
    runs.length ? h("table", null, h("tr", null, h("th", null, "label"), h("th", null, "run id"), h("th", { class: "num" }, "passed"), h("th", null, "target / source"), h("th", null, "commit"), h("th", null, "")),
      runs.map((r) => h("tr", null, h("td", null, h("b", null, r.label)), h("td", { class: "mono small" }, r.run_id),
        h("td", { class: "num" }, `${r.passed}/${r.trials}`), h("td", { class: "small" }, h("code", null, r.target || r.source || "—")),
        h("td", { class: "small mono" }, r.git && r.git.commit ? r.git.commit + (r.git.dirty ? "+dirty" : "") : "—"),
        h("td", null, runs.length >= 2 ? [
          h("button", { class: "btn", disabled: p.baseline === r.run_id || null, onclick: () => setPair("b", r.run_id) }, p.baseline === r.run_id ? "baseline ✓" : "use as baseline"), " ",
          h("button", { class: "btn", disabled: p.candidate === r.run_id || null, onclick: () => setPair("c", r.run_id) }, p.candidate === r.run_id ? "candidate ✓" : "use as candidate")] : null))))
      : h("p", { class: "muted" }, "No runs yet. Try ", h("code", null, "agentpatch demo"), ".")));
}

render();
