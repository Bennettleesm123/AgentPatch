"""Terminal and Markdown renderings of a comparison and of a single run's coverage."""

from __future__ import annotations

import os
import sys

ICON = {"critical": "✖", "warning": "!", "info": "·"}
VERDICT = {"regression": "REGRESSION", "needs_review": "NEEDS REVIEW", "improvement": "IMPROVEMENT",
           "no_change": "NO CHANGE"}


class Style:
    def __init__(self, enabled: bool | None = None):
        if enabled is None:
            enabled = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        self.on = enabled

    def _c(self, code, s):
        return f"\033[{code}m{s}\033[0m" if self.on else s

    def red(self, s): return self._c("31", s)
    def yellow(self, s): return self._c("33", s)
    def green(self, s): return self._c("32", s)
    def dim(self, s): return self._c("2", s)
    def bold(self, s): return self._c("1", s)

    def sev(self, sev, s):
        return {"critical": self.red, "warning": self.yellow}.get(sev, self.dim)(s)


def _run_label(m: dict) -> str:
    git = m.get("git") or {}
    commit = f", {git['commit']}{'+dirty' if git.get('dirty') else ''}" if git.get("commit") else ""
    return f"{m['label']} ({m['run_id']}{commit})"


def _fmt(v, unit="", pct=False):
    if v is None:
        return "n/a"
    if pct:
        return f"{v:.0%}"
    return f"{v:.0f}{unit}" if isinstance(v, float) and v >= 10 else f"{v}{unit}"


METRICS = [("success_rate", "Success rate", True, ""), ("cases_all_pass", "Cases passing every trial", False, ""),
           ("p50_ms", "Latency p50", False, " ms"), ("p95_ms", "Latency p95", False, " ms"),
           ("tool_calls_per_run", "Tool calls / run", False, ""), ("tool_errors_per_run", "Tool errors / run", False, ""),
           ("llm_calls_per_run", "Model calls / run", False, ""), ("tokens_per_run", "Tokens / run", False, ""),
           ("run_errors", "Runs that crashed", False, "")]


def metric_rows(cmp: dict) -> list[tuple[str, str, str]]:
    b, c = cmp["overall"]["baseline"], cmp["overall"]["candidate"]
    rows = []
    for key, label, pct, unit in METRICS:
        bv, cv = _fmt(b.get(key), unit, pct), _fmt(c.get(key), unit, pct)
        if key == "success_rate":
            bv += f" ({b['passed']}/{b['trials']})"
            cv += f" ({c['passed']}/{c['trials']})"
        if key == "cases_all_pass":
            bv += f"/{b['cases']}"
            cv += f"/{c['cases']}"
        rows.append((label, bv, cv))
    return rows


def _evidence(f: dict) -> str:
    ev = [e for e in f.get("evidence") or [] if e and e.get("step")]
    return ", ".join(f"{e['case_id']} trial {e['trial']} → {e['step']}" for e in ev[:2])


def compare_text(cmp: dict, style: Style | None = None, limit: int = 12) -> str:
    st = style or Style()
    v = cmp["verdict"]
    color = st.red if v == "regression" else st.yellow if v == "needs_review" else st.green
    out = [st.bold("AgentPatch comparison"),
           f"  baseline:  {_run_label(cmp['baseline'])}", f"  candidate: {_run_label(cmp['candidate'])}", "",
           color(st.bold(VERDICT[v])) + "  " + cmp["headline"], ""]
    w = max(len(r[0]) for r in metric_rows(cmp))
    for label, bv, cv in metric_rows(cmp):
        changed = bv != cv
        out.append(f"  {label.ljust(w)}  {bv:>16} → {cv if not changed else st.bold(cv)}")
    out += ["", st.bold("What changed")]
    for f in cmp["findings"][:limit]:
        out.append(f"  {st.sev(f['severity'], ICON[f['severity']])} {st.sev(f['severity'], f['id'])} {f['title']}")
        if f["detail"]:
            out.append(st.dim(f"      {f['detail']}"))
        ev = _evidence(f)
        if ev:
            out.append(st.dim(f"      evidence: {ev}"))
    if len(cmp["findings"]) > limit:
        out.append(st.dim(f"  … {len(cmp['findings']) - limit} more (see the dashboard or --md report)"))
    for n in cmp["notes"]:
        out.append(st.yellow(f"  note: {n}"))
    return "\n".join(out)


def compare_markdown(cmp: dict) -> str:
    v = cmp["verdict"]
    lines = [f"## AgentPatch: {VERDICT[v]}", "", cmp["headline"], "",
             f"Baseline `{_run_label(cmp['baseline'])}` → candidate `{_run_label(cmp['candidate'])}`", "",
             "| metric | baseline | candidate |", "|---|---|---|"]
    lines += [f"| {a} | {b} | {c} |" for a, b, c in metric_rows(cmp)]
    lines += ["", "### What changed", ""]
    for f in cmp["findings"]:
        sev = {"critical": "🔴", "warning": "🟠", "info": "⚪"}[f["severity"]]
        lines.append(f"- {sev} **{f['title']}**" + (f": {f['detail']}" if f["detail"] else "")
                     + (f" _(evidence: {_evidence(f)})_" if _evidence(f) else ""))
    diag = [r for r in cmp["cases"] if r.get("diagnosis") and r["change"] in ("regressed", "intermittent")]
    if diag:
        lines += ["", "### Failure diagnosis", ""]
    for r in diag:
        d = r["diagnosis"]
        lp = d.get("likely_failure_point")
        lines.append(f"**`{r['case_id']}`** (candidate trial {d['trial']} vs baseline trial {d['baseline_trial']})")
        if lp:
            lines.append(f"- Likely failure point: `{lp['step']['id']}`, {lp['rule']} ({lp['confidence']}).")
        lines += [f"- {x['text']}" for x in d["facts"]]
        if d["next_steps"]:
            lines.append("- Next: " + " ".join(d["next_steps"]))
        lines.append("")
    for n in cmp["notes"]:
        lines.append(f"> Note: {n}")
    return "\n".join(lines) + "\n"


def coverage_text(run_id: str, cov: dict, style: Style | None = None) -> str:
    st = style or Style()
    s, sm = cov["structure"], cov["summary"]
    cap = s["capture"]
    out = [st.bold(f"Observability coverage: {run_id}"), f"  {s['traces']} trace(s); nodes: {', '.join(s['nodes']) or '—'}",
           f"  tools: {', '.join(s['tools']) or '—'}; models: {', '.join(s['models']) or '—'}",
           f"  model calls with token usage: {_fmt(cap['llm_usage'], pct=True)}; tool calls with input/output: "
           f"{_fmt(cap['tool_io'], pct=True)}", ""]
    if not sm["items"]:
        out.append(st.green("  No gaps detected in what was recorded. (Work that never touches the instrumentation "
                            "can't be detected from traces.)"))
    for item in sm["items"]:
        color = st.red if item["level"] == "verified" else st.yellow if item["level"] == "possible" else st.dim
        out.append(color(f"  [{item['level']}] {item['code']}: {item['count']}x in {item['traces']} trace(s)"))
        cases = item.get("cases") or []
        if cases:
            out.append(st.dim(f"      cases: {', '.join(cases)}"))
        msgs = list(dict.fromkeys(ex["message"] for ex in item["examples"]))
        for msg in msgs[:2]:
            ex = next(e for e in item["examples"] if e["message"] == msg)
            where = f"e.g. {ex['case_id']} trial {ex['trial']}: " if ex.get("case_id") else ""
            out.append(st.dim(f"      {where}{msg}"))
    return "\n".join(out)


def steps_tree(trace, style: Style | None = None) -> str:
    st = style or Style()
    depth: dict[str | None, int] = {None: -1}
    out = []
    for s in trace.steps:
        d = depth.get(s.parent_id, 0) + 1
        depth[s.id] = d
        extra = []
        if s.kind == "llm" and s.attributes.get("requested_tools"):
            extra.append("requested " + ", ".join(s.attributes["requested_tools"]))
        if s.duration_ms is not None:
            extra.append(f"{s.duration_ms:.0f} ms")
        line = "  " * d + f"{s.id}" + (f"  ({'; '.join(extra)})" if extra else "")
        if s.status == "error":
            line = st.red(line + f"  ✗ {s.error}")
        out.append(line)
    return "\n".join(out)
