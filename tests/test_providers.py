import json

import pytest

from agenteval import cli
from agenteval.providers import PROVIDERS, NativeEvalUnsupported, dataset_items, get_adapter
from agenteval.providers.base import Ledger
from agenteval.providers.raindrop import to_otlp
from agenteval.tracing import load_trace

from test_agent import run


@pytest.fixture
def trace(out_dir):
    return load_trace(run("transient_failure", out_dir=out_dir)["trace_path"])


@pytest.mark.parametrize("name", [p for p in PROVIDERS if p != "raindrop"])
def test_sdk_plans_cover_every_span(trace, name):
    plan = get_adapter(name).plan(trace)
    assert len(plan) == len(trace["spans"])
    assert plan[0]["parent"] is None
    assert any(row.get("error") or row.get("error_info") for row in plan)


def test_raindrop_otlp_payload(trace):
    spans = to_otlp(trace)["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == len(trace["spans"])
    attrs = lambda s: {a["key"]: a["value"] for a in s["attributes"]}
    llm = [s for s in spans if s["name"].startswith("llm.")]
    tools = [s for s in spans if s["name"].startswith("tool.")]
    assert all("gen_ai.system" in attrs(s) and "gen_ai.usage.input_tokens" in attrs(s) for s in llm)
    assert all("gen_ai.system" not in attrs(s) for s in tools)
    assert sum(s["status"]["code"] == 2 for s in spans) == 2  # timeout + 503 attempts
    assert all(len(s["traceId"]) == 32 and len(s["spanId"]) == 16 for s in spans)
    assert sum("parentSpanId" not in s for s in spans) == 1


def test_raindrop_event_links_to_trace(trace):
    from agenteval.providers.raindrop import to_event

    event = to_event(trace)
    assert event["event_id"] == trace["run_id"] and event["user_id"] == "agenteval-lab"
    calls = event["ai_data"]["tool_calls"]
    assert [c["status"] for c in calls] == ["ok", "error", "error", "ok", "ok"]
    assert calls[1]["name"] == "get_company_details" and "timed out" in calls[1]["error"]
    assert calls[1]["arguments"] == {"company_id": "GLBX-001"}
    assert event["ai_data"]["input"].startswith("Research the company")
    for span in to_otlp(trace)["resourceSpans"][0]["scopeSpans"][0]["spans"]:
        keys = {a["key"]: a["value"] for a in span["attributes"]}
        assert keys["traceloop.association.properties.event_id"] == {"stringValue": trace["run_id"]}


def test_raindrop_native_eval_unsupported():
    with pytest.raises(NativeEvalUnsupported):
        get_adapter("raindrop").native_eval(None, [], [])


def test_dataset_items_identical_shape():
    items = dataset_items(["normal", "conflict"])
    assert items[1]["expected"]["expect_conflict"] is True and items[0]["input"]["company"] == "Acme Robotics"


def test_ledger_caps_uploads(tmp_path):
    ledger = Ledger(tmp_path / "ledger.json")
    for _ in range(3):
        ledger.reserve("opik", "traces", 1, 3)
    with pytest.raises(SystemExit):
        ledger.reserve("opik", "traces", 1, 3)
    assert json.loads((tmp_path / "ledger.json").read_text()) == {"opik": {"traces": 3}}


def test_cli_trace_dry_run_sends_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["trace", "--provider", "all", "--case", "normal"]) == 0
    out = capsys.readouterr().out
    assert out.count("[dry-run]") == 4
    assert not (tmp_path / "artifacts/upload_ledger.json").exists()
    assert (tmp_path / "artifacts/provider_payloads/raindrop__baseline__normal.json").exists()


def test_cli_upload_requires_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPIK_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="OPIK_API_KEY"):
        cli.main(["trace", "--provider", "opik", "--approve-upload"])
    assert not (tmp_path / "artifacts/upload_ledger.json").exists()


def test_cli_native_eval_dry_run(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["eval", "--native", "braintrust"]) == 0
    assert "Nothing was sent" in capsys.readouterr().out
    assert cli.main(["eval", "--native", "raindrop", "--approve-upload"]) == 0
    assert "not available" in capsys.readouterr().out


def test_cli_gemini_requires_approval(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run", "--gemini", "--case", "normal"]) == 2
    assert "Nothing was sent" in capsys.readouterr().out


def test_cli_compare_fail_on_regression(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["compare"]) == 0
    assert cli.main(["compare", "--fail-on-regression"]) == 1
    assert json.loads((tmp_path / "artifacts/reports/compare.json").read_text())["verdict"] == "regression"
