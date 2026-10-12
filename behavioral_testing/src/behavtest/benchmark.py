"""Reproducible accuracy benchmark over the ground-truth cases. Metrics are kept separate on purpose:

- compile: does the requirement become the right rule (or correctly abstain)?
- verdict (gold rule): given the *correct* rule, is pass/fail/unknown right? Isolates the evaluator.
- end-to-end: compiled rule -> verdict, so compile errors propagate.
- evidence: exact set match and Jaccard of evidence ids, plus first-violation match (gold rules).
- claims: diagnosis claims without valid citations, and citations of known decoy events.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

from .compiler import CompileResult, compile_requirement
from .diagnosis import diagnose, unsupported_claims
from .evaluator import evaluate
from .events import build_events
from .fixtures import build_trace
from .rules import parse_rule

CASES_DIR = Path(__file__).resolve().parents[2] / "cases"
LABELS = ("pass", "fail", "unknown")


def load_cases(name: str = "ground_truth.json") -> list[dict]:
    return json.loads((CASES_DIR / name).read_text())["cases"]


def _semantic(rule: dict | None) -> dict | None:
    return None if rule is None else parse_rule(rule).semantic()


def _prf(pairs: list[tuple[str, str]]) -> dict:
    out = {}
    for label in LABELS:
        tp = sum(1 for e, p in pairs if e == label and p == label)
        pred = sum(1 for _, p in pairs if p == label)
        gold = sum(1 for e, _ in pairs if e == label)
        out[label] = {"precision": tp / pred if pred else None, "recall": tp / gold if gold else None,
                      "support": gold, "predicted": pred}
    out["accuracy"] = sum(e == p for e, p in pairs) / len(pairs) if pairs else None
    out["confusion"] = {e: {p: sum(1 for x, y in pairs if x == e and y == p) for p in (*LABELS, "not_evaluated")}
                        for e in LABELS}
    return out


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))], 3)


def compile_metrics(cases: list[dict], compiler) -> tuple[dict, list[dict], list[float]]:
    rows, lat = [], []
    for c in cases:
        t0 = time.perf_counter()
        cr: CompileResult = compiler(c["requirement"])
        lat.append((time.perf_counter() - t0) * 1000)
        exp = c["expected"]
        rule_ok = (cr.status == "compiled" and exp["status"] == "compiled"
                   and _semantic(cr.rule) == _semantic(exp["rule"]))
        rows.append({"id": c["id"], "expected_status": exp["status"], "status": cr.status, "rule_ok": rule_ok,
                     "issues": cr.issues, "method": cr.method, "rule": cr.rule})
    should_compile = [r for r in rows if r["expected_status"] == "compiled"]
    should_abstain = [r for r in rows if r["expected_status"] != "compiled"]
    m = {
        "n": len(rows),
        "status_accuracy": sum(r["status"] == r["expected_status"] for r in rows) / len(rows),
        "semantic_rule_accuracy": (sum(r["rule_ok"] for r in should_compile) / len(should_compile)
                                   if should_compile else None),
        "n_should_compile": len(should_compile),
        "correct_abstention": (sum(r["status"] != "compiled" for r in should_abstain) / len(should_abstain)
                               if should_abstain else None),
        "abstention_label_accuracy": (sum(r["status"] == r["expected_status"] for r in should_abstain)
                                      / len(should_abstain) if should_abstain else None),
        "n_should_abstain": len(should_abstain),
        "false_abstentions": sum(r["status"] != "compiled" for r in should_compile),
        "unsafe_compiles": sum(r["status"] == "compiled" and not r["rule_ok"] for r in rows),
    }
    return m, rows, lat


def run_benchmark(compiler=compile_requirement, model_usage: dict | None = None) -> dict:
    cases = load_cases()
    comp, comp_rows, comp_lat = compile_metrics(cases, compiler)
    para = load_cases("paraphrases.json")
    para_m, para_rows, _ = compile_metrics(para, compiler)
    held_m, held_rows, _ = compile_metrics(load_cases("heldout.json"), compiler)
    gold_pairs, e2e_pairs, ev_exact, ev_jac, fv, claims, bad, decoys, per_case = [], [], [], [], [], 0, 0, 0, []
    eval_lat, diag_lat = [], []
    by_id = {r["id"]: r for r in comp_rows}
    for c in cases:
        exp = c["expected"]
        row = {"id": c["id"], "category": c["category"], "compile_status": by_id[c["id"]]["status"],
               "compile_ok": by_id[c["id"]]["status"] == exp["status"] and
               (exp["status"] != "compiled" or by_id[c["id"]]["rule_ok"])}
        if exp["verdict"] is not None:
            log = build_events(build_trace(c["trace"]))
            rule = parse_rule(exp["rule"]).with_identity(c["requirement"])
            t0 = time.perf_counter()
            res = evaluate(rule, log)
            eval_lat.append((time.perf_counter() - t0) * 1000)
            t0 = time.perf_counter()
            diag = diagnose(res, log)
            diag_lat.append((time.perf_counter() - t0) * 1000)
            gold_pairs.append((exp["verdict"], res.verdict))
            got, want = set(res.evidence), set(exp["evidence"])
            ev_exact.append(got == want)
            ev_jac.append(len(got & want) / len(got | want) if got | want else 1.0)
            if exp.get("first_violation"):
                fv.append(diag.first_violation is not None and set(diag.first_violation.event_ids)
                          == set(exp["first_violation"]))
            n_bad = len(unsupported_claims(diag))
            cited = {i for cl in diag.claims() for i in cl.cites}
            n_decoy = len(cited & set(c.get("must_not_cite", [])))
            claims += len(diag.claims())
            bad += n_bad
            decoys += n_decoy
            compiled = by_id[c["id"]]
            e2e = (evaluate(parse_rule(compiled["rule"]), log).verdict if compiled["status"] == "compiled"
                   else "not_evaluated")
            e2e_pairs.append((exp["verdict"], e2e))
            row.update(expected=exp["verdict"], verdict=res.verdict, end_to_end=e2e, evidence_exact=got == want,
                       missing_evidence=sorted(want - got), extra_evidence=sorted(got - want),
                       unsupported_claims=n_bad, decoy_citations=n_decoy)
        per_case.append(row)
    return {
        "cases": len(cases),
        "compile": comp,
        "compile_paraphrases": para_m,
        "compile_heldout": held_m,
        "verdict_gold_rules": _prf(gold_pairs),
        "unknown_reported_as_pass": sum(1 for e, p in gold_pairs if e == "unknown" and p == "pass"),
        "verdict_end_to_end": _prf(e2e_pairs),
        "evidence": {"n": len(ev_exact), "exact_set_match": sum(ev_exact) / len(ev_exact),
                     "mean_jaccard": round(statistics.mean(ev_jac), 4),
                     "first_violation_match": sum(fv) / len(fv) if fv else None, "n_first_violation": len(fv)},
        "explanations": {"claims": claims, "unsupported_claims": bad, "decoy_citations": decoys},
        "latency_ms": {stage: {"p50": _pct(xs, .5), "p95": _pct(xs, .95), "n": len(xs)}
                       for stage, xs in (("compile", comp_lat), ("evaluate", eval_lat), ("diagnose", diag_lat))},
        "model_usage": model_usage or {"mode": "offline", "calls": 0},
        "per_case": per_case,
        "paraphrase_rows": para_rows + held_rows,
    }


def _f(x) -> str:
    return "n/a" if x is None else f"{x:.3f}" if isinstance(x, float) else str(x)


def to_markdown(b: dict) -> str:
    c, p, h, g, e = b["compile"], b["compile_paraphrases"], b["compile_heldout"], b["verdict_gold_rules"], b["evidence"]
    lines = [f"# behavtest benchmark ({b['cases']} ground-truth cases, model usage: {b['model_usage']})", "",
             "| metric | value |", "|---|---|",
             f"| compile status accuracy | {_f(c['status_accuracy'])} ({c['n']}) |",
             f"| semantic rule accuracy | {_f(c['semantic_rule_accuracy'])} ({c['n_should_compile']}) |",
             f"| correct abstention | {_f(c['correct_abstention'])} ({c['n_should_abstain']}) |",
             f"| abstention label (ambiguous vs unsupported) | {_f(c['abstention_label_accuracy'])} |",
             f"| unsafe compiles / false abstentions | {c['unsafe_compiles']} / {c['false_abstentions']} |",
             f"| paraphrase set A (adjusted once): status / semantic / unsafe | {_f(p['status_accuracy'])} / "
             f"{_f(p['semantic_rule_accuracy'])} / {p['unsafe_compiles']} ({p['n']}) |",
             f"| paraphrase set B (adjusted once): status / semantic / unsafe | {_f(h['status_accuracy'])} / "
             f"{_f(h['semantic_rule_accuracy'])} / {h['unsafe_compiles']} ({h['n']}) |",
             f"| verdict accuracy, gold rules | {_f(g['accuracy'])} ({sum(g[x]['support'] for x in LABELS)}) |"]
    for label in LABELS:
        lines.append(f"| {label} precision / recall (gold rules) | {_f(g[label]['precision'])} / "
                     f"{_f(g[label]['recall'])} (support {g[label]['support']}) |")
    lines += [f"| unknown reported as pass | {b['unknown_reported_as_pass']} |",
              f"| verdict accuracy, end-to-end | {_f(b['verdict_end_to_end']['accuracy'])} |",
              f"| evidence exact-set / mean Jaccard | {_f(e['exact_set_match'])} / {_f(e['mean_jaccard'])} |",
              f"| first-violation match | {_f(e['first_violation_match'])} ({e['n_first_violation']}) |",
              f"| unsupported claims / decoy citations | {b['explanations']['unsupported_claims']} of "
              f"{b['explanations']['claims']} / {b['explanations']['decoy_citations']} |"]
    for stage, v in b["latency_ms"].items():
        lines.append(f"| {stage} latency p50 / p95 ms | {_f(v['p50'])} / {_f(v['p95'])} |")
    misses = [r for r in b["per_case"] if not r["compile_ok"] or r.get("evidence_exact") is False
              or r.get("verdict", r.get("expected")) != r.get("expected")]
    pmiss = [r for r in b["paraphrase_rows"] if r["status"] != r["expected_status"] or
             (r["expected_status"] == "compiled" and not r["rule_ok"])]
    lines += ["", "Misses: " + (", ".join(r["id"] for r in misses + pmiss) or "none")]
    for r in pmiss:
        got = r["status"] if r["status"] != "compiled" else f"compiled {_semantic(r['rule'])}"
        lines.append(f"- {r['id']}: expected {r['expected_status']}, got {got} {r['issues'] or ''}".rstrip())
    return "\n".join(lines) + "\n"


def write_benchmark(b: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    jp, mp = out_dir / "benchmark.json", out_dir / "benchmark.md"
    jp.write_text(json.dumps(b, indent=2))
    mp.write_text(to_markdown(b))
    return jp, mp
