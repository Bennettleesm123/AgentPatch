"""Baseline vs candidate comparison: what changed, how much it matters, and the evidence.

Findings are ranked: critical (a case got worse on its checks), warning (behaviour or
observability changed in a way worth a look), info (improvements and neutral changes).
Success rates come from repeated trials. With few trials a drop can be flakiness, so partial
drops are labelled "intermittent" and the trial counts are always shown.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict

from .coverage import drift, run_coverage, unrecorded_tool_calls
from .diagnose import diagnose, error_hint
from .schema import Trace
from .store import Run
from .suite import DEFAULT_THRESHOLDS

SEVERITY = {"critical": 0, "warning": 1, "info": 2}


def _pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 1)


def _mean(xs):
    return round(statistics.fmean(xs), 2) if xs else None


def _tokens(t: Trace) -> int:
    return sum(int((s.attributes.get("usage") or {}).get("total_tokens") or 0) for s in t.of_kind("llm"))


def _norm_error(msg: str | None) -> str:
    msg = re.sub(r"0x[0-9a-f]+|[0-9a-f]{8,}", "…", msg or "", flags=re.I)
    msg = re.sub(r"\d+(\.\d+)?(ms|s)\b", "#\\2", msg)
    return msg[:140]


class RunData:
    def __init__(self, run: Run):
        self.run = run
        self.traces = run.traces()
        self.results = {(r["case_id"], r["trial"]): r for r in run.results()}
        self.by_case: dict[str, list[Trace]] = defaultdict(list)
        for t in self.traces:
            self.by_case[t.case_id].append(t)

    def result(self, t: Trace) -> dict:
        return self.results.get((t.case_id, t.trial)) or {"outcome": "unknown", "checks": []}

    def case_stats(self, case_id: str) -> dict:
        ts = self.by_case.get(case_id, [])
        outcomes = Counter(self.result(t)["outcome"] for t in ts)
        n = len(ts)
        return {"trials": n, "passed": outcomes["pass"], "failed": outcomes["fail"], "unknown": outcomes["unknown"],
                "pass_rate": round(outcomes["pass"] / n, 3) if n else None,
                "p50_ms": _pct([t.duration_ms for t in ts if t.duration_ms is not None], .5),
                "tool_calls": _mean([len(t.tool_steps()) for t in ts]), "llm_calls": _mean([len(t.of_kind("llm"))
                                                                                          for t in ts]),
                "tokens": _mean([_tokens(t) for t in ts]),
                "tool_sequence": _common_sequence(ts)}

    def check_rates(self, case_id: str) -> dict:
        out: dict[str, dict] = {}
        for t in self.by_case.get(case_id, []):
            for c in self.result(t)["checks"]:
                e = out.setdefault(c["id"], {"id": c["id"], "description": c.get("description", c["id"]),
                                             "passed": 0, "failed": 0, "unknown": 0})
                e["passed" if c["passed"] is True else "failed" if c["passed"] is False else "unknown"] += 1
        return out


def _common_sequence(ts: list[Trace]) -> list[str]:
    seqs = Counter(tuple(s.name for s in t.tool_steps() if s.status == "ok") for t in ts)
    return list(seqs.most_common(1)[0][0]) if seqs else []


def _overall(d: RunData) -> dict:
    ts = d.traces
    durs = [t.duration_ms for t in ts if t.duration_ms is not None]
    outcomes = Counter(d.result(t)["outcome"] for t in ts)
    tool_steps = [s for t in ts for s in t.tool_steps()]
    return {"trials": len(ts), "passed": outcomes["pass"], "unknown": outcomes["unknown"],
            "success_rate": round(outcomes["pass"] / len(ts), 3) if ts else None,
            "cases": len(d.by_case),
            "cases_all_pass": sum(1 for c in d.by_case if d.case_stats(c)["pass_rate"] == 1),
            "p50_ms": _pct(durs, .5), "p95_ms": _pct(durs, .95),
            "tool_calls_per_run": _mean([len(t.tool_steps()) for t in ts]),
            "tool_errors_per_run": round(sum(s.status == "error" for s in tool_steps) / len(ts), 2) if ts else None,
            "llm_calls_per_run": _mean([len(t.of_kind("llm")) for t in ts]),
            "tokens_per_run": _mean([_tokens(t) for t in ts]),
            "run_errors": sum(t.status == "error" for t in ts)}


def _tool_stats(d: RunData) -> dict:
    stats: dict[str, dict] = {}
    n = len(d.traces) or 1
    for t in d.traces:
        for s in t.tool_steps():
            e = stats.setdefault(s.name, {"calls": 0, "errors": 0, "traces": set(), "first_error": None, "ms": [],
                                          "error_cases": set()})
            e["calls"] += 1
            e["traces"].add((t.case_id, t.trial))
            if s.duration_ms is not None:
                e["ms"].append(s.duration_ms)
            if s.status == "error":
                e["errors"] += 1
                e["error_cases"].add(t.case_id)
                e["first_error"] = e["first_error"] or {"case_id": t.case_id, "trial": t.trial, "step": s.id,
                                                        "error": s.error}
        for llm, tool in unrecorded_tool_calls(t):
            e = stats.setdefault(tool, {"calls": 0, "errors": 0, "traces": set(), "first_error": None, "ms": [],
                                        "error_cases": set()})
            e["unrecorded"] = e.get("unrecorded", 0) + 1
    return {k: {"calls_per_run": round(v["calls"] / n, 2), "error_rate": round(v["errors"] / v["calls"], 3)
                if v["calls"] else None, "calls": v["calls"], "errors": v["errors"], "runs_using": len(v["traces"]),
                "p50_ms": _pct(v["ms"], .5), "first_error": v["first_error"], "unrecorded": v.get("unrecorded", 0),
                "error_cases": sorted(v["error_cases"])}
            for k, v in stats.items()}


def _step_time(d: RunData, case_id: str) -> dict[str, float]:
    """Mean self-time-ish total per step (kind:name) per trace, for latency attribution (top-level only)."""
    totals: dict[str, float] = defaultdict(float)
    ts = d.by_case.get(case_id, [])
    for t in ts:
        root = t.root
        for s in t.steps:
            if s.parent_id == (root.id if root else None) and s.duration_ms is not None:
                totals[f"{s.kind}:{s.name}"] += s.duration_ms
    return {k: v / len(ts) for k, v in totals.items()} if ts else {}


def _errors(d: RunData) -> dict[tuple, dict]:
    out: dict[tuple, dict] = {}
    for t in d.traces:
        for s in t.error_origins():
            key = (f"{s.kind}:{s.name}", _norm_error(s.error))
            e = out.setdefault(key, {"step": key[0], "message": key[1], "count": 0, "runs": set(), "fatal": 0,
                                     "example": {"case_id": t.case_id, "trial": t.trial, "step": s.id,
                                                 "error": s.error}})
            e["count"] += 1
            e["runs"].add((t.case_id, t.trial))
        if t.status == "error" and t.error_origins():
            last = t.error_origins()[-1]
            out[(f"{last.kind}:{last.name}", _norm_error(last.error))]["fatal"] += 1
    return out


def _pick_base(d: RunData, case_id: str) -> Trace | None:
    ts = d.by_case.get(case_id, [])
    passing = [t for t in ts if d.result(t)["outcome"] == "pass"]
    return (passing or ts or [None])[0]


def compare(base_run: Run, cand_run: Run, thresholds: dict | None = None) -> dict:
    th = dict(DEFAULT_THRESHOLDS)
    th.update(cand_run.info.get("thresholds") or base_run.info.get("thresholds") or {})
    th.update(thresholds or {})
    b, c = RunData(base_run), RunData(cand_run)
    findings: list[dict] = []

    def add(severity, category, title, detail="", cases=(), evidence=(), **extra):
        findings.append({"severity": severity, "category": category, "title": title, "detail": detail,
                         "cases": list(cases), "evidence": list(evidence), **extra})

    cases, slow = [], []
    all_ids = list(dict.fromkeys(list(b.by_case) + list(c.by_case)))
    for cid in all_ids:
        bs, cs = b.case_stats(cid), c.case_stats(cid)
        row = {"case_id": cid, "baseline": bs, "candidate": cs, "checks": [], "diagnosis": None,
               "latency": None}
        if not bs["trials"] or not cs["trials"]:
            row["change"] = "added" if not bs["trials"] else "removed"
            add("info", "suite", f"Case `{cid}` only ran in the {'candidate' if row['change'] == 'added' else 'baseline'}",
                "It can't be compared.", [cid])
            cases.append(row)
            continue
        br, cr = bs["pass_rate"], cs["pass_rate"]
        if cr < br:
            consistent = cr == 0 or br - cr >= 0.5
            row["change"] = "regressed" if consistent else "intermittent"
        elif cr > br:
            row["change"] = "fixed"
        elif cr < 1:
            row["change"] = "still_failing"
        else:
            row["change"] = "unchanged"
        bchecks, cchecks = b.check_rates(cid), c.check_rates(cid)
        for chk_id in dict.fromkeys(list(bchecks) + list(cchecks)):
            bx, cx = bchecks.get(chk_id), cchecks.get(chk_id)
            if bx is None or cx is None:
                row["checks"].append({"id": chk_id, "change": "added" if bx is None else "removed",
                                      "description": (cx or bx)["description"], "baseline": bx, "candidate": cx})
                continue
            bpr = bx["passed"] / bs["trials"]
            cpr = cx["passed"] / cs["trials"]
            ch = "regressed" if cpr < bpr else "fixed" if cpr > bpr else ("failing" if cpr < 1 else "passing")
            row["checks"].append({"id": chk_id, "description": cx["description"], "change": ch,
                                  "baseline": bx, "candidate": cx})
        failing = [t for t in c.by_case[cid] if c.result(t)["outcome"] != "pass"]
        if failing:
            ct = failing[0]
            bt = _pick_base(b, cid)
            row["diagnosis"] = diagnose(ct, c.result(ct), bt, b.result(bt) if bt else None)
        # latency
        bp, cp = bs["p50_ms"], cs["p50_ms"]
        if bp is not None and cp is not None:
            delta = cp - bp
            pct = delta / bp * 100 if bp else 0
            row["latency"] = {"baseline_p50": bp, "candidate_p50": cp, "delta_ms": round(delta, 1),
                              "delta_pct": round(pct, 1)}
            if delta >= th["latency_min_ms"] and pct >= th["latency_pct"]:
                bst, cst = _step_time(b, cid), _step_time(c, cid)
                contrib = sorted(((k, cst.get(k, 0) - bst.get(k, 0), k not in bst) for k in set(bst) | set(cst)),
                                 key=lambda x: -x[1])
                top = [x for x in contrib if x[1] > 0][:3]
                row["latency"]["attribution"] = [{"step": k, "delta_ms": round(v, 1), "new": new} for k, v, new in top]
                slow.append((cid, bp, cp, pct, top))
        cases.append(row)

        if row["change"] in ("regressed", "intermittent"):
            d = row["diagnosis"]
            lp = (d or {}).get("likely_failure_point") or {}
            step = (lp.get("step") or {}).get("id")
            regressed_checks = [x for x in row["checks"] if x["change"] == "regressed"]
            add("critical" if row["change"] == "regressed" else "warning", "regression",
                f"`{cid}` regressed: passed {bs['passed']}/{bs['trials']} → {cs['passed']}/{cs['trials']} trials"
                + (" (intermittent)" if row["change"] == "intermittent" else ""),
                "Failing: " + "; ".join(f"{x['description']}" for x in regressed_checks)
                + (f". Likely failure point: {step} ({lp.get('rule')})." if step else "."),
                [cid], [{"case_id": cid, "trial": d["trial"], "step": step}] if d else [])
        elif row["change"] == "fixed":
            add("info", "fix", f"`{cid}` fixed: passed {bs['passed']}/{bs['trials']} → {cs['passed']}/{cs['trials']}",
                "Now passing: " + "; ".join(x["description"] for x in row["checks"] if x["change"] == "fixed"), [cid])
        if cs["unknown"] > bs["unknown"]:
            add("warning", "observability", f"`{cid}`: {cs['unknown']} trial(s) couldn't be verified",
                "A check depended on a tool call that the model requested but the trace didn't record.", [cid])

    groups: dict[str, list] = defaultdict(list)
    for item in slow:
        groups[item[4][0][0] if item[4] else ""].append(item)
    for step_key, items in groups.items():
        names = ", ".join(f"`{x[0]}` {x[1]:.0f}→{x[2]:.0f} ms" for x in items)
        top = items[0][4][0] if items[0][4] else None
        share = ""
        if top:
            deltas = [x[2] - x[1] for x in items]
            contrib = [next((v for k, v, _ in x[4] if k == step_key), 0) for x in items]
            share = (f" Most of the increase is in {'new step ' if top[2] else ''}`{step_key}` "
                     f"(about {statistics.median(contrib):.0f} ms of a typical +{statistics.median(deltas):.0f} ms "
                     "slowdown per run).")
        title = (f"{len(items)} cases are slower" if len(items) > 1 else f"`{items[0][0]}` is slower") + \
            (f", mostly in `{step_key}`" if top else "")
        add("warning", "latency", title, f"p50: {names}.{share}", [x[0] for x in items],
            [{"case_id": x[0], "trial": 0, "step": None} for x in items[:1]])

    # Failure points of regressed cases, by step name: errors there are reported inside the regression.
    explained: dict[str, str] = {}
    for r in cases:
        lp = ((r.get("diagnosis") or {}).get("likely_failure_point") or {}).get("step") or {}
        if r["change"] in ("regressed", "intermittent", "still_failing") and lp.get("id"):
            explained[r["case_id"]] = lp["name"]

    def _explained(name: str, case_ids) -> bool:
        case_ids = list(case_ids)
        return bool(case_ids) and all(explained.get(cid) == name for cid in case_ids)

    # tools
    bt, ct = _tool_stats(b), _tool_stats(c)
    tool_rows = []
    for name in sorted(set(bt) | set(ct)):
        x, y = bt.get(name), ct.get(name)
        tool_rows.append({"tool": name, "baseline": x, "candidate": y})
        if x and y and x["calls"] and y["calls"]:
            new_err_cases = set(y["error_cases"]) - set(x["error_cases"])
            if (y["error_rate"] or 0) - (x["error_rate"] or 0) >= 0.1 and y["errors"] > x["errors"] \
                    and not _explained(name, new_err_cases):
                ev = [y["first_error"]] if y["first_error"] else []
                add("warning", "tools", f"`{name}` fails more often: {x['error_rate']:.0%} → {y['error_rate']:.0%} of calls",
                    f"First new error: {y['first_error']['error']}" if y["first_error"] else "", evidence=ev)
            base_cpr = x["calls_per_run"]
            if base_cpr and abs(y["calls_per_run"] - base_cpr) / base_cpr * 100 >= th["tool_calls_pct"] \
                    and abs(y["calls_per_run"] - base_cpr) >= 0.5:
                add("info", "tools", f"`{name}` calls per run: {base_cpr} → {y['calls_per_run']}")
        elif x and x["calls"] and not (y and (y["calls"] or y["unrecorded"])):
            add("warning", "tools", f"`{name}` is no longer called", f"Baseline called it {x['calls_per_run']}x per run.")
    # errors
    be, ce = _errors(b), _errors(c)
    for key, e in ce.items():
        if key not in be:
            hint = error_hint(e["message"])
            if _explained(e["step"].split(":", 1)[-1], (case for case, _ in e["runs"])):
                for f in findings:
                    if f["category"] == "regression" and set(f["cases"]) & {case for case, _ in e["runs"]} \
                            and e["message"] not in f["detail"]:
                        f["detail"] += f" New error there: {e['message']}" + (f" ({hint})." if hint else ".")
                continue
            add("warning", "errors", f"New error in `{e['step']}`: {e['message']}",
                f"{e['count']}x in {len(e['runs'])} run(s), {e['fatal']} of them ended the run"
                + (f"; classified as {hint}" if hint else "") + ".",
                sorted({r[0] for r in e["runs"]}), [e["example"]])
    error_rows = [{"step": e["step"], "message": e["message"], "baseline": be.get(k, {}).get("count", 0),
                   "candidate": e["count"], "fatal": e["fatal"], "example": e["example"]} for k, e in ce.items()]
    error_rows += [{"step": e["step"], "message": e["message"], "baseline": e["count"], "candidate": 0,
                    "fatal": 0, "example": e["example"]} for k, e in be.items() if k not in ce]

    bo, co = _overall(b), _overall(c)
    if bo["tokens_per_run"] and co["tokens_per_run"]:
        pct = (co["tokens_per_run"] - bo["tokens_per_run"]) / bo["tokens_per_run"] * 100
        if abs(pct) >= th["tokens_pct"]:
            add("info", "cost", f"Tokens per run {bo['tokens_per_run']:.0f} → {co['tokens_per_run']:.0f} ({pct:+.0f}%)",
                "A cost signal, not a quality signal.")

    # coverage + drift
    b_cov = run_coverage(b.traces, base_run.info.get("graph"), set(base_run.info.get("checked_tools") or []),
                         base_run.info.get("coverage_expectations"), th)
    c_cov = run_coverage(c.traces, cand_run.info.get("graph"), set(cand_run.info.get("checked_tools") or []),
                         cand_run.info.get("coverage_expectations"), th)
    suite_tools = set(cand_run.info.get("checked_tools") or []) if cand_run.info.get("suite") else None
    drift_items = drift(b_cov, c_cov, suite_tools)
    for item in drift_items:
        if item["code"] in ("new_verified_gap", "tool_capture_lost"):
            continue  # reported below as observability findings, with evidence
        add(item["severity"], "drift", item["message"])
    b_codes = {i["code"]: i for i in b_cov["summary"]["items"]}
    for item in c_cov["summary"]["items"]:
        if item["level"] == "info":
            continue
        new = item["code"] not in b_codes
        if item["level"] == "verified" or new:
            ex = item["examples"][0] if item["examples"] else {}
            add("warning" if item["level"] == "verified" else "info", "observability",
                f"{'Verified' if item['level'] == 'verified' else 'Possible'} gap: {_GAP_TITLES.get(item['code'], item['code'])}"
                f" ({item['count']}x in {item['traces']} trace(s))", ex.get("message", item["message"]),
                [ex["case_id"]] if ex.get("case_id") else [],
                [{"case_id": ex.get("case_id"), "trial": ex.get("trial"), "step": ex.get("step")}] if ex.get("step") else [],
                gap=item["code"], new=new)

    findings.sort(key=lambda f: (SEVERITY[f["severity"]], _CATEGORY_ORDER.get(f["category"], 9)))
    for i, f in enumerate(findings):
        f["id"] = f"F{i + 1}"
    counts = Counter(f["severity"] for f in findings)
    regressions = [r for r in cases if r["change"] in ("regressed", "intermittent")]
    fixes = [r for r in cases if r["change"] == "fixed"]
    verdict = ("regression" if counts["critical"] else "needs_review" if counts["warning"]
               else "improvement" if fixes else "no_change")
    notes = []
    if min(base_run.info.get("repeats") or 1, cand_run.info.get("repeats") or 1) < 3:
        notes.append("Fewer than 3 trials per case: a single failure can't be told apart from flakiness.")
    if set(b.by_case) != set(c.by_case):
        notes.append("The two runs don't cover the same cases; only shared cases are compared.")
    changed_checks = [r["case_id"] for r in cases if any(x["change"] in ("added", "removed") for x in r["checks"])]
    if changed_checks:
        notes.append("Check definitions differ between runs for: " + ", ".join(changed_checks) + ".")
    return {
        "baseline": _run_meta(base_run), "candidate": _run_meta(cand_run), "verdict": verdict,
        "headline": _headline(regressions, fixes, counts, findings), "counts": dict(counts),
        "observability": _observability(b_cov, c_cov, drift_items),
        "overall": {"baseline": bo, "candidate": co}, "cases": cases, "tools": tool_rows, "errors": error_rows,
        "coverage": {"baseline": b_cov, "candidate": c_cov, "drift": drift_items}, "findings": findings,
        "thresholds": th, "notes": notes,
    }


_GAP_TITLES = {"unrecorded_tool_call": "model-requested tool calls with no recorded tool step",
               "orphan_step": "steps whose parent wasn't recorded", "unclosed_step": "steps that never finished",
               "error_without_message": "errors without a message", "empty_trace": "traces with no steps",
               "untraced_time": "node time not covered by any recorded step",
               "declared_node_not_observed": "declared graph node never observed",
               "check_references_unseen_tool": "checks reference a tool that never appeared",
               "expected_tool_missing": "expected tool never recorded",
               "expected_node_missing": "expected node never recorded"}
_CATEGORY_ORDER = {"regression": 0, "errors": 1, "observability": 2, "drift": 3, "tools": 4, "latency": 5,
                   "cost": 6, "fix": 7, "suite": 8}


def _observability(b_cov: dict, c_cov: dict, drift_items: list[dict]) -> dict:
    """One-line answer to "is the candidate still observable?", from verified and possible gaps."""
    b_items = {i["code"]: i for i in b_cov["summary"]["items"]}
    c_items = [i for i in c_cov["summary"]["items"] if i["level"] != "info"]
    verified = [i for i in c_items if i["level"] == "verified"]
    possible = [i for i in c_items if i["level"] == "possible"]
    structural = [d for d in drift_items if d["code"] in
                  ("node_no_longer_observed", "tool_removed", "capture_rate_dropped")]

    def phrase(i):
        new = "new" if i["code"] not in b_items else f"baseline {b_items[i['code']]['count']}x"
        return f"{_GAP_TITLES.get(i['code'], i['code'])} ({i['count']}x, {new})"

    if verified:
        status = "gaps"
        summary = "Verified instrumentation gaps: " + "; ".join(phrase(i) for i in verified) + "."
    elif possible or structural:
        status = "check"
        summary = "No verified gaps, but worth checking: " + "; ".join(
            [phrase(i) for i in possible] + [d["message"] for d in structural]) + "."
    else:
        status = "ok"
        summary = "No instrumentation gaps detected in what was recorded."
    if possible and verified:
        summary += " Also possible: " + "; ".join(phrase(i) for i in possible) + "."
    summary += " (Work that never touches the instrumentation can't be seen in traces at all.)"
    return {"status": status, "summary": summary, "verified": sum(i["count"] for i in verified),
            "possible": sum(i["count"] for i in possible), "capture": c_cov["structure"]["capture"]}


def _run_meta(run: Run) -> dict:
    keys = ("run_id", "label", "created_at", "suite", "target", "agent_kind", "repeats", "git", "source")
    return {k: run.info.get(k) for k in keys}


def _headline(regressions, fixes, counts, findings) -> str:
    parts = []
    hard = [r for r in regressions if r["change"] == "regressed"]
    soft = [r for r in regressions if r["change"] == "intermittent"]
    if hard:
        parts.append(f"{len(hard)} case{'s' * (len(hard) != 1)} regressed ({', '.join(r['case_id'] for r in hard)})")
    if soft:
        parts.append(f"{len(soft)} intermittent regression{'s' * (len(soft) != 1)} "
                     f"({', '.join(r['case_id'] for r in soft)})")
    if fixes:
        parts.append(f"{len(fixes)} fixed ({', '.join(r['case_id'] for r in fixes)})")
    obs = sum(1 for f in findings if f["category"] in ("observability", "drift") and f["severity"] != "info")
    other = counts.get("warning", 0) - obs - len(soft)
    if obs:
        parts.append(f"{obs} observability warning{'s' * (obs != 1)}")
    if other > 0:
        parts.append(f"{other} other change{'s' * (other != 1)} to review")
    return ("; ".join(parts) + ".") if parts else "No meaningful changes between baseline and candidate."
