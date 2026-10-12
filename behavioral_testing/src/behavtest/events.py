"""Canonical agenteval traces -> ordered events with stable, human-readable IDs.

Event IDs are "{span name}#{k}" (the k-th span with that name in start order), so they stay the
same across re-runs even though OpenTelemetry span IDs are random. Trace content is only ever
read as data; nothing in it is evaluated or executed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agenteval.tracing import SCHEMA

KINDS = {"agent": "agent", "llm": "llm", "tool": "tool_call", "tool_attempt": "tool_attempt",
         "approval": "approval"}


@dataclass(frozen=True)
class TraceEvent:
    id: str
    index: int
    kind: str
    name: str
    span_id: str
    parent_id: str | None
    start_ns: int
    end_ns: int
    status: str
    error: Any
    input: Any
    output: Any
    attributes: dict
    tool: str | None = None

    def brief(self) -> str:
        bits = [self.id, self.kind, f"status={self.status}"]
        if self.error:
            bits.append(f"error={_error_text(self.error)}")
        return " ".join(bits)


@dataclass
class EventLog:
    events: list[TraceEvent]
    meta: dict
    integrity_issues: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.by_id = {e.id: e for e in self.events}
        self.by_span = {e.span_id: e for e in self.events}

    @property
    def complete(self) -> bool:
        return not self.integrity_issues

    @property
    def root(self) -> TraceEvent | None:
        return next((e for e in self.events if e.kind == "agent" and e.parent_id is None), None)

    def tool_calls(self, tool: str | None = None) -> list[TraceEvent]:
        return [e for e in self.events if e.kind == "tool_call" and (tool in (None, "*") or e.tool == tool)]

    def attempts(self, call: TraceEvent) -> list[TraceEvent]:
        return [e for e in self.events if e.kind == "tool_attempt" and e.parent_id == call.span_id]

    def orphans(self) -> list[TraceEvent]:
        return [e for e in self.events if e.parent_id is not None and e.parent_id not in self.by_span]

    def approvals(self) -> list[TraceEvent]:
        return [e for e in self.events if e.kind == "approval"]

    def preceding(self, event: TraceEvent, kind: str) -> TraceEvent | None:
        return next((e for e in reversed(self.events[:event.index]) if e.kind == kind), None)

    def sort_ids(self, ids) -> list[str]:
        return sorted(set(ids), key=lambda i: self.by_id[i].index if i in self.by_id else 1 << 30)


def _error_text(error: Any) -> str:
    if isinstance(error, dict):
        return f"{error.get('type', '')}: {error.get('message', '')}".strip(": ")
    return str(error)


def _tool_name(span: dict, parent: dict | None) -> str | None:
    if span.get("kind") == "tool":
        return (span.get("attributes") or {}).get("gen_ai.tool.name") or span["name"].removeprefix("tool.")
    if span.get("kind") == "tool_attempt":
        if parent is not None and parent.get("kind") == "tool":
            return _tool_name(parent, None)
        return span["name"].split(".attempt_")[0]
    return None


def build_events(trace: dict) -> EventLog:
    if not isinstance(trace, dict) or not isinstance(trace.get("spans"), list):
        raise ValueError("not a canonical trace: expected an object with a 'spans' list")
    issues = []
    if trace.get("schema") not in (None, SCHEMA):
        issues.append(f"unexpected trace schema {trace.get('schema')!r}")
    spans = trace["spans"]
    ids = [s.get("span_id") for s in spans]
    if len(set(ids)) != len(ids):
        issues.append("duplicate span ids")
    by_span = {s.get("span_id"): s for s in spans}
    order = sorted(range(len(spans)), key=lambda i: (int(spans[i].get("start_ns") or 0), i))
    counts: dict[str, int] = {}
    events = []
    for idx, i in enumerate(order):
        s = spans[i]
        name = str(s.get("name", "unnamed"))
        counts[name] = counts.get(name, 0) + 1
        parent = by_span.get(s.get("parent_id"))
        events.append(TraceEvent(
            id=f"{name}#{counts[name]}", index=idx, kind=KINDS.get(s.get("kind"), "other"), name=name,
            span_id=str(s.get("span_id")), parent_id=s.get("parent_id"),
            start_ns=int(s.get("start_ns") or 0), end_ns=int(s.get("end_ns") or 0),
            status=str(s.get("status", "unset")), error=s.get("error"), input=s.get("input"),
            output=s.get("output"), attributes=dict(s.get("attributes") or {}), tool=_tool_name(s, parent)))
    log = EventLog(events, {k: trace.get(k) for k in ("run_id", "case_id", "config", "mode", "model")})
    integrity = trace.get("integrity") or {}
    if integrity.get("complete") is False:
        issues.append(f"trace marked incomplete: {integrity.get('reason', 'no reason given')}")
    orphans = log.orphans()
    if orphans:
        issues.append("orphan events whose parent span is missing: " + ", ".join(e.id for e in orphans))
    if log.root is None:
        issues.append("no root agent event")
    log.integrity_issues = issues
    return log


def load_events(path: str | Path) -> EventLog:
    return build_events(json.loads(Path(path).read_text()))


def get_path(data: Any, dotted: str) -> Any:
    """Read a dotted key path from nested dicts; None when any part is missing."""
    for part in dotted.split("."):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data
