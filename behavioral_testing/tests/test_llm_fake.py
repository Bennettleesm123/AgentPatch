"""Gemini adapter tests with a fake client: no network, no keys, no live requests."""

import json
from types import SimpleNamespace

import pytest

from behavtest.compiler import compile_requirement
from behavtest.events import build_events
from behavtest.fixtures import build_trace
from behavtest.llm import CallBudgetExceeded, GeminiCompiler
from behavtest.report import check, to_markdown


class FakeClient:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.prompts = []
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, model, contents, config):
        self.prompts.append(contents)
        p = self.payloads.pop(0)
        if isinstance(p, Exception):
            raise p
        return SimpleNamespace(text=p if isinstance(p, str) else json.dumps(p),
                               usage_metadata=SimpleNamespace(total_token_count=10))


def _gc(*payloads, **kw):
    return GeminiCompiler(client=FakeClient(*payloads), min_interval_s=0, **kw)


def test_valid_model_rule_is_accepted():
    gc = _gc({"status": "compiled", "rule": {"type": "max_retries", "tool": "*", "max_retries": 2, "id": "x"}})
    res = gc.compile("Do not retry any tool more than 2 times.")
    assert res.status == "compiled" and res.method == "gemini"
    assert res.rule["id"].startswith("max_retries:")
    assert gc.usage()["calls"] == 1 and gc.usage()["total_tokens"] == 10


@pytest.mark.parametrize("payload", [
    {"status": "compiled", "rule": {"type": "forbidden_tool_call", "tool": "send_email"}},
    {"status": "compiled", "rule": {"type": "exec", "code": "import os"}},
    {"status": "compiled", "rule": None},
    "not json",
    ["a", "list"],
    TimeoutError("network down"),
])
def test_invalid_model_output_abstains(payload):
    res = _gc(payload).compile("Never call send_email.")
    assert res.status == "unsupported" and res.rule is None


def test_model_abstention_passes_through():
    res = _gc({"status": "ambiguous", "issues": ["hedged"]}).compile("Usually save.")
    assert res.status == "ambiguous" and res.issues == ["hedged"]


def test_call_cap_is_enforced():
    gc = _gc({"status": "ambiguous"}, {"status": "ambiguous"}, max_calls=1)
    gc.compile("a")
    with pytest.raises(CallBudgetExceeded):
        gc.compile("b")


def test_explanations_keep_only_cited_sentences():
    log = build_events(build_trace({"agent_case": "normal", "config": "baseline"}))
    report = check([compile_requirement("Get user approval before calling save_assessment.")], log)
    gc = _gc({"sentences": [
        "tool.save_assessment#1 ran with no approval event before it [tool.save_assessment#1].",
        "The model was probably confused by the prompt.",
        "A heartbeat timeout caused this [noise.heartbeat#1].",
        "Check llm.turn_3#1, which chose the call [llm.turn_3#1, tool.save_assessment#1]",
    ]})
    gc.explain_report(report, log)
    mx = report["requirements"][0]["model_explanation"]
    assert [s["cites"] for s in mx["sentences"]] == [["tool.save_assessment#1"],
                                                      ["llm.turn_3#1", "tool.save_assessment#1"]]
    assert mx["dropped_uncited"] == 2 and gc.usage()["dropped_explanation_sentences"] == 2
    assert "Model explanation" in to_markdown(report, log)
    prompt = gc.client.prompts[0]
    assert "Acme Robotics" not in prompt  # raw trace inputs are not sent, only structured summaries
