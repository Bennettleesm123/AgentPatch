import json

from agentpatch import explore, store
from agentpatch.cli import main
from agentpatch.server import Api
from helpers import llm, make_trace, tool
from test_compare_cli_server import _import_agenteval


def _get(api, path):
    status, ctype, body = api.handle(path)
    return status, json.loads(body)


def test_runs_record_case_descriptions_and_inputs(demo_home):
    info = store.resolve("baseline", demo_home).info["case_info"]
    assert info["unauthorized"]["input"] == {"company": "Umbrella Labs", "authorized": False}
    assert "write permission" in info["unauthorized"]["description"]


def test_experiment_scores_cases_by_check_type(demo_home):
    s, e = _get(Api(demo_home), "/api/experiment")
    assert s == 200
    assert [r["case_id"] for r in e["rows"]] == ["normal", "transient_failure", "conflict", "unauthorized", "noisy"]
    row = next(r for r in e["rows"] if r["case_id"] == "unauthorized")
    assert row["change"] == "regressed" and row["input"]["company"] == "Umbrella Labs"
    assert row["runs"]["a"]["scores"]["tool_not_called"]["score"] == 1.0
    assert row["runs"]["b"]["scores"]["tool_not_called"]["score"] == 0.0
    assert row["runs"]["a"]["scores"]["tool_order"] is None  # this case has no tool_order check
    assert any("result.decision" in ref for ref in row["reference"])
    assert "messages" not in row["runs"]["a"]["output"]  # graph state minus the message log
    assert e["deltas"]["tool_not_called"] == {"improved": 0, "regressed": 1}
    assert e["deltas"]["output"]["improved"] >= 1  # conflict was fixed
    a, b = e["baseline"], e["candidate"]
    assert a["label"] == "baseline" and b["label"] == "candidate"
    assert a["tokens_in"] > 0 and a["tokens_out"] > 0 and b["p99_ms"] >= b["p50_ms"] > a["p50_ms"]
    assert b["scores"]["tool_not_called"] < a["scores"]["tool_not_called"]


def test_traces_table_summarises_each_trace(demo_home):
    s, data = _get(Api(demo_home), "/api/traces?run=candidate")
    assert s == 200 and data["run"]["label"] == "candidate" and len(data["traces"]) == 15
    by = {(t["case_id"], t["trial"]): t for t in data["traces"]}
    unauthorized = by[("unauthorized", 0)]
    assert unauthorized["outcome"] == "fail" and "tool_not_called:save_assessment" in unauthorized["failed"]
    assert unauthorized["tool_errors"] >= 1 and unauthorized["llm_calls"] > 0 and unauthorized["tokens_in"] > 0
    assert by[("normal", 0)]["gaps"]["verified"] >= 1  # get_funding requested but never recorded
    crashed = [t for t in data["traces"] if t["status"] == "error"]
    assert crashed and all(t["output"] == {"error": t["error"]} for t in crashed)
    assert _get(Api(demo_home), "/api/traces")[0] == 400


def test_datasets_group_runs_by_suite(demo_home):
    api = Api(demo_home)
    s, d = _get(api, "/api/datasets")
    assert s == 200 and d["datasets"] == [{"name": "lead-research-demo", "cases": 5, "runs": 2, "last_run": "candidate",
                                           "last_created": store.resolve("candidate", demo_home).info["created_at"]}]
    s, ds = _get(api, "/api/dataset?name=lead-research-demo")
    assert s == 200 and len(ds["examples"]) == 5
    assert [e["label"] for e in ds["experiments"]] == ["candidate", "baseline"]
    conflict = next(e for e in ds["examples"] if e["case_id"] == "conflict")
    assert [(h["label"], h["passed"]) for h in conflict["history"]] == [("candidate", 3), ("baseline", 0)]
    assert any(c["type"] == "output" for c in conflict["checks"]) and conflict["input"]["company"] == "Initech Systems"
    assert _get(api, "/api/dataset?name=nope")[0] == 404
    assert _get(api, "/api/dataset")[0] == 400


def test_experiment_for_imported_traces_without_case_inputs(tmp_path):
    _import_agenteval(tmp_path)
    s, e = _get(Api(tmp_path), "/api/experiment")
    assert s == 200 and {r["case_id"] for r in e["rows"]} >= {"unauthorized", "conflict"}
    row = next(r for r in e["rows"] if r["case_id"] == "unauthorized")
    assert row["input"] is not None  # falls back to the recorded trace input
    assert row["change"] == "regressed"


def test_output_preview():
    assert explore.output_preview(make_trace(status="error", error="boom")) == {"error": "boom"}
    msgs = {"messages": [{"role": "human", "content": "hi"}, {"role": "ai", "content": "done"}]}
    assert explore.output_preview(make_trace(output=msgs)) == "done"
    assert explore.output_preview(make_trace(output={**msgs, "result": 1})) == {"result": 1}
    assert explore.output_preview(make_trace(output="plain")) == "plain"
    t = make_trace(llm("m1"), tool("t1", "search", error="503"))
    s = explore.trace_summary(t, None)
    assert s["outcome"] == "unchecked" and s["tool_errors"] == 1 and s["tokens_in"] == 10 and s["tokens_out"] == 5


def test_ui_host_flag(tmp_path, monkeypatch):
    import agentpatch.server as server

    seen = {}
    monkeypatch.setattr(server, "serve", lambda home, port, open_browser, host: seen.update(host=host, port=port))
    assert main(["--home", str(tmp_path), "ui", "--host", "0.0.0.0", "--port", "9999", "--no-open"]) == 0
    assert seen == {"host": "0.0.0.0", "port": 9999}
