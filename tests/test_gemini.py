"""Exercises the Gemini backend against a fake client: no network, no API key."""

import pytest

genai = pytest.importorskip("google.genai")
from google.genai import types  # noqa: E402

from agenteval.agent import Limits, run_case  # noqa: E402
from agenteval.configs import BASELINE  # noqa: E402
from agenteval.data import CASES_BY_ID  # noqa: E402
from agenteval.evals import evaluate_record  # noqa: E402


def _call(name, args, cid):
    part = types.Part(function_call=types.FunctionCall(name=name, args=args, id=cid))
    return _resp([part])


def _resp(parts):
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=parts))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=100, candidates_token_count=10, total_token_count=110),
        model_version="gemini-3.5-flash-lite")


class FakeModels:
    def __init__(self):
        final = ('{"company": "Acme Robotics", "company_id": "ACME-001", "decision": "qualified", '
                 '"conflict": false, "saved": true, "reason": "420 employees, NA"}')
        self.script = [
            _call("search_company", {"name": "Acme Robotics"}, "c1"),
            _call("get_company_details", {"company_id": "ACME-001"}, None),
            _call("save_assessment", {"result": {"company": "Acme Robotics", "company_id": "ACME-001",
                                                  "decision": "qualified", "reason": "fits ICP"}}, "c3"),
            _resp([types.Part.from_text(text=final)]),
        ]
        self.requests = []

    def generate_content(self, model, contents, config):
        self.requests.append((model, contents, config))
        if len(self.requests) == 1:
            raise genai.errors.ServerError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})
        return self.script.pop(0)


class FakeClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.models = FakeModels()


def test_gemini_tool_calling_loop(monkeypatch, out_dir):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key-000")
    monkeypatch.setattr(genai, "Client", FakeClient)
    from agenteval.gemini import GeminiModel

    model = GeminiModel()
    rec = run_case(CASES_BY_ID["normal"], BASELINE, model, "gemini", Limits(), out_dir=out_dir)
    assert rec["status"] == "completed" and evaluate_record(rec)["passed"]
    assert rec["usage"]["total_tokens"] == 440 and "estimated" not in rec["usage"]

    reqs = model.client.models.requests
    assert len(reqs) == 5  # one 503 retried, then four turns
    _, contents, cfg = reqs[-1]
    assert cfg.automatic_function_calling.disable is True
    assert [c.role for c in contents] == ["user", "model", "tool", "model", "tool", "model", "tool"]
    fr = contents[2].parts[0].function_response
    assert fr.name == "search_company" and fr.id == "c1" and fr.response["results"]
    assert contents[4].parts[0].function_response.id is None


def test_gemini_live_call_cap(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key-000")
    monkeypatch.setattr(genai, "Client", FakeClient)
    from agenteval.gemini import GeminiModel
    from agenteval.models import ModelError

    model = GeminiModel(max_calls=0)
    with pytest.raises(ModelError, match="safety cap"):
        model.generate("sys", [{"role": "user", "content": "hi"}], [])
