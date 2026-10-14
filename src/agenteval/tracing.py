"""Provider-neutral tracing on OpenTelemetry, exported to a canonical local JSON trace.

Span attributes follow the OpenTelemetry GenAI semantic conventions where they
exist (gen_ai.*); inputs/outputs are stored as JSON strings under agenteval.*.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

SCHEMA = "agenteval.trace.v1"
_USAGE_PREFIX = "gen_ai.usage."


def dumps(value) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def _loads(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat()


class SpanHandle:
    def __init__(self, span):
        self.otel = span

    def set_output(self, value) -> None:
        self.otel.set_attribute("agenteval.output", dumps(value))

    def set_attributes(self, attrs: dict) -> None:
        for k, v in attrs.items():
            if v is not None:
                self.otel.set_attribute(k, v if isinstance(v, (str, bool, int, float)) else dumps(v))

    def set_usage(self, usage: dict) -> None:
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            if usage.get(key) is not None:
                self.otel.set_attribute(_USAGE_PREFIX + key, int(usage[key]))
        if usage.get("estimated"):
            self.otel.set_attribute("agenteval.usage.estimated", True)

    def set_error(self, exc: BaseException) -> None:
        self.otel.record_exception(exc)
        self.otel.set_attribute("error.type", type(exc).__name__)
        self.otel.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))


class Tracer:
    """One tracer per agent run; extra OTel span processors may be attached (e.g. OTLP)."""

    def __init__(self, processors=()):
        self._memory = InMemorySpanExporter()
        self.provider = TracerProvider(resource=Resource.create({"service.name": "agenteval"}))
        self.provider.add_span_processor(SimpleSpanProcessor(self._memory))
        for p in processors:
            self.provider.add_span_processor(p)
        self._otel = self.provider.get_tracer("agenteval")

    @contextmanager
    def span(self, name: str, kind: str, input=None, attributes: dict | None = None):
        attrs = {"agenteval.kind": kind}
        if input is not None:
            attrs["agenteval.input"] = dumps(input)
        with self._otel.start_as_current_span(name, attributes=attrs) as s:
            handle = SpanHandle(s)
            if attributes:
                handle.set_attributes(attributes)
            yield handle

    def canonical(self, meta: dict) -> dict:
        spans = sorted(self._memory.get_finished_spans(), key=lambda s: s.start_time)
        out = []
        for s in spans:
            attrs = dict(s.attributes or {})
            usage = {k[len(_USAGE_PREFIX):]: attrs.pop(k) for k in list(attrs) if k.startswith(_USAGE_PREFIX)}
            if attrs.pop("agenteval.usage.estimated", False):
                usage["estimated"] = True
            error = s.status.description if s.status.status_code == StatusCode.ERROR else None
            out.append({
                "span_id": f"{s.context.span_id:016x}",
                "parent_id": f"{s.parent.span_id:016x}" if s.parent else None,
                "name": s.name,
                "kind": attrs.pop("agenteval.kind", "internal"),
                "start_time": iso(s.start_time),
                "end_time": iso(s.end_time),
                "start_ns": s.start_time,
                "end_ns": s.end_time,
                "latency_ms": round((s.end_time - s.start_time) / 1e6, 3),
                "status": "error" if error else "ok",
                "error": error,
                "input": _loads(attrs.pop("agenteval.input", None)),
                "output": _loads(attrs.pop("agenteval.output", None)),
                "usage": usage or None,
                "attributes": attrs,
            })
        root = next((s for s in out if s["parent_id"] is None), None)
        trace_id = f"{spans[0].context.trace_id:032x}" if spans else None
        return {
            "schema": SCHEMA,
            "trace_id": trace_id,
            **meta,
            "start_time": root and root["start_time"],
            "end_time": root and root["end_time"],
            "latency_ms": root and root["latency_ms"],
            "spans": out,
        }

    def shutdown(self) -> None:
        self.provider.shutdown()


def write_trace(trace: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trace, indent=2, default=str))
    return path


def load_trace(path: Path) -> dict:
    return json.loads(Path(path).read_text())
