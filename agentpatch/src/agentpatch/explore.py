"""Read models for the dashboard's inspection views: experiment comparison, trace list and datasets.

These only reshape what the run store already holds (results, traces, run metadata); every number
shown in those views comes from here so it can be tested without a browser.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .coverage import trace_gaps
from .schema import Trace
from .store import Run
from .suite import SuiteError, load_suite

NO_SUITE = "(no suite)"


def _pct(values: list[float], q: float) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    i = min(len(vals) - 1, max(0, round(q * (len(vals) - 1))))
    return round(vals[i], 3)


def _mean(values) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def usage(t: Trace) -> dict:
    total = {"input": 0, "output": 0}
    for s in t.of_kind("llm"):
        u = s.attributes.get("usage") or {}
        total["input"] += int(u.get("input_tokens") or 0)
        total["output"] += int(u.get("output_tokens") or 0)
    return total


def output_preview(t: Trace | None) -> Any:
    """The part of a trace's final output worth showing in a table cell (graph state minus the message log)."""
    if t is None:
        return None
    if t.status == "error":
        return {"error": t.error}
    out = t.output
    if isinstance(out, dict) and "messages" in out:
        rest = {k: v for k, v in out.items() if k != "messages"}
        if rest:
            return rest
        msgs = out["messages"] or []
        last = msgs[-1] if msgs else None
        return last.get("content") if isinstance(last, dict) else last
    return out


def case_info(run: Run) -> dict[str, dict]:
    """Case descriptions and inputs, recorded at run time; older runs fall back to the suite file and traces."""
    info = {k: dict(v) for k, v in (run.info.get("case_info") or {}).items()}
    if not info and run.info.get("suite_path") and Path(run.info["suite_path"]).is_file():
        try:
            suite = load_suite(run.info["suite_path"])
            info = {c.id: {"description": c.description, "input": c.input} for c in suite.cases}
        except (SuiteError, OSError):
            info = {}
    for cid in run.info.get("cases") or []:
        e = info.setdefault(cid, {"description": "", "input": None})
        if e.get("input") is None:
            t = run.trace(cid, 0)
            e["input"] = t.input if t else None
    return info


def trace_summary(t: Trace, result: dict | None, thresholds: dict | None = None) -> dict:
    tools = t.of_kind("tool")
    gaps = trace_gaps(t, thresholds)
    u = usage(t)
    return {
        "case_id": t.case_id, "trial": t.trial, "trace_id": t.trace_id,
        "outcome": (result or {}).get("outcome", "unchecked"), "status": t.status, "error": t.error,
        "started_at": t.started_at, "duration_ms": t.duration_ms, "steps": len(t.steps),
        "llm_calls": len(t.of_kind("llm")), "tool_calls": len(tools),
        "tool_errors": sum(1 for s in t.steps if s.kind in ("tool", "attempt") and s.status == "error"),
        "tokens_in": u["input"], "tokens_out": u["output"],
        "failed": [c["id"] for c in (result or {}).get("checks", []) if c["passed"] is False],
        "gaps": {lvl: sum(1 for g in gaps if g["level"] == lvl) for lvl in ("verified", "possible", "info")},
        "input": t.input, "output": output_preview(t),
    }


def traces_table(run: Run) -> list[dict]:
    res = {(x["case_id"], x["trial"]): x for x in run.results()}
    th = run.info.get("thresholds")
    return [trace_summary(t, res.get((t.case_id, t.trial)), th) for t in run.traces()]


def _check_types(results: list[dict]) -> list[str]:
    seen: dict[str, None] = {}
    for x in results:
        for c in x["checks"]:
            seen.setdefault(c["type"], None)
    return list(seen)


def _score(trials: list[dict], typ: str) -> dict | None:
    vals = [c["passed"] for x in trials for c in x["checks"] if c["type"] == typ]
    if not vals:
        return None
    known = [v for v in vals if v is not None]
    return {"score": round(sum(known) / len(known), 3) if known else None,
            "passed": sum(known), "total": len(known), "unknown": len(vals) - len(known)}


def _reference(trials: list[dict]) -> list[str]:
    if not trials:
        return []
    return [c["description"] for c in trials[0]["checks"] if c["type"] == "output"]


def _row_score(row: dict, side: str, typ: str) -> float | None:
    s = (row["runs"].get(side) or {}).get("scores", {}).get(typ)
    return s["score"] if s else None


def experiment(base: Run, cand: Run, cmp: dict) -> dict:
    """LangSmith-style experiment comparison: one row per case, one column group per check type."""
    sides = {"a": base, "b": cand}
    loaded = {}
    for side, run in sides.items():
        results = run.results()
        loaded[side] = {"results": results, "traces": {(t.case_id, t.trial): t for t in run.traces()}}
    types = _check_types(loaded["a"]["results"] + loaded["b"]["results"])
    change = {r["case_id"]: r["change"] for r in cmp.get("cases", [])}
    info = {**case_info(base), **case_info(cand)}
    order: dict[str, None] = {}
    for run in (base, cand):
        for cid in run.info.get("cases") or []:
            order.setdefault(cid, None)
    for side in sides:
        for x in loaded[side]["results"]:
            order.setdefault(x["case_id"], None)

    rows = []
    for cid in order:
        row = {"case_id": cid, "description": (info.get(cid) or {}).get("description", ""),
               "input": (info.get(cid) or {}).get("input"), "change": change.get(cid), "runs": {}}
        ref = []
        for side in sides:
            trials = sorted((x for x in loaded[side]["results"] if x["case_id"] == cid), key=lambda x: x["trial"])
            if not trials:
                row["runs"][side] = None
                continue
            ref = ref or _reference(trials)
            ts = [loaded[side]["traces"].get((cid, x["trial"])) for x in trials]
            us = [usage(t) for t in ts if t]
            first = next((t for t in ts if t), None)
            row["runs"][side] = {
                "trials": [{"trial": x["trial"], "outcome": x["outcome"], "duration_ms": x.get("duration_ms")} for x in trials],
                "passed": sum(1 for x in trials if x["outcome"] == "pass"),
                "scores": {typ: _score(trials, typ) for typ in types},
                "p50_ms": _pct([x.get("duration_ms") for x in trials], .5),
                "tokens": _mean([u["input"] + u["output"] for u in us]),
                "output": output_preview(first), "output_trial": first.trial if first else None,
            }
        row["reference"] = ref
        rows.append(row)

    summary = {}
    for side, run in sides.items():
        results = loaded[side]["results"]
        traces = list(loaded[side]["traces"].values())
        us = [usage(t) for t in traces]
        durs = [x.get("duration_ms") for x in results]
        summary[side] = {
            "run_id": run.id, "label": run.label, "created_at": run.info.get("created_at"),
            "git": run.info.get("git"), "trials": len(results),
            "passed": sum(1 for x in results if x["outcome"] == "pass"),
            "scores": {typ: _mean([_row_score(r, side, typ) for r in rows]) for typ in types},
            "p50_ms": _pct(durs, .5), "p99_ms": _pct(durs, .99),
            "tokens_in": _mean([u["input"] for u in us]), "tokens_out": _mean([u["output"] for u in us]),
            "tool_errors": _mean([sum(1 for s in t.steps if s.kind in ("tool", "attempt") and s.status == "error")
                                  for t in traces]),
        }
    deltas = {}
    for typ in types:
        up = down = 0
        for r in rows:
            sa, sb = _row_score(r, "a", typ), _row_score(r, "b", typ)
            if sa is None or sb is None:
                continue
            up += sb > sa
            down += sb < sa
        deltas[typ] = {"improved": up, "regressed": down}
    return {"baseline": summary["a"], "candidate": summary["b"], "check_types": types, "deltas": deltas, "rows": rows}


def _suite_name(run: Run) -> str:
    return run.info.get("suite") or NO_SUITE


def datasets(runs: list[Run]) -> list[dict]:
    """Suites seen in the store (a suite is the dataset: its cases are the examples)."""
    by: dict[str, dict] = {}
    for r in runs:
        d = by.setdefault(_suite_name(r), {"name": _suite_name(r), "cases": {}, "runs": 0, "last_run": None,
                                           "last_created": None})
        for cid in r.info.get("cases") or []:
            d["cases"].setdefault(cid, None)
        d["runs"] += 1
        if d["last_created"] is None or (r.info.get("created_at") or "") > d["last_created"]:
            d["last_created"], d["last_run"] = r.info.get("created_at"), r.label
    return [{**d, "cases": len(d["cases"])} for d in by.values()]


def dataset(runs: list[Run], name: str) -> dict | None:
    mine = [r for r in runs if _suite_name(r) == name]
    if not mine:
        return None
    newest_first = sorted(mine, key=lambda r: r.info.get("created_at") or "", reverse=True)
    info: dict[str, dict] = {}
    for r in reversed(newest_first):
        info.update(case_info(r))
    checks: dict[str, list] = {}
    history: dict[str, list] = {}
    order: dict[str, None] = {}
    experiments = []
    for r in newest_first:
        results = r.results()
        for cid in r.info.get("cases") or []:
            order.setdefault(cid, None)
        by_case: dict[str, list] = {}
        for x in results:
            by_case.setdefault(x["case_id"], []).append(x)
            order.setdefault(x["case_id"], None)
        for cid, xs in by_case.items():
            if cid not in checks:
                checks[cid] = [{"id": c["id"], "type": c["type"], "description": c["description"]} for c in xs[0]["checks"]]
            history.setdefault(cid, []).append({"run_id": r.id, "label": r.label, "created_at": r.info.get("created_at"),
                                                "passed": sum(1 for x in xs if x["outcome"] == "pass"), "trials": len(xs)})
        durs = [x.get("duration_ms") for x in results]
        experiments.append({"run_id": r.id, "label": r.label, "created_at": r.info.get("created_at"),
                            "target": r.info.get("target") or r.info.get("source"), "git": r.info.get("git"),
                            "trials": len(results), "passed": sum(1 for x in results if x["outcome"] == "pass"),
                            "p50_ms": _pct(durs, .5), "cases": len(by_case)})
    examples = [{"case_id": cid, "description": (info.get(cid) or {}).get("description", ""),
                 "input": (info.get(cid) or {}).get("input"), "checks": checks.get(cid, []),
                 "history": history.get(cid, [])} for cid in order]
    return {"name": name, "examples": examples, "experiments": experiments}
