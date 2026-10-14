"""Raindrop via its documented HTTP API: one event per run plus the nested OTLP trace.

Raindrop's dashboard is built on events (POST /v1/events/track); OTLP traces alone do not
create one. Spans are linked to the event through `traceloop.association.properties.*`
attributes, the same keys the raindrop-ai SDK stamps on spans. The SDK itself cannot set
explicit span timestamps or create manual LLM spans, so the recorded trace is sent as OTLP.
Docs: https://raindrop.ai/docs/sdk/http-api  https://raindrop.ai/docs/sdk/opentelemetry
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
BASE_URL = "https://api.raindrop.ai/v1"
ENDPOINT = f"{BASE_URL}/traces"
EVENTS_ENDPOINT = f"{BASE_URL}/events/track"
USER_ID = "agenteval-lab"
EVENT_NAME = "lead_research"
NATIVE_EVAL = False


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


def _identity(trace: dict) -> dict:
    return {"user_id": USER_ID, "event": EVENT_NAME, "event_id": trace["run_id"], "convo_id": trace["run_id"]}


def to_event(trace: dict) -> dict:
    """One Raindrop AI event per run; every tool attempt (including failed ones) is a tool call."""
    by_id = {s["span_id"]: s for s in trace["spans"]}
    root = next(s for s in trace["spans"] if s["parent_id"] is None)
    llm = next((s for s in trace["spans"] if s["kind"] == "llm"), None)
    tool_calls = []
    for s in trace["spans"]:
        if s["kind"] != "tool_attempt":
            continue
        parent = by_id.get(s["parent_id"], {})
        call = {"name": parent.get("attributes", {}).get("gen_ai.tool.name", s["name"].rsplit(".attempt_", 1)[0]),
                "arguments": s["input"] if s["input"] is not None else parent.get("input"),
                "status": "error" if s["error"] else "ok", "call_id": s["span_id"]}
        if s["error"]:
            call["error"] = s["error"]
        else:
            call["result"] = s["output"]
        tool_calls.append(call)
    outcome = trace.get("outcome") or {}
    final = outcome.get("final") or {}
    ident = _identity(trace)
    return {
        "user_id": ident["user_id"], "event": ident["event"], "event_id": ident["event_id"],
        "properties": {"case_id": trace["case_id"], "config": trace["config"], "mode": trace["mode"],
                       "status": outcome.get("status"), "decision": final.get("decision"),
                       "local_trace_id": trace["trace_id"]},
        "ai_data": {"model": (llm or {}).get("attributes", {}).get("gen_ai.response.model", trace.get("model")),
                    "input": (root["input"] or {}).get("prompt", ""), "output": _text(final),
                    "convo_id": ident["convo_id"], "tool_calls": tool_calls},
    }


def to_otlp(trace: dict) -> dict:
    association = {f"traceloop.association.properties.{k}": v for k, v in _identity(trace).items()}
    spans = []
    for span, _ in walk(trace):
        attrs = dict(span["attributes"]) | association
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


def _headers(key: str) -> dict:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if os.environ.get("RAINDROP_PROJECT_ID"):
        headers["X-Raindrop-Project-Id"] = os.environ["RAINDROP_PROJECT_ID"]
    return headers


def plan(trace: dict) -> dict:
    headers = _headers("[RAINDROP_WRITE_KEY]")
    return {"requests": [{"POST": EVENTS_ENDPOINT, "headers": headers, "body": [to_event(trace)]},
                         {"POST": ENDPOINT, "headers": headers, "body": to_otlp(trace)}]}


def _post(url: str, body, headers: dict) -> int:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.status


def upload(trace: dict) -> str:
    headers = _headers(os.environ["RAINDROP_WRITE_KEY"])
    event_status = _post(EVENTS_ENDPOINT, [to_event(trace)], headers)
    trace_status = _post(ENDPOINT, to_otlp(trace), headers)
    return (f"event HTTP {event_status} (event_id={trace['run_id']}), trace HTTP {trace_status} "
            f"(trace_id={trace['trace_id']}). Look under Events for user '{USER_ID}'.")


def native_eval(run_fn, configs, case_ids):
    from . import NativeEvalUnsupported

    raise NativeEvalUnsupported(
        "Raindrop has no SDK harness for deterministic code scorers on the free Hobby plan. "
        "Its evaluation features are Signals (LLM-drafted classifiers; preset tool-error signals) and "
        "Experiments (cohort comparison, Pro plan). Upload traces and review Signals in the UI instead.")
