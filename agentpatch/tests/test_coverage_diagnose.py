from agentpatch.checks import evaluate_trace, validate_check
from agentpatch.coverage import drift, run_coverage, trace_gaps
from agentpatch.diagnose import align, diagnose, error_hint
from helpers import S, llm, make_trace, tool


def codes(gaps, level=None):
    return sorted({g["code"] for g in gaps if level is None or g["level"] == level})


# ---------- coverage ----------

def test_clean_trace_has_no_gaps():
    t = make_trace(S("n", "node", "plan", start=0, end=3), llm("l", ["search"], "n", 0, 1), tool("t", "search", "n", 1, 2))
    assert trace_gaps(t) == []


def test_verified_gaps():
    t = make_trace(llm("l1", ["search"], start=0, end=1), llm("l2", start=2, end=3),
                   S("o", "tool", "x", parent="ghost", start=3, end=4, input=1, output=1),
                   S("u", "node", "stuck", start=4, end=None),
                   S("e", "tool", "y", start=5, end=6, status="error", error="", input=1))
    assert codes(trace_gaps(t), "verified") == ["error_without_message", "orphan_step", "unclosed_step",
                                                 "unrecorded_tool_call"]


def test_requested_tool_followed_by_a_crash_is_not_a_gap():
    t = make_trace(llm("l1", ["search"], start=0, end=1),
                   S("n", "node", "tools", start=1, end=2, status="error", error="boom"),
                   status="error", error="boom")
    assert "unrecorded_tool_call" not in codes(trace_gaps(t))


def test_untraced_time_is_only_possible_and_threshold_based():
    slow = make_trace(S("n", "node", "enrich", start=0, end=300), duration=300)
    gaps = trace_gaps(slow)
    assert codes(gaps) == ["untraced_time"] and gaps[0]["level"] == "possible"
    covered = make_trace(S("n", "node", "enrich", start=0, end=300), tool("t", "f", "n", 5, 290), duration=300)
    assert trace_gaps(covered) == []
    assert trace_gaps(slow, {"untraced_min_ms": 500}) == []


def test_empty_trace_and_capture_hints():
    assert codes(trace_gaps(make_trace())) == ["empty_trace"]
    t = make_trace(llm("l", usage=False, start=0, end=1), S("t", "tool", "f", start=1, end=2, input=None, output=None))
    assert codes(trace_gaps(t), "info") == ["llm_usage_missing", "tool_io_missing"]


def test_run_level_gaps_from_graph_checks_and_expectations():
    t = make_trace(S("n", "node", "plan"), tool("t", "search", "n"))
    cov = run_coverage([t], {"nodes": ["plan", "review"]}, {"search", "save"},
                       {"expected_tools": ["search", "fetch"], "expected_nodes": ["review"]})
    found = {(g["code"], g["level"]) for g in cov["gaps"]}
    assert ("declared_node_not_observed", "possible") in found
    assert ("check_references_unseen_tool", "possible") in found
    assert ("expected_tool_missing", "verified") in found and ("expected_node_missing", "verified") in found
    item = next(i for i in cov["summary"]["items"] if i["code"] == "expected_tool_missing")
    assert "`fetch`" in item["examples"][0]["message"]


def test_drift_detects_lost_tools_nodes_and_capture():
    base = make_trace(S("p", "node", "plan", start=0, end=1), llm("l", ["search"], "p", 0, 1),
                      S("s", "node", "tools", start=1, end=3), tool("t", "search", "s", 1, 2),
                      S("r", "node", "review", start=3, end=4))
    cand = make_trace(S("p", "node", "plan", start=0, end=1), llm("l", ["search"], "p", 0, 1, usage=False),
                      S("s", "node", "tools", start=1, end=3))
    graph = {"nodes": ["plan", "tools", "review"]}
    b, c = run_coverage([base], graph), run_coverage([cand], graph)
    d = {x["code"]: x for x in drift(b, c)}
    assert "node_no_longer_observed" in d and d["node_no_longer_observed"]["node"] == "review"
    assert "tool_capture_lost" in d and "new_verified_gap" in d
    assert d["capture_rate_dropped"]["metric"] == "llm_usage"
    assert "tool_removed" not in d  # still requested by the model, so it's capture loss, not removal
    assert "flow_changed" in d


def test_drift_flags_new_unchecked_tools():
    b = run_coverage([make_trace(tool("t", "search"))], None)
    c = run_coverage([make_trace(tool("t", "search", start=0, end=1), tool("u", "delete", start=1, end=2))], None)
    d = {x["code"]: x for x in drift(b, c, suite_tools={"search"})}
    assert d["tool_added"]["severity"] == "warning" and "isn't covered" in d["tool_added"]["message"]


# ---------- diagnosis ----------

def _check(*chs):
    return [validate_check(c) for c in chs]


def test_failure_point_comes_from_failed_check_evidence():
    base = make_trace(llm("l1", ["search"], start=0, end=1), tool("t", "search", start=1, end=2), llm("l2", start=2, end=3))
    cand = make_trace(llm("l1", ["search"], start=0, end=1), tool("t", "search", start=1, end=2),
                      llm("l2", ["delete"], start=2, end=3), tool("d", "delete", start=3, end=4), llm("l3", start=4, end=5))
    checks = _check({"type": "tool_not_called", "tool": "delete"})
    d = diagnose(cand, evaluate_trace(cand, checks), base, evaluate_trace(base, checks))
    lp = d["likely_failure_point"]
    assert lp["step"]["id"] == "tool:delete#1" and lp["confidence"] == "evidence"
    assert d["decision_step"]["id"] == "llm:m#2" and d["decision_step"]["requested_tools"] == ["delete"]
    texts = " ".join(f["text"] for f in d["facts"])
    assert "Baseline made no `delete` call" in texts
    assert any(c.startswith("baseline:") for f in d["facts"] for c in f["cites"])
    assert any("guard" in s for s in d["next_steps"])


def test_failure_point_is_the_fatal_error_origin():
    cand = make_trace(S("n", "node", "enrich", start=0, end=2, status="error", error="RateLimited: 429 Too Many Requests"),
                      status="error", error="RateLimited: 429 Too Many Requests")
    res = evaluate_trace(cand, _check({"type": "completes"}))
    d = diagnose(cand, res)
    assert d["likely_failure_point"]["step"]["id"] == "node:enrich#1"
    assert d["likely_failure_point"]["error_hint"] == "rate limit / HTTP 429"
    assert any("no recorded child steps" in s for s in d["next_steps"])


def test_output_failure_without_evidence_step_falls_back_to_last_llm():
    cand = make_trace(llm("l1", start=0, end=1), llm("l2", start=1, end=2), output={"a": 2})
    base = make_trace(llm("l1", start=0, end=1), llm("l2", start=1, end=2), output={"a": 1})
    checks = _check({"type": "output", "path": "a", "equals": 1})
    d = diagnose(cand, evaluate_trace(cand, checks), base, evaluate_trace(base, checks))
    assert d["likely_failure_point"]["step"]["id"] == "llm:m#2"
    assert d["likely_failure_point"]["confidence"] == "heuristic"
    texts = " ".join(f["text"] for f in d["facts"])
    assert "same steps" in texts and "baseline 1, candidate 2" in texts


def test_alignment_rows_pair_steps_and_mark_inserts():
    b = make_trace(llm("l1", ["a"], start=0, end=1), tool("t", "a", start=1, end=2))
    c = make_trace(llm("l1", ["a"], start=0, end=1), S("e", "node", "enrich", start=1, end=2), tool("t", "a", start=2, end=3))
    al = align(b, c)
    assert [r["tag"] for r in al["rows"]] == ["equal", "insert", "equal"]
    assert al["rows"][1] == {"tag": "insert", "baseline": None, "candidate": "node:enrich#1"}
    assert not al["identical"]


def test_error_hints():
    assert error_hint("HTTP 403 Forbidden") == "permission denied / HTTP 403"
    assert error_hint("Request timed out after 30s") == "timeout"
    assert error_hint("503 Service Unavailable") == "upstream server error / 5xx"
    assert error_hint("all good") is None
