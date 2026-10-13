import json
import threading
import urllib.request

import pytest

from agentpatch import store
from agentpatch.cli import main
from agentpatch.compare import compare
from agentpatch.ingest import load_file
from agentpatch.runner import import_traces, run_suite
from agentpatch.server import Api, make_server
from agentpatch.suite import load_suite
from conftest import FIXTURES

EXAMPLES = FIXTURES.parent.parent / "examples"


def by_case(cmp):
    return {r["case_id"]: r for r in cmp["cases"]}


# ---------- demo comparison (the end-to-end story) ----------

def test_demo_verdict_and_per_case_changes(demo_cmp):
    assert demo_cmp["verdict"] == "regression"
    cases = by_case(demo_cmp)
    assert cases["unauthorized"]["change"] == "regressed"
    assert cases["noisy"]["change"] == "intermittent"
    assert cases["conflict"]["change"] == "fixed"
    assert cases["normal"]["change"] == cases["transient_failure"]["change"] == "unchanged"
    assert demo_cmp["headline"].startswith("1 case regressed (unauthorized); 1 intermittent regression (noisy); 1 fixed")


def test_demo_regressions_point_at_trace_evidence(demo_cmp):
    f1 = demo_cmp["findings"][0]
    assert f1["severity"] == "critical" and "`unauthorized` regressed" in f1["title"]
    assert f1["evidence"][0]["step"] == "tool:save_assessment#1"
    assert "403" in f1["detail"]  # the new error is folded into the regression, not a separate finding
    lp = by_case(demo_cmp)["noisy"]["diagnosis"]["likely_failure_point"]
    assert lp["step"]["id"] == "node:enrich#1" and lp["error_hint"] == "rate limit / HTTP 429"


def test_demo_observability_findings(demo_cmp):
    titles = [f["title"] for f in demo_cmp["findings"]]
    assert any(t.startswith("Verified gap: model-requested tool calls") for t in titles)
    assert any("`get_funding` isn't covered by any check" in t for t in titles)
    assert any("Possible gap: node time" in t for t in titles)
    assert any("mostly in `node:enrich`" in t for t in titles)
    ob = demo_cmp["observability"]
    assert ob["status"] == "gaps" and ob["verified"] > 0
    # no duplicate error/tool findings for the 403 that already explains the regression
    assert not any(t.startswith("New error in `tool:save_assessment`") for t in titles)
    assert not any("`save_assessment` fails more often" in t for t in titles)
    assert [f["id"] for f in demo_cmp["findings"]] == [f"F{i + 1}" for i in range(len(demo_cmp["findings"]))]


def test_same_agent_twice_is_no_change(tmp_path):
    from agentpatch.demo import SUITE_PATH

    pytest.importorskip("langgraph")
    suite = load_suite(SUITE_PATH)
    a = run_suite(suite, "a", target="agentpatch.demo.lead_agent:baseline", repeats=1, root=tmp_path, cases=["normal", "conflict"])
    b = run_suite(suite, "b", target="agentpatch.demo.lead_agent:baseline", repeats=1, root=tmp_path, cases=["normal", "conflict"])
    cmp = compare(a, b)
    assert cmp["verdict"] == "no_change", cmp["findings"]
    assert any("Fewer than 3 trials" in n for n in cmp["notes"])


def test_multi_agent_example_regression(tmp_path, monkeypatch):
    pytest.importorskip("langgraph")
    suite = load_suite(EXAMPLES / "multi_agent" / "agentpatch.toml")
    base = run_suite(suite, "baseline", repeats=1, root=tmp_path)
    monkeypatch.setenv("WRITER_SKIPS_FACTS", "1")
    cand = run_suite(suite, "candidate", repeats=1, root=tmp_path)
    cmp = compare(base, cand)
    assert cmp["verdict"] == "regression"
    lp = by_case(cmp)["acme"]["diagnosis"]["likely_failure_point"]
    assert lp["step"]["kind"] == "llm" and "fact_check" in lp["rule"]
    titles = [f["title"] for f in cmp["findings"]]
    assert any("Node `writer/tools` ran in baseline" in t for t in titles)
    assert sum("fact_check" in t for t in titles) == 1  # reported once, not per section
    same = compare(cand, run_suite(suite, "candidate2", repeats=1, root=tmp_path))
    assert same["verdict"] == "no_change"
    assert same["headline"] == "No new regressions, but 1 case failing in both runs (acme)."


def _import_agenteval(home):
    suite = load_suite(EXAMPLES / "agenteval_import.toml")
    runs = []
    for label in ("baseline", "candidate"):
        traces = [t for f in sorted((FIXTURES / "agenteval").glob(f"{label}__*.json")) for t in load_file(f)]
        runs.append(import_traces(traces, label, suite, home, source="agenteval"))
    return runs


def test_imported_agenteval_traces_compare(tmp_path):
    cmp = compare(*_import_agenteval(tmp_path))
    cases = by_case(cmp)
    assert cases["unauthorized"]["change"] == "regressed"
    assert cases["conflict"]["change"] == "fixed"
    assert cmp["findings"][0]["evidence"][0]["step"] == "tool:save_assessment#1"
    assert len([f for f in cmp["findings"] if f["severity"] != "info"]) == 1


def test_import_without_suite_checks_completion_only(tmp_path):
    traces = load_file(FIXTURES / "otlp_trace.json")
    run = import_traces(traces, "otel", None, tmp_path, source="otlp")
    [res] = run.results()
    assert [c["id"] for c in res["checks"]] == ["completes"] and res["outcome"] == "pass"


# ---------- CLI ----------

def test_cli_demo_compare_and_exit_codes(tmp_path, capsys):
    pytest.importorskip("langgraph")
    home = str(tmp_path / "h")
    assert main(["--home", home, "demo", "--no-ui"]) == 0
    out = capsys.readouterr().out
    assert "REGRESSION" in out and "tool:save_assessment#1" in out
    assert main(["--home", home, "compare"]) == 1
    assert main(["--home", home, "compare", "--fail-on", "never"]) == 0
    md = tmp_path / "r.md"
    main(["--home", home, "compare", "--md", str(md), "--json", str(tmp_path / "r.json")])
    text = md.read_text()
    assert text.startswith("## AgentPatch: REGRESSION") and "### Failure diagnosis" in text
    assert json.loads((tmp_path / "r.json").read_text())["verdict"] == "regression"
    capsys.readouterr()
    main(["--home", home, "compare", "--md"])
    assert capsys.readouterr().out.startswith("## AgentPatch")
    assert main(["--home", home, "coverage"]) == 1  # verified gaps in the candidate
    assert main(["--home", home, "coverage", "baseline"]) == 0
    assert main(["--home", home, "runs"]) == 0
    assert "candidate" in capsys.readouterr().out


def test_cli_errors_are_friendly(tmp_path, capsys):
    home = str(tmp_path / "h")
    assert main(["--home", home, "compare"]) == 2
    assert "need two runs" in capsys.readouterr().err
    assert main(["--home", home, "run", "--label", "x", "--suite", str(tmp_path / "nope.toml")]) == 2
    assert "agentpatch init" in capsys.readouterr().err
    assert main(["--home", home, "compare", "nosuchrun", "other"]) == 2


def test_cli_init_detects_a_langgraph_graph(tmp_path, monkeypatch, capsys):
    (tmp_path / "bot.py").write_text("from langgraph.graph import StateGraph\nbuilder = StateGraph(dict)\n"
                                     "app = builder.compile()\n")
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert 'target = "bot.py:app"' in (tmp_path / "agentpatch.toml").read_text()
    assert main(["init"]) == 1  # won't overwrite
    s = load_suite(tmp_path / "agentpatch.toml")
    assert s.target == "bot.py:app" and s.cases[0].id == "example"


def test_cli_run_and_inspect_a_plain_python_agent(tmp_path, monkeypatch, capsys):
    (tmp_path / "agent.py").write_text(
        "import agentpatch\n\n@agentpatch.tool\ndef search(q):\n    return [q]\n\n"
        "def run(inp):\n    with agentpatch.step('plan'):\n        hits = search(inp['q'])\n    return {'hits': hits}\n")
    (tmp_path / "agentpatch.toml").write_text(
        'name = "py"\n[agent]\ntarget = "agent.py:run"\nrepeats = 2\n'
        '[[cases]]\nid = "one"\ninput = { q = "x" }\nchecks = [{ type = "tool_called", tool = "search" },'
        ' { type = "output", path = "hits.0", equals = "x" }]\n')
    monkeypatch.chdir(tmp_path)
    home = str(tmp_path / "h")
    assert main(["--home", home, "inspect"]) == 0
    out = capsys.readouterr().out
    assert "tool:search#1" in out and "No instrumentation gaps" in out
    assert main(["--home", home, "run", "--label", "baseline"]) == 0
    assert "2/2 trials passed" in capsys.readouterr().out
    [r] = store.list_runs(home)
    assert r.info["agent_kind"] == "python" and all(x["outcome"] == "pass" for x in r.results())


def test_cli_import_warns_on_mismatched_output_paths(tmp_path, capsys):
    from agentpatch.demo import SUITE_PATH

    files = [str(p) for p in sorted((FIXTURES / "agenteval").glob("baseline__*.json"))]
    assert main(["--home", str(tmp_path), "import", *files, "--label", "b", "--suite", str(SUITE_PATH)]) == 0
    assert "missing from every imported trace" in capsys.readouterr().out


# ---------- server ----------

def test_api_routes(tmp_path):
    _import_agenteval(tmp_path)
    api = Api(tmp_path)

    def get(path):
        status, ctype, body = api.handle(path)
        return status, json.loads(body) if "json" in ctype else body

    s, runs = get("/api/runs")
    assert s == 200 and len(runs["runs"]) == 2 and runs["default"]["baseline"].endswith("baseline")
    s, cmp = get("/api/compare")
    assert s == 200 and cmp["verdict"] == "regression"
    s, case = get("/api/case?case=unauthorized")
    assert s == 200 and case["diagnosis"]["likely_failure_point"]["step"]["id"] == "tool:save_assessment#1"
    assert case["candidate"]["trace"]["case_id"] == "unauthorized" and case["alignment"]["rows"]
    s, case = get("/api/case?case=normal&ctrial=99")  # unknown trial falls back to an existing one
    assert s == 200 and case["diagnosis"] is None
    s, tr = get("/api/trace?run=candidate&case=unauthorized&trial=0")
    assert s == 200 and tr["result"]["outcome"] == "fail"
    assert get("/api/coverage?run=baseline")[0] == 200
    assert get("/api/run?run=baseline")[1]["run"]["label"] == "baseline"
    assert get("/api/trace?run=candidate&case=unauthorized&trial=x")[0] == 400
    assert get("/api/trace?run=nope&case=a")[0] == 404
    assert get("/api/case?case=zzz")[0] == 404
    assert get("/api/compare?baseline=baseline&candidate=baseline")[0] == 400
    assert get("/api/unknown")[0] == 404


def test_static_files_and_path_traversal():
    api = Api()
    s, ctype, body = api.handle("/")
    assert s == 200 and b"AgentPatch" in body and ctype.startswith("text/html")
    assert api.handle("/app.js")[0] == 200
    assert api.handle("/../server.py")[0] == 404
    assert api.handle("/%2e%2e/server.py")[0] == 404


def test_api_with_no_runs(tmp_path):
    api = Api(tmp_path)
    s, _, body = api.handle("/api/runs")
    assert s == 200 and json.loads(body) == {"home": str(tmp_path.resolve()), "runs": [], "default": None}
    assert api.handle("/api/compare")[0] == 404


def test_http_server_serves_over_localhost(tmp_path):
    httpd = make_server(tmp_path, port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        port = httpd.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/runs", timeout=5) as r:
            assert r.status == 200 and json.loads(r.read())["runs"] == []
            assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    finally:
        httpd.shutdown()
        httpd.server_close()
