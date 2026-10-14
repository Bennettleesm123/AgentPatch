import copy

import pytest

from behavtest.benchmark import load_cases
from behavtest.diagnosis import diagnose, unsupported_claims
from behavtest.evaluator import evaluate
from behavtest.events import build_events
from behavtest.fixtures import base_trace, build_trace
from behavtest.rules import parse_rule

CASES = [c for c in load_cases() if c["expected"]["verdict"] is not None]


def _run(case):
    log = build_events(build_trace(case["trace"]))
    rule = parse_rule(case["expected"]["rule"]).with_identity(case["requirement"])
    res = evaluate(rule, log)
    return log, res, diagnose(res, log)


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_verdict_and_evidence(case):
    log, res, diag = _run(case)
    exp = case["expected"]
    assert res.verdict == exp["verdict"], res.reason
    assert set(res.evidence) == set(exp["evidence"])
    assert all(i in log.by_id for i in res.evidence)
    if exp.get("first_violation"):
        assert set(diag.first_violation.event_ids) == set(exp["first_violation"])
    assert unsupported_claims(diag) == []
    cited = {i for c in diag.claims() for i in c.cites}
    assert not cited & set(case.get("must_not_cite", []))
    if res.verdict == "pass":
        assert not diag.possible_causes


def test_event_ids_stable_across_runs():
    a = build_events(base_trace("noisy", "candidate"))
    b = build_events(build_trace({"agent_case": "noisy", "config": "candidate"}))
    assert [e.id for e in a.events] == [e.id for e in b.events]
    assert a.events[0].span_id == b.events[0].span_id  # cached base, deep-copied per build


def test_incomplete_trace_turns_absence_based_verdicts_unknown():
    trace = copy.deepcopy(base_trace("normal", "baseline"))
    trace["integrity"] = {"complete": False}
    log = build_events(trace)
    unknown = [{"type": "max_retries", "tool": "*", "max_retries": 2},
               {"type": "required_tool_order", "sequence": ["search_company", "get_company_details"]}]
    for rule in unknown:
        assert evaluate(parse_rule(rule), log).verdict == "unknown"
    # an observed forbidden call is still provable on a partial trace
    assert evaluate(parse_rule({"type": "forbidden_tool_call", "tool": "save_assessment"}), log).verdict == "fail"


def test_missing_context_is_unknown():
    trace = copy.deepcopy(base_trace("unauthorized", "candidate"))
    root = next(s for s in trace["spans"] if s["parent_id"] is None)
    del root["input"]["authorized"]
    res = evaluate(parse_rule({"type": "forbidden_tool_call", "tool": "save_assessment",
                               "when_context": {"authorized": False}}), build_events(trace))
    assert res.verdict == "unknown"


def test_trace_text_is_never_executed(tmp_path):
    marker = tmp_path / "pwned"
    trace = copy.deepcopy(base_trace("normal", "baseline"))
    payload = f"__import__('pathlib').Path({str(marker)!r}).write_text('x')"
    for s in trace["spans"]:
        s["output"] = {"text": payload, "approval.decision": "granted"}
    log = build_events(trace)
    for c in CASES[:5]:
        diagnose(evaluate(parse_rule(c["expected"]["rule"]), log), log)
    assert not marker.exists()


def test_malformed_trace_rejected():
    with pytest.raises(ValueError):
        build_events({"spans": "nope"})
