"""Shared provider plumbing: replaying canonical traces, approval gate and upload ledger.

Adapters only read canonical traces / run records after the agent has finished,
so no provider can influence agent behaviour.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

LEDGER = Path("artifacts/upload_ledger.json")
MAX_TRACE_UPLOADS = 3
MAX_EVAL_ROWS = 30  # cumulative per provider: three full native-eval runs (5 cases x 2 configs)


def span_tree(trace: dict) -> tuple[dict, dict[str, list[dict]]]:
    children: dict[str, list[dict]] = {}
    root = None
    for s in trace["spans"]:
        if s["parent_id"] is None:
            root = s
        else:
            children.setdefault(s["parent_id"], []).append(s)
    if root is None:
        raise ValueError("trace has no root span")
    return root, children


def walk(trace: dict):
    """Yield (span, parent_span_or_None) depth-first in start order."""
    root, children = span_tree(trace)
    stack = [(root, None)]
    while stack:
        span, parent = stack.pop()
        yield span, parent
        for child in reversed(children.get(span["span_id"], [])):
            stack.append((child, span))


def as_dict(value) -> dict:
    return value if isinstance(value, dict) else {"value": value}


def dt(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)


def trace_metadata(trace: dict) -> dict:
    return {k: trace.get(k) for k in ("run_id", "case_id", "config", "mode", "model")} | {
        "local_trace_id": trace.get("trace_id"), "source": "agenteval-replay"}


def span_metadata(span: dict) -> dict:
    meta = {"kind": span["kind"], "latency_ms": span["latency_ms"], **span["attributes"]}
    if span.get("usage"):
        meta["usage"] = span["usage"]
    return meta


class Ledger:
    def __init__(self, path: Path = LEDGER):
        self.path = path
        self.data = json.loads(path.read_text()) if path.is_file() else {}

    def used(self, provider: str, kind: str) -> int:
        return self.data.get(provider, {}).get(kind, 0)

    def reserve(self, provider: str, kind: str, n: int, cap: int) -> None:
        used = self.used(provider, kind)
        if used + n > cap:
            raise SystemExit(f"{provider}: {kind} cap reached ({used}/{cap} used, {n} requested). "
                             f"Edit or delete {self.path} only if you deliberately want more uploads.")
        self.data.setdefault(provider, {})[kind] = used + n
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))
