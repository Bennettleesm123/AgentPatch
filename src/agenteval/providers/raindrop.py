"""Raindrop via its documented OTLP/HTTP JSON trace endpoint.

The `raindrop-ai` Python SDK cannot set explicit span timestamps or create manual
LLM spans, so faithful replay of a recorded trace uses OTLP instead.
Docs: https://raindrop.ai/docs/sdk/opentelemetry
"""

from __future__ import annotations

import json
import os
import urllib.request

from ..tracing import dumps
from .base import trace_metadata, walk

NAME = "raindrop"
ENV = ("RAINDROP_WRITE_KEY",)
DOCS = "https://raindrop.ai/docs"
ENDPOINT = "https://api.raindrop.ai/v1/traces"


def _attr(key: str, value) -> dict:
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    else:
        v = {"stringValue": value if isinstance(value, str) else dumps(value)}
    return {"key": key, "value": v}


def _text(value) -> str:
    return value if isinstance(value, str) else dumps(value)


def _messages(span: dict) -> tuple[list, list]:
    msgs = [{"role": m["role"], "content": _text(m["tool_calls"] if m.get("content") is None else m["content"])}
            for m in (span["input"] or {}).get("messages", [])]
    out = span["output"] or {}
    reply = _text(out["tool_calls"] if out.get("text") is None else out["text"])
    return msgs, [{"role": "assistant", "content": reply}]


def to_otlp(trace: dict) -> dict:
    spans = []
    for span, _ in walk(trace):
        attrs = dict(span["attributes"])
        if span["kind"] == "llm":
            inp, outp = _messages(span)
            attrs |= {"gen_ai.input.messages": dumps(inp), "gen_ai.output.messages": dumps(outp),
                      "gen_ai.system_instructions": (span["input"] or {}).get("system", "")}
            for k, v in (span["usage"] or {}).items():
                if k in ("input_tokens", "output_tokens"):
                    attrs[f"gen_ai.usage.{k}"] = v
        else:
            # gen_ai.system marks LLM spans in Raindrop, so keep it off non-LLM spans.
            attrs.pop("gen_ai.system", None)
            attrs |= {"agenteval.input": span["input"], "agenteval.output": span["output"]}
        if span["parent_id"] is None:
            attrs |= {f"agenteval.{k}": v for k, v in trace_metadata(trace).items() if v is not None}
        otlp = {"traceId": trace["trace_id"], "spanId": span["span_id"], "name": span["name"], "kind": 1,
                "startTimeUnixNano": str(span["start_ns"]), "endTimeUnixNano": str(span["end_ns"]),
                "attributes": [_attr(k, v) for k, v in attrs.items() if v is not None],
                "status": {"code": 2, "message": span["error"]} if span["error"] else {"code": 1}}
        if span["parent_id"]:
            otlp["parentSpanId"] = span["parent_id"]
        spans.append(otlp)
    return {"resourceSpans": [{
        "resource": {"attributes": [_attr("service.name", "agenteval")]},
        "scopeSpans": [{"scope": {"name": "agenteval"}, "spans": spans}],
    }]}


def plan(trace: dict) -> dict:
    return {"POST": ENDPOINT, "headers": {"Authorization": "Bearer [RAINDROP_WRITE_KEY]",
                                         "Content-Type": "application/json",
                                         "X-Raindrop-Project-Id": os.environ.get("RAINDROP_PROJECT_ID", "(default)")},
            "body": to_otlp(trace)}


def upload(trace: dict) -> str:
    headers = {"Authorization": f"Bearer {os.environ['RAINDROP_WRITE_KEY']}", "Content-Type": "application/json"}
    if os.environ.get("RAINDROP_PROJECT_ID"):
        headers["X-Raindrop-Project-Id"] = os.environ["RAINDROP_PROJECT_ID"]
    req = urllib.request.Request(ENDPOINT, data=json.dumps(to_otlp(trace)).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return f"HTTP {resp.status} trace_id={trace['trace_id']}"


def native_eval(records, case_ids):
    from . import NativeEvalUnsupported

    raise NativeEvalUnsupported(
        "Raindrop has no SDK harness for deterministic code scorers on the free Hobby plan. "
        "Its evaluation features are Signals (LLM-drafted classifiers; preset tool-error signals) and "
        "Experiments (cohort comparison, Pro plan). Upload traces and review Signals in the UI instead.")
