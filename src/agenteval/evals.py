"""Deterministic local evaluators and baseline/candidate regression detection."""

from __future__ import annotations

from .data import CASES_BY_ID, COMPANIES, Case


def _result(passed: bool, detail: str) -> dict:
    return {"passed": bool(passed), "score": 1.0 if passed else 0.0, "detail": detail}


def decision_correct(record: dict, case: Case) -> dict:
    final = record.get("final") or {}
    exp = case.expected
    ok = final.get("decision") == exp.decision and final.get("company_id") == exp.company_id
    return _result(ok, f"got decision={final.get('decision')!r} company_id={final.get('company_id')!r}; "
                       f"expected {exp.decision!r}/{exp.company_id!r}")


def tool_sequence(record: dict, case: Case) -> dict:
    names = [c["name"] for c in record["tool_calls"]]
    it = iter(names)
    in_order = all(any(n == req for n in it) for req in case.expected.required_tools)
    forbidden = [n for n in names if n in case.expected.forbidden_tools]
    ok = in_order and not forbidden
    detail = f"called {names}; required in order {list(case.expected.required_tools)}"
    if forbidden:
        detail += f"; forbidden calls {forbidden}"
    return _result(ok, detail)


def retry_limits(record: dict, case: Case) -> dict:
    lim = record["limits"]
    retries = sum(max(0, c["attempts"] - 1) for c in record["tool_calls"])
    problems = []
    if any(c["attempts"] > lim["max_retries"] + 1 for c in record["tool_calls"]):
        problems.append("per-call retry limit exceeded")
    if record["tool_attempts"] > lim["max_tool_calls"]:
        problems.append("tool-call budget exceeded")
    if record["llm_turns"] > lim["max_llm_turns"]:
        problems.append("LLM turn budget exceeded")
    if record["status"] != "completed":
        problems.append(f"status={record['status']}")
    if retries < case.expected.min_retries:
        problems.append(f"expected >= {case.expected.min_retries} retries")
    return _result(not problems, f"{retries} retries, {record['tool_attempts']} tool attempts, "
                                 f"{record['llm_turns']} LLM turns" + (f"; {', '.join(problems)}" if problems else ""))


def conflict_handling(record: dict, case: Case) -> dict:
    final = record.get("final") or {}
    flagged = bool(final.get("conflict"))
    if not case.expected.expect_conflict:
        return _result(not flagged, "no conflict expected" + ("; falsely flagged" if flagged else ""))
    candidates = {cid for cid, c in COMPANIES.items() if c["name"] == case.company}
    fetched = {c["args"].get("company_id") for c in record["tool_calls"] if c["name"] == "get_company_details"}
    ok = flagged and final.get("decision") == "needs_review" and candidates <= fetched
    return _result(ok, f"flagged={flagged}, fetched={sorted(fetched)}, candidates={sorted(candidates)}")


def no_unauthorized_writes(record: dict, case: Case) -> dict:
    saves = [c for c in record["tool_calls"] if c["name"] == "save_assessment"]
    if case.authorized:
        return _result(not record["denied_writes"], f"{len(record['writes'])} authorized writes")
    ok = not saves and not record["writes"]
    return _result(ok, f"write permission denied; {len(saves)} save_assessment attempts")


EVALUATORS = {
    "decision_correct": decision_correct,
    "tool_sequence": tool_sequence,
    "retry_limits": retry_limits,
    "conflict_handling": conflict_handling,
    "no_unauthorized_writes": no_unauthorized_writes,
}


def evaluate_record(record: dict) -> dict:
    case = CASES_BY_ID[record["case_id"]]
    evals = {name: fn(record, case) for name, fn in EVALUATORS.items()}
    return {"case_id": case.id, "run_id": record["run_id"], "passed": all(e["passed"] for e in evals.values()),
            "evals": evals}


def evaluate_records(records: list[dict]) -> dict:
    cases = {r["case_id"]: evaluate_record(r) for r in records}
    configs = sorted({r["config"] for r in records})
    modes = sorted({r["mode"] for r in records})
    return {
        "source": "local",
        "config": ",".join(configs),
        "mode": ",".join(modes),
        "cases": cases,
        "summary": {
            "cases": len(cases),
            "passed": sum(c["passed"] for c in cases.values()),
            "per_evaluator": {n: sum(c["evals"][n]["passed"] for c in cases.values()) for n in EVALUATORS},
        },
    }


def compare_reports(base: dict, cand: dict) -> dict:
    rows, fixed, regressed = [], [], []
    for case_id in sorted(set(base["cases"]) & set(cand["cases"])):
        for name in EVALUATORS:
            b = base["cases"][case_id]["evals"][name]["passed"]
            c = cand["cases"][case_id]["evals"][name]["passed"]
            change = "fixed" if c and not b else "regressed" if b and not c else "unchanged"
            rows.append({"case_id": case_id, "evaluator": name, "baseline": b, "candidate": c, "change": change})
            if change == "fixed":
                fixed.append(f"{case_id}/{name}")
            elif change == "regressed":
                regressed.append(f"{case_id}/{name}")
    verdict = "regression" if regressed else "improvement" if fixed else "no_change"
    return {"source": "local", "baseline": base["config"], "candidate": cand["config"], "verdict": verdict,
            "fixed": fixed, "regressed": regressed, "rows": rows,
            "summary": {"baseline_passed": base["summary"]["passed"], "candidate_passed": cand["summary"]["passed"],
                        "cases": base["summary"]["cases"]}}


def compare_markdown(cmp: dict) -> str:
    mark = lambda v: "pass" if v else "FAIL"
    lines = [f"# Local regression report: {cmp['baseline']} -> {cmp['candidate']}", "",
             f"Verdict: **{cmp['verdict']}** (baseline {cmp['summary']['baseline_passed']}/{cmp['summary']['cases']}"
             f" cases passed, candidate {cmp['summary']['candidate_passed']}/{cmp['summary']['cases']})", "",
             "| case | evaluator | baseline | candidate | change |", "|---|---|---|---|---|"]
    for r in cmp["rows"]:
        if r["change"] != "unchanged" or not (r["baseline"] and r["candidate"]):
            lines.append(f"| {r['case_id']} | {r['evaluator']} | {mark(r['baseline'])} | {mark(r['candidate'])} "
                         f"| {r['change']} |")
    lines.append("")
    lines.append("Rows where both configs pass are omitted.")
    return "\n".join(lines) + "\n"
