"""Synthetic fixtures: real offline agenteval traces plus small, declarative mutations.

Base traces come from running the existing scripted agent (no network, no model calls). Mutations
add the events the sample agent doesn't emit (approvals, noise) or damage the trace (dropped spans,
incomplete flag) so the evaluator can be tested against known answers.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from functools import lru_cache
from pathlib import Path

from agenteval.agent import run_case
from agenteval.configs import load_config
from agenteval.data import CASES_BY_ID
from agenteval.models import build_model

from .events import build_events, get_path
from .rules import ENTITY_PATHS


@lru_cache(maxsize=None)
def _base_json(agent_case: str, config: str) -> str:
    cfg = load_config(config)
    with tempfile.TemporaryDirectory() as tmp:
        rec = run_case(CASES_BY_ID[agent_case], cfg, build_model("offline", cfg), "offline", out_dir=Path(tmp))
        return Path(rec["trace_path"]).read_text()


def base_trace(agent_case: str, config: str) -> dict:
    return json.loads(_base_json(agent_case, config))


def _span_for(trace: dict, event_id: str) -> dict:
    log = build_events(trace)
    if event_id not in log.by_id:
        raise KeyError(f"mutation target {event_id!r} not in trace")
    sid = log.by_id[event_id].span_id
    return next(s for s in trace["spans"] if s["span_id"] == sid)


def _root(trace: dict) -> dict:
    return next(s for s in trace["spans"] if s.get("parent_id") is None)


def _new_span(trace: dict, target: dict, position: str, offset: int, name: str, kind: str, **fields) -> dict:
    start = target["start_ns"] - offset if position == "before" else target["end_ns"] + offset
    sid = hashlib.sha1(f"{name}:{len(trace['spans'])}:{start}".encode()).hexdigest()[:16]
    span = {"span_id": sid, "parent_id": _root(trace)["span_id"], "name": name, "kind": kind,
            "start_ns": start, "end_ns": start, "start_time": target.get("start_time"),
            "end_time": target.get("start_time"), "latency_ms": 0.0, "status": "ok", "error": None,
            "input": None, "output": None, "usage": None, "attributes": {}}
    span.update(fields)
    trace["spans"].append(span)
    return span


def _insert_approval(trace, m):
    target = _span_for(trace, m["target"])
    op = m.get("operation", "save_assessment")
    entity = m.get("entity", "auto")
    if entity == "auto":
        tool = (target.get("attributes") or {}).get("gen_ai.tool.name") or target["name"].removeprefix("tool.")
        key, path = ENTITY_PATHS[tool]
        entity = {key: get_path(target.get("input"), path)}
    _new_span(trace, target, m.get("position", "before"), 1, "approval", "approval",
              input={"request": f"Approve {op}?"}, output={"decision": m.get("decision", "granted")},
              attributes={"approval.operation": op, "approval.entity": entity, "approval.scope": m.get("scope", "write"),
                          "approval.decision": m.get("decision", "granted")})


def _drop(trace, m):
    target = _span_for(trace, m["target"])
    drop = {target["span_id"]}
    if not m.get("keep_children", False):
        drop |= {s["span_id"] for s in trace["spans"] if s.get("parent_id") == target["span_id"]}
    trace["spans"] = [s for s in trace["spans"] if s["span_id"] not in drop]


def _mark_incomplete(trace, m):
    trace["integrity"] = {"complete": False, "reason": m.get("reason", "exporter dropped spans")}


def _insert_noise(trace, m):
    target = _span_for(trace, m["target"])
    for k in range(m.get("count", 1)):
        _new_span(trace, target, "before", 2 + k, m.get("name", "noise.heartbeat"), "other",
                  output={"ok": True}, attributes={"noise": True})


def _insert_event(trace, m):
    target = _span_for(trace, m["target"])
    fields = {k: m[k] for k in ("input", "output", "attributes", "status", "error") if k in m}
    _new_span(trace, target, m.get("position", "before"), m.get("offset", 3), m["name"], m.get("kind", "other"),
              **fields)


def _merge_output(trace, m):
    target = _span_for(trace, m["target"])
    out = target.get("output") if isinstance(target.get("output"), dict) else {}
    target["output"] = {**out, **m["fields"]}


MUTATIONS = {"insert_approval": _insert_approval, "drop": _drop, "mark_incomplete": _mark_incomplete,
             "insert_noise": _insert_noise, "insert_event": _insert_event, "merge_output": _merge_output}


def build_trace(spec: dict) -> dict:
    """spec = {"agent_case": ..., "config": ..., "mutations": [{"op": ..., ...}]}"""
    trace = copy.deepcopy(base_trace(spec["agent_case"], spec["config"]))
    for m in spec.get("mutations", []):
        if m["op"] not in MUTATIONS:
            raise ValueError(f"unknown mutation {m['op']!r}")
        MUTATIONS[m["op"]](trace, m)
    return trace
