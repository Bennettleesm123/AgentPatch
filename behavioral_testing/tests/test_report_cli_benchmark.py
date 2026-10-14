import json

import pytest

from behavtest.benchmark import run_benchmark
from behavtest.cli import main
from behavtest.compiler import compile_requirement
from behavtest.events import build_events
from behavtest.fixtures import build_trace
from behavtest.report import check, to_markdown


def test_report_orders_failures_first_and_links_evidence():
    log = build_events(build_trace({"agent_case": "unauthorized", "config": "candidate"}))
    reqs = [compile_requirement(t) for t in ("Do not retry any tool more than 2 times.",
                                             "Never call save_assessment when write permission is denied.",
                                             "Responses must be polite and concise.")]
    report = check(reqs, log)
    assert report["overall"] == "fail"
    assert report["counts"] == {"pass": 1, "fail": 1, "unknown": 0, "not_tested": 1}
    md = to_markdown(report, log)
    assert md.index("## FAIL") < md.index("## NOT TESTED") < md.index("## PASS")
    assert "`tool.save_assessment#1`" in md and "Possible causes** (not verified" in md


def test_untested_requirement_blocks_overall_pass():
    log = build_events(build_trace({"agent_case": "normal", "config": "baseline"}))
    report = check([compile_requirement("Retry a few times at most.")], log)
    assert report["overall"] == "unknown"


def test_report_redacts_secrets(monkeypatch):
    secret = "sk-test-0123456789abcdef"
    monkeypatch.setenv("FAKE_API_KEY", secret)
    trace = build_trace({"agent_case": "normal", "config": "baseline"})
    save = next(s for s in trace["spans"] if s["name"] == "tool.save_assessment")
    save["status"], save["error"] = "error", {"type": "AuthError", "message": f"bad key {secret}"}
    log = build_events(trace)
    report = check([compile_requirement("Get user approval before calling save_assessment.")], log)
    md = to_markdown(report, log)
    assert secret not in md and "[REDACTED]" in md


def test_cli_compile_and_evaluate(capsys):
    assert main(["compile", "Retry get_company_details at most once."]) == 0
    assert json.loads(capsys.readouterr().out)["rule"]["max_retries"] == 1
    assert main(["compile", "Usually save the assessment."]) == 2
    capsys.readouterr()
    code = main(["evaluate", "--agent-case", "transient_failure", "--rule", "Retry get_company_details at most once."])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["overall"] == "fail"
    assert "get_company_details.attempt_3#1" in out["results"][0]["evidence"]


def test_cli_diagnose_writes_files(tmp_path, capsys):
    trace = tmp_path / "t.json"
    trace.write_text(json.dumps(build_trace({"agent_case": "normal", "config": "baseline"})))
    code = main(["diagnose", "--trace", str(trace), "--rule", "Get user approval before calling save_assessment.",
                 "--out", str(tmp_path / "out")])
    assert code == 1
    assert json.loads((tmp_path / "out/report.json").read_text())["overall"] == "fail"
    assert (tmp_path / "out/report.md").read_text().startswith("# Behavioral check: FAIL")


def test_live_mode_requires_approval():
    with pytest.raises(SystemExit, match="approve-live"):
        main(["compile", "--gemini", "Never call save_assessment."])


def test_benchmark_is_reproducible(tmp_path):
    a, b = run_benchmark(), run_benchmark()
    strip = lambda r: {k: v for k, v in r.items() if k != "latency_ms"}
    assert strip(a) == strip(b)
    assert a["unknown_reported_as_pass"] == 0
    assert a["explanations"]["unsupported_claims"] == 0
    assert a["model_usage"] == {"mode": "offline", "calls": 0}
    assert main(["benchmark", "--out", str(tmp_path)]) == 0
    assert json.loads((tmp_path / "benchmark.json").read_text())["cases"] == 30
