"""Model backends: a deterministic scripted model (offline) and Gemini (explicit opt-in)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

from .data import CONFLICT_FIELDS, icp_decision


class ModelError(Exception):
    retryable = False


class TransientModelError(ModelError):
    retryable = True


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class ModelResponse:
    text: str | None
    tool_calls: list[ToolCall]
    model: str
    usage: dict = field(default_factory=dict)
    raw: object | None = None  # provider-native content needed to continue the conversation


class Model(Protocol):
    name: str
    provider: str

    def generate(self, system: str, messages: list[dict], tools: list[dict]) -> ModelResponse: ...


def _estimate_tokens(obj) -> int:
    return max(1, len(json.dumps(obj, default=str)) // 4)


class ScriptedModel:
    """Deterministic stand-in for an LLM. Decisions follow `policy` only."""

    provider = "scripted"

    def __init__(self, policy: dict, name: str = "scripted-v1"):
        self.policy = policy
        self.name = name

    def generate(self, system: str, messages: list[dict], tools: list[dict]) -> ModelResponse:
        step = self._decide(messages)
        if isinstance(step, list):
            calls = [ToolCall(id=f"call_{len(messages)}_{i}", name=n, args=a) for i, (n, a) in enumerate(step)]
            resp = ModelResponse(text=None, tool_calls=calls, model=self.name)
        else:
            resp = ModelResponse(text=json.dumps(step, sort_keys=True), tool_calls=[], model=self.name)
        out = resp.text or [c.__dict__ for c in resp.tool_calls]
        inp, outp = _estimate_tokens([system, messages]), _estimate_tokens(out)
        resp.usage = {"input_tokens": inp, "output_tokens": outp, "total_tokens": inp + outp, "estimated": True}
        return resp

    def _decide(self, messages: list[dict]):
        task = messages[0]["meta"]
        company, authorized = task["company"], task["authorized"]
        results = {}
        for m in messages:
            if m["role"] == "tool":
                results.setdefault(m["name"], []).append(m["content"])

        def final(decision, company_id=None, conflict=False, saved=False, reason=""):
            return {"company": company, "company_id": company_id, "decision": decision,
                    "conflict": conflict, "saved": saved, "reason": reason}

        if "search_company" not in results:
            return [("search_company", {"name": company})]
        search = results["search_company"][-1]
        if "error" in search:
            return final("error", reason=f"search failed: {search['error']}")
        hits = search.get("results", [])
        if not hits:
            return final("not_found", reason="no CRM record")

        if "get_company_details" not in results:
            targets = hits if self.policy.get("conflict_strategy") == "review_all" else hits[:1]
            return [("get_company_details", {"company_id": h["company_id"]}) for h in targets]
        details = results["get_company_details"]
        errors = [d["error"] for d in details if "error" in d]
        if errors:
            return final("error", reason=f"details failed: {errors[0]}")

        conflict = len(details) > 1 and any(
            len({json.dumps(d.get(f)) for d in details}) > 1 for f in CONFLICT_FIELDS
        )
        if conflict:
            assessment = final("needs_review", conflict=True, reason="records disagree on firmographics")
        else:
            rec = details[0]
            assessment = final(icp_decision(rec), company_id=rec["company_id"],
                               reason=f"{rec['employees']} employees, region {rec['region']}")

        should_save = authorized or self.policy.get("always_save", False)
        if should_save and "save_assessment" not in results:
            payload = {k: assessment[k] for k in ("company", "company_id", "decision", "reason")}
            return [("save_assessment", {"result": payload})]
        if "save_assessment" in results:
            assessment["saved"] = bool(results["save_assessment"][-1].get("saved"))
        return assessment


def build_model(mode: str, config, **kwargs) -> Model:
    if mode == "offline":
        return ScriptedModel(config.policy)
    if mode == "gemini":
        from .gemini import GeminiModel

        return GeminiModel(**kwargs)
    raise ValueError(f"unknown mode {mode!r}")
