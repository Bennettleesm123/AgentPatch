from agenteval.tracing import load_trace

from test_agent import run
from agenteval.configs import CANDIDATE


def by_kind(trace, kind):
    return [s for s in trace["spans"] if s["kind"] == kind]


def test_trace_structure(out_dir):
    r = run("normal", out_dir=out_dir)
    t = load_trace(r["trace_path"])
    assert t["schema"] == "agenteval.trace.v1" and t["case_id"] == "normal"
    (root,) = [s for s in t["spans"] if s["parent_id"] is None]
    assert root["kind"] == "agent" and root["output"]["final"]["decision"] == "qualified"
    assert root["usage"]["total_tokens"] > 0
    llms, tools = by_kind(t, "llm"), by_kind(t, "tool")
    assert len(llms) == 4 and len(tools) == 3
    assert all(s["parent_id"] == root["span_id"] for s in llms + tools)
    assert [s["attributes"]["gen_ai.tool.name"] for s in tools] == ["search_company", "get_company_details",
                                                                  "save_assessment"]
    assert tools[0]["input"] == {"name": "Acme Robotics"} and tools[0]["output"]["results"]
    assert llms[0]["attributes"]["gen_ai.request.model"] == "scripted-v1"
    assert llms[0]["usage"]["estimated"] is True
    for s in t["spans"]:
        assert s["end_ns"] >= s["start_ns"] and s["latency_ms"] >= 0
        assert s["start_time"].endswith("+00:00")
    assert len({s["span_id"] for s in t["spans"]}) == len(t["spans"])


def test_retry_attempts_are_nested_with_errors(out_dir):
    t = load_trace(run("transient_failure", out_dir=out_dir)["trace_path"])
    details = [s for s in by_kind(t, "tool") if s["name"] == "tool.get_company_details"][0]
    attempts = [s for s in t["spans"] if s["parent_id"] == details["span_id"]]
    assert [a["status"] for a in attempts] == ["error", "error", "ok"]
    assert "timed out" in attempts[0]["error"] and "503" in attempts[1]["error"]
    assert details["attributes"]["agenteval.retries"] == 2 and details["status"] == "ok"


def test_denied_write_is_error_span(out_dir):
    t = load_trace(run("unauthorized", CANDIDATE, out_dir=out_dir)["trace_path"])
    save = [s for s in by_kind(t, "tool") if s["name"] == "tool.save_assessment"][0]
    assert save["status"] == "error" and "UnauthorizedError" in save["error"]
