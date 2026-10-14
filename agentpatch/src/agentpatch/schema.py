"""AgentPatch trace format: one trace = one agent execution, as a flat list of steps.

Step ids are "{kind}:{name}#{k}" (k-th step of that kind and name in start order). They are readable
and stay the same when the same agent is re-run, which lets reports and the dashboard point at
"tool:save_assessment#1" instead of a random span id.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = "agentpatch.trace.v1"
KINDS = ("agent", "node", "llm", "tool", "attempt", "router", "retriever", "other")

_SECRET_KEY = re.compile(r"(api[_-]?key|token|secret|password|authorization|cookie|credential)", re.I)
MAX_STR = 2000
MAX_ITEMS = 50
MAX_DEPTH = 8


@dataclass
class Step:
    id: str
    name: str
    kind: str
    parent_id: str | None
    start_ms: float
    end_ms: float | None
    status: str = "ok"
    error: str | None = None
    input: Any = None
    output: Any = None
    attributes: dict = field(default_factory=dict)

    @property
    def duration_ms(self) -> float | None:
        return None if self.end_ms is None else round(self.end_ms - self.start_ms, 3)

    @property
    def tool_name(self) -> str | None:
        return self.name if self.kind in ("tool", "attempt") else None


@dataclass
class Trace:
    trace_id: str
    case_id: str
    trial: int = 0
    input: Any = None
    output: Any = None
    status: str = "ok"
    error: str | None = None
    started_at: str | None = None
    duration_ms: float | None = None
    source: str = "sdk"
    steps: list[Step] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        self._index()

    def _index(self):
        self.by_id = {s.id: s for s in self.steps}
        self.order = {s.id: i for i, s in enumerate(self.steps)}

    @property
    def root(self) -> Step | None:
        return next((s for s in self.steps if s.parent_id is None and s.kind == "agent"), None)

    def of_kind(self, *kinds: str) -> list[Step]:
        return [s for s in self.steps if s.kind in kinds]

    def tool_steps(self, tool: str | None = None) -> list[Step]:
        return [s for s in self.steps if s.kind == "tool" and (tool is None or s.name == tool)]

    def children(self, step: Step) -> list[Step]:
        return [s for s in self.steps if s.parent_id == step.id]

    def error_origins(self) -> list[Step]:
        """Error steps none of whose children errored: where failures started, not where they bubbled up."""
        errs = [s for s in self.steps if s.status == "error" and s.kind != "agent"]
        parents = {s.parent_id for s in errs}
        return [s for s in errs if s.id not in parents]

    def sort_ids(self, ids) -> list[str]:
        return sorted(set(ids), key=lambda i: self.order.get(i, 1 << 30))

    def to_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k != "steps"}
        d = {"schema": SCHEMA, **d}
        d["steps"] = [asdict(s) for s in self.steps]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Trace":
        if d.get("schema") not in (None, SCHEMA):
            raise ValueError(f"not an AgentPatch trace (schema {d.get('schema')!r})")
        steps = [Step(**{k: s.get(k) for k in Step.__dataclass_fields__ if k in s}) for s in d.get("steps", [])]
        for s in steps:
            s.attributes = s.attributes or {}
            s.status = s.status or "ok"
        fields = {k: d.get(k) for k in cls.__dataclass_fields__ if k in d and k != "steps"}
        return cls(steps=steps, **fields)


def finalize_steps(raw: list[dict]) -> list[Step]:
    """Turn recorded steps (with internal keys) into ordered Steps with readable ids."""
    raw = sorted(enumerate(raw), key=lambda p: (p[1]["start_ms"], p[0]))
    counts: dict[tuple, int] = {}
    ids: dict[Any, str] = {}
    for _, r in raw:
        key = (r["kind"], r["name"])
        counts[key] = counts.get(key, 0) + 1
        ids[r["key"]] = f"{r['kind']}:{r['name']}#{counts[key]}"
    steps = []
    for _, r in raw:
        parent = r.get("parent_key")
        steps.append(Step(
            id=ids[r["key"]], name=r["name"], kind=r["kind"],
            parent_id=ids.get(parent, None if parent is None else f"missing:{parent}"),
            start_ms=round(r["start_ms"], 3), end_ms=None if r.get("end_ms") is None else round(r["end_ms"], 3),
            status=r.get("status", "ok"), error=r.get("error"), input=r.get("input"), output=r.get("output"),
            attributes=r.get("attributes") or {}))
    return steps


def _secret_values() -> list[str]:
    return [v for k, v in os.environ.items()
            if v and len(v) >= 8 and any(t in k.upper() for t in ("KEY", "TOKEN", "SECRET", "PASSWORD"))]


def redact_text(text: str) -> str:
    for v in _secret_values():
        text = text.replace(v, "[REDACTED]")
    return text


def jsonable(value: Any, depth: int = 0) -> Any:
    """Best-effort conversion of agent inputs/outputs to bounded, secret-free JSON."""
    if depth > MAX_DEPTH:
        return "…"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        value = redact_text(value)
        return value if len(value) <= MAX_STR else value[:MAX_STR] + f"… [{len(value) - MAX_STR} more chars]"
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, dict):
        out = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= MAX_ITEMS:
                out["…"] = f"{len(value) - MAX_ITEMS} more keys"
                break
            k = str(k)
            out[k] = "[REDACTED]" if _SECRET_KEY.search(k) and isinstance(v, str) else jsonable(v, depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        out = [jsonable(v, depth + 1) for v in items[:MAX_ITEMS]]
        if len(items) > MAX_ITEMS:
            out.append(f"… {len(items) - MAX_ITEMS} more items")
        return out
    msg = _message(value, depth)
    if msg is not None:
        return msg
    for attr in ("model_dump", "dict", "_asdict"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return jsonable(fn(), depth + 1)
            except Exception:
                pass
    if hasattr(value, "__dataclass_fields__"):
        return jsonable(asdict(value), depth + 1)
    return jsonable(repr(value), depth + 1)


def _message(value: Any, depth: int):
    """LangChain-style chat messages -> compact dicts (without importing LangChain)."""
    mtype = getattr(value, "type", None)
    if not isinstance(mtype, str) or not hasattr(value, "content"):
        return None
    out = {"role": mtype, "content": jsonable(value.content, depth + 1)}
    for attr in ("name", "tool_call_id", "status"):
        v = getattr(value, attr, None)
        if v:
            out[attr] = v
    calls = getattr(value, "tool_calls", None)
    if calls:
        out["tool_calls"] = [{"name": c.get("name"), "args": jsonable(c.get("args"), depth + 2), "id": c.get("id")}
                             for c in calls]
    return out


def write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str))
    tmp.replace(path)
    return path


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text())
