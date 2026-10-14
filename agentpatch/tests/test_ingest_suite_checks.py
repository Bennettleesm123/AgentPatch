import json

import pytest

from agentpatch.checks import evaluate_trace, run_check, validate_check
from agentpatch.coverage import unrecorded_counts
from agentpatch.ingest import detect, load_file
from agentpatch.suite import SuiteError, load_suite, parse_suite
from conftest import FIXTURES
from helpers import S, llm, make_trace, tool

AGENTEVAL = FIXTURES / "agenteval"


# ---------- ingest ----------

def test_agenteval_trace_import_keeps_tools_errors_and_case():
    [t] = load_file(AGENTEVAL / "candidate__unauthorized__offline.json")
    assert t.case_id == "unauthorized" and t.source == "agenteval"
    saves = t.tool_steps("save_assessment")
    assert saves and saves[0].status == "error" and "403" in saves[0].error
    assert t.of_kind("llm") and all(s.attributes.get("usage") for s in t.of_kind("llm"))
    assert t.output and "decision" in t.output


def test_otlp_import_maps_genai_spans():
    [t] = load_file(FIXTURES / "otlp_trace.json")
    assert t.case_id == "refund" and t.source == "otlp"
    assert t.input == {"question": "refund please"} and t.output == {"answer": "done"}
    [m] = t.of_kind("llm")
    assert m.name == "gpt-4o-mini" and m.attributes["usage"]["total_tokens"] == 150
    [refund] = t.tool_steps("issue_refund")
    assert refund.status == "error" and "card declined" in refund.error
    assert round(t.duration_ms) == 900
    orphan = next(s for s in t.steps if s.name == "lost_child")
    assert orphan.parent_id.startswith("missing:")


def test_native_trace_file_and_detection(tmp_path):
    t = make_trace(S("a", "node", "plan"))
    p = tmp_path / "x.json"
    p.write_text(json.dumps(t.to_dict()))
    assert detect(json.loads(p.read_text())) == "agentpatch"
    assert load_file(p)[0].to_dict() == t.to_dict()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"hello": 1}))
    with pytest.raises(ValueError):
        load_file(bad)


# ---------- suite ----------

def test_demo_suite_loads():
    from agentpatch.demo import SUITE_PATH

    s = load_suite(SUITE_PATH)
    assert [c.id for c in s.cases] == ["normal", "transient_failure", "conflict", "unauthorized", "noisy"]
    assert s.repeats == 3 and "save_assessment" in s.referenced_tools()
    assert s.case("normal").checks[0]["type"] == "completes"  # defaults come first


@pytest.mark.parametrize("data, msg", [
    ({"cases": []}, "no [[cases]]"),
    ({"bogus": 1, "cases": [{"id": "a"}]}, "unknown top-level"),
    ({"agent": {"repeats": 0}, "cases": [{"id": "a"}]}, "repeats"),
    ({"cases": [{"id": "a"}, {"id": "a"}]}, "duplicate case"),
    ({"cases": [{"input": 1}]}, "needs a string `id`"),
    ({"cases": [{"id": "a", "checks": [{"type": "nope"}]}]}, "unknown check type"),
    ({"cases": [{"id": "a", "checks": [{"type": "output", "path": "x"}]}]}, "exactly one of"),
    ({"cases": [{"id": "a", "checks": [{"type": "tool_called"}]}]}, "needs ['tool']"),
    ({"cases": [{"id": "a", "checks": [{"type": "completes", "tool": "x"}]}]}, "doesn't take"),
    ({"cases": [{"id": "a"}], "thresholds": {"speed": 1}}, "threshold"),
    ({"cases": [{"id": "a"}], "coverage": {"tools": []}}, "coverage keys"),
    ({"cases": [{"id": "a", "checks": [{"type": "completes"}, {"type": "completes"}]}]}, "duplicate checks"),
])
def test_suite_validation_errors_are_specific(data, msg):
    with pytest.raises(SuiteError, match=None) as exc:
        parse_suite(data)
    assert msg in str(exc.value)


def test_suite_file_errors(tmp_path):
    with pytest.raises(SuiteError, match="not found"):
        load_suite(tmp_path / "missing.toml")
    p = tmp_path / "bad.toml"
    p.write_text("name = [")
    with pytest.raises(SuiteError, match="invalid TOML"):
        load_suite(p)


def test_import_only_cases_cant_be_run():
    from agentpatch.runner import run_suite

    s = parse_suite({"agent": {"target": "json:dumps"}, "cases": [{"id": "a"}]})
    with pytest.raises(SuiteError, match="no `input`"):
        run_suite(s, "x")


# ---------- checks ----------

def _t(**kw):
    return make_trace(
        llm("l1", ["search"], start=0, end=1), tool("t1", "search", start=1, end=2),
        llm("l2", ["save"], start=2, end=3), tool("t2", "save", start=3, end=4, error="HTTP 503"),
        tool("t3", "save", start=4, end=5), llm("l3", start=5, end=6),
        output={"result": {"decision": "qualified", "tags": ["a", "b"]}, "items": [{"v": 1}]}, duration=6, **kw)


def _run(ch, t=None, unrecorded=None):
    return run_check(validate_check(ch), t or _t(), unrecorded)


@pytest.mark.parametrize("ch, passed", [
    ({"type": "completes"}, True),
    ({"type": "output", "path": "result.decision", "equals": "qualified"}, True),
    ({"type": "output", "path": "result.decision", "not_equals": "qualified"}, False),
    ({"type": "output", "path": "result.tags", "contains": "b"}, True),
    ({"type": "output", "path": "result.decision", "one_of": ["x", "y"]}, False),
    ({"type": "output", "path": "items.0.v", "equals": 1}, True),
    ({"type": "output", "path": "result.missing", "exists": False}, True),
    ({"type": "output", "path": "result.missing", "equals": 1}, False),
    ({"type": "tool_called", "tool": "save", "min": 2}, True),
    ({"type": "tool_called", "tool": "save", "max": 1}, False),
    ({"type": "tool_not_called", "tool": "delete"}, True),
    ({"type": "tool_not_called", "tool": "save"}, False),
    ({"type": "tool_order", "sequence": ["search", "save"]}, True),
    ({"type": "tool_order", "sequence": ["save", "search"]}, False),
    ({"type": "no_tool_errors"}, False),
    ({"type": "max_latency_ms", "value": 10}, True),
    ({"type": "max_latency_ms", "value": 5}, False),
    ({"type": "max_tool_calls", "value": 2}, False),
    ({"type": "max_tool_calls", "value": 1, "tool": "search"}, True),
    ({"type": "max_llm_calls", "value": 3}, True),
])
def test_check_outcomes(ch, passed):
    assert _run(ch)["passed"] is passed


def test_failed_checks_cite_the_offending_steps():
    assert _run({"type": "tool_not_called", "tool": "save"})["evidence"] == ["tool:save#1", "tool:save#2"]
    assert _run({"type": "no_tool_errors"})["evidence"] == ["tool:save#1"]
    assert _run({"type": "tool_order", "sequence": ["save", "search"]})["evidence"] == ["tool:search#1", "tool:save#1"]


def test_failed_run_explains_missing_output():
    t = make_trace(S("n", "node", "plan", status="error", error="KeyError: x"), status="error", error="KeyError: x")
    r = _run({"type": "output", "path": "a", "equals": 1}, t)
    assert r["passed"] is False and "run failed" in r["detail"]
    c = _run({"type": "completes"}, t)
    assert c["passed"] is False and c["evidence"][0] == "node:plan#1"


def test_tool_check_is_unknown_when_the_call_was_requested_but_not_recorded():
    # The model asked for `save` but no tool step exists: we can't claim it wasn't called.
    t = make_trace(llm("l1", ["save"], start=0, end=1), llm("l2", start=2, end=3))
    un = unrecorded_counts(t)
    assert un == {"save": 1}
    r = _run({"type": "tool_not_called", "tool": "save"}, t, un)
    assert r["passed"] is None and "can't verify" in r["detail"]
    assert _run({"type": "tool_called", "tool": "save"}, t, un)["passed"] is None
    res = evaluate_trace(t, [validate_check({"type": "completes"}), validate_check({"type": "tool_called", "tool": "save"})], un)
    assert res["outcome"] == "unknown"


def test_check_ids_are_stable_and_overridable():
    from agentpatch.checks import check_id

    assert check_id({"type": "output", "path": "a.b", "equals": 1}) == "output:a.b:equals"
    assert check_id({"type": "tool_not_called", "tool": "x"}) == "tool_not_called:x"
    assert check_id({"type": "completes", "id": "mine"}) == "mine"
