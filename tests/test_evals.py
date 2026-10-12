from agenteval.agent import Limits, run_case
from agenteval.configs import BASELINE, CANDIDATE
from agenteval.data import CASES
from agenteval.evals import compare_reports, evaluate_record, evaluate_records
from agenteval.models import ScriptedModel


def run_all(config, out_dir):
    return [run_case(c, config, ScriptedModel(config.policy), "offline", Limits(), out_dir=out_dir) for c in CASES]


def failing(report):
    return {(cid, n) for cid, c in report["cases"].items() for n, e in c["evals"].items() if not e["passed"]}


def test_baseline_fails_only_conflict(out_dir):
    rep = evaluate_records(run_all(BASELINE, out_dir))
    assert failing(rep) == {("conflict", "decision_correct"), ("conflict", "conflict_handling")}


def test_candidate_fails_only_unauthorized(out_dir):
    rep = evaluate_records(run_all(CANDIDATE, out_dir))
    assert failing(rep) == {("unauthorized", "tool_sequence"), ("unauthorized", "no_unauthorized_writes")}


def test_regression_detected(out_dir):
    cmp = compare_reports(evaluate_records(run_all(BASELINE, out_dir)), evaluate_records(run_all(CANDIDATE, out_dir)))
    assert cmp["verdict"] == "regression"
    assert set(cmp["fixed"]) == {"conflict/decision_correct", "conflict/conflict_handling"}
    assert set(cmp["regressed"]) == {"unauthorized/tool_sequence", "unauthorized/no_unauthorized_writes"}


def test_retry_limit_evaluator_flags_exhausted_retries(out_dir):
    case = [c for c in CASES if c.id == "transient_failure"][0]
    rec = run_case(case, BASELINE, ScriptedModel(BASELINE.policy), "offline", Limits(max_retries=1), out_dir=out_dir)
    res = evaluate_record(rec)
    assert not res["evals"]["retry_limits"]["passed"]
    assert not res["evals"]["decision_correct"]["passed"]


def test_tampered_record_violates_retry_limit(out_dir):
    case = CASES[0]
    rec = run_case(case, BASELINE, ScriptedModel(BASELINE.policy), "offline", Limits(), out_dir=out_dir)
    rec["tool_calls"][0]["attempts"] = 9
    assert not evaluate_record(rec)["evals"]["retry_limits"]["passed"]
