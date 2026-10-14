"""Import existing traces without changing agent code.

Supported (auto-detected):
- AgentPatch traces (`agentpatch.trace.v1`)
- AgentEvalLab canonical traces (`agenteval.trace.v1`, in this repo's `artifacts/traces/`)
- OpenTelemetry OTLP/JSON exports (`{"resourceSpans": [...]}`), using GenAI semantic conventions
  (`gen_ai.operation.name`, `gen_ai.tool.name`, `gen_ai.request.model`, `gen_ai.usage.*`) or
  OpenInference span kinds when present.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .schema import SCHEMA, Trace, finalize_steps, jsonable

OTEL_KIND = {"chat": "llm", "text_completion": "llm", "generate_content": "llm", "embeddings": "llm",
             "execute_tool": "tool", "invoke_agent": "agent", "create_agent": "agent"}
OPENINFERENCE_KIND = {"LLM": "llm", "TOOL": "tool", "AGENT": "node", "CHAIN": "node", "RETRIEVER": "retriever"}
CASE_KEYS = ("agentpatch.case_id", "agenteval.case_id", "case_id", "session.id", "gen_ai.conversation.id")


def detect(data: Any) -> str:
    if isinstance(data, dict):
        if data.get("schema") == SCHEMA:
            return "agentpatch"
        if data.get("schema") == "agenteval.trace.v1":
            return "agenteval"
        if "resourceSpans" in data:
            return "otlp"
    raise ValueError("unrecognised trace format: expected an AgentPatch trace, an AgentEvalLab trace "
                     "(schema agenteval.trace.v1) or an OTLP/JSON export with 'resourceSpans'")


def load_file(path: str | Path) -> list[Trace]:
    try:
        data = json.loads(Path(path).read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: not valid JSON ({exc.msg} at line {exc.lineno})") from None
    fmt = detect(data)
    if fmt == "agentpatch":
        return [Trace.from_dict(data)]
    if fmt == "agenteval":
        return [from_agenteval(data)]
    return from_otlp(data)


def _ns_to_ms(ns: int, t0: int) -> float:
    return (int(ns) - t0) / 1e6


def from_agenteval(d: dict) -> Trace:
    spans = d.get("spans") or []
    t0 = min((int(s["start_ns"]) for s in spans), default=0)
    raw = []
    for s in spans:
        kind = {"agent": "agent", "llm": "llm", "tool": "tool", "tool_attempt": "attempt"}.get(s.get("kind"), "other")
        attrs = s.get("attributes") or {}
        name = s["name"]
        if kind == "tool":
            name = attrs.get("gen_ai.tool.name") or name.removeprefix("tool.")
        elif kind == "attempt":
            name = name.split(".attempt_")[0]
        elif kind == "llm":
            name = attrs.get("gen_ai.request.model") or name
        a: dict = {}
        if kind == "llm":
            a["model"] = attrs.get("gen_ai.response.model") or attrs.get("gen_ai.request.model")
            calls = (s.get("output") or {}).get("tool_calls") if isinstance(s.get("output"), dict) else None
            if calls:
                a["requested_tools"] = [c.get("name") for c in calls]
        if s.get("usage"):
            a["usage"] = {k: s["usage"].get(k) for k in ("input_tokens", "output_tokens", "total_tokens")}
            if s["usage"].get("estimated"):
                a["usage_estimated"] = True
        if "agenteval.attempt" in attrs:
            a["attempt"] = attrs["agenteval.attempt"]
        raw.append({"key": s["span_id"], "name": name, "kind": kind, "parent_key": s.get("parent_id"),
                    "start_ms": _ns_to_ms(s["start_ns"], t0),
                    "end_ms": _ns_to_ms(s["end_ns"], t0) if s.get("end_ns") else None,
                    "status": s.get("status", "ok"), "error": s.get("error"), "input": jsonable(s.get("input")),
                    "output": jsonable(s.get("output")), "attributes": a})
    steps = finalize_steps(raw)
    outcome = d.get("outcome") or {}
    root = next((s for s in steps if s.parent_id is None), None)
    status = "ok" if outcome.get("status", "completed") == "completed" else "error"
    return Trace(trace_id=d.get("trace_id") or d.get("run_id") or "agenteval", case_id=d.get("case_id") or "unknown",
                 input=root.input if root else None, output=outcome.get("final"), status=status,
                 error=None if status == "ok" else f"run ended with status {outcome.get('status')}",
                 started_at=d.get("start_time"), duration_ms=d.get("latency_ms"), source="agenteval", steps=steps,
                 meta={"config": d.get("config"), "mode": d.get("mode"), "model": d.get("model")})


def _attr_value(v: dict) -> Any:
    for k in ("stringValue", "boolValue", "doubleValue"):
        if k in v:
            return v[k]
    if "intValue" in v:
        return int(v["intValue"])
    if "arrayValue" in v:
        return [_attr_value(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return {x["key"]: _attr_value(x.get("value", {})) for x in v["kvlistValue"].get("values", [])}
    return None


def _maybe_json(v: Any) -> Any:
    if isinstance(v, str) and v[:1] in "[{":
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def from_otlp(data: dict) -> list[Trace]:
    spans = []
    for rs in data.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", rs.get("instrumentationLibrarySpans", [])):
            for sp in ss.get("spans", []):
                attrs = {a["key"]: _attr_value(a.get("value", {})) for a in sp.get("attributes", [])}
                spans.append({**sp, "_attrs": attrs})
    by_trace: dict[str, list] = {}
    for sp in spans:
        by_trace.setdefault(sp.get("traceId", ""), []).append(sp)
    traces = []
    for trace_id, group in by_trace.items():
        t0 = min(int(s.get("startTimeUnixNano", 0)) for s in group)
        raw, case_id = [], None
        for s in group:
            a = s["_attrs"]
            op = a.get("gen_ai.operation.name")
            kind = OTEL_KIND.get(op) or OPENINFERENCE_KIND.get(a.get("openinference.span.kind")) or \
                (a.get("agenteval.kind") if a.get("agenteval.kind") in ("agent", "llm", "tool") else None) or "other"
            if not s.get("parentSpanId") and kind == "other":
                kind = "agent"
            name = s.get("name", "span")
            if kind == "tool":
                name = a.get("gen_ai.tool.name") or a.get("tool.name") or name.removeprefix("tool.")
            elif kind == "llm":
                name = a.get("gen_ai.request.model") or a.get("llm.model_name") or name
            attrs: dict = {}
            if kind == "llm":
                attrs["model"] = a.get("gen_ai.response.model") or a.get("gen_ai.request.model") or a.get("llm.model_name")
                usage = {"input_tokens": a.get("gen_ai.usage.input_tokens", a.get("llm.token_count.prompt")),
                         "output_tokens": a.get("gen_ai.usage.output_tokens", a.get("llm.token_count.completion"))}
                if any(v is not None for v in usage.values()):
                    usage["total_tokens"] = sum(v or 0 for v in usage.values())
                    attrs["usage"] = usage
            for k in CASE_KEYS:
                if a.get(k) is not None and case_id is None:
                    case_id = str(a[k])
            status = s.get("status") or {}
            err = status.get("code") in (2, "STATUS_CODE_ERROR")
            raw.append({"key": s.get("spanId"), "name": name, "kind": kind, "parent_key": s.get("parentSpanId") or None,
                        "start_ms": _ns_to_ms(s.get("startTimeUnixNano", t0), t0),
                        "end_ms": _ns_to_ms(s["endTimeUnixNano"], t0) if s.get("endTimeUnixNano") else None,
                        "status": "error" if err else "ok",
                        "error": (status.get("message") or a.get("error.type") or "error") if err else None,
                        "input": jsonable(_maybe_json(a.get("input.value", a.get("agenteval.input")))),
                        "output": jsonable(_maybe_json(a.get("output.value", a.get("agenteval.output")))),
                        "attributes": attrs})
        steps = finalize_steps(raw)
        root = next((s for s in steps if s.parent_id is None), None)
        traces.append(Trace(trace_id=trace_id, case_id=case_id or (root.name if root else "unknown"),
                            input=root.input if root else None, output=root.output if root else None,
                            status=root.status if root else "ok", error=root.error if root else None,
                            duration_ms=root.duration_ms if root else None, source="otlp", steps=steps))
    return traces
