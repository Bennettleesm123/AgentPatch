"""Deterministic checks. Each result says passed / failed / can't verify (passed=None) and lists the
step ids that support it, so a failure always points at trace evidence.
"""

from __future__ import annotations

from typing import Any

from .schema import Trace

CHECK_TYPES = {
    "completes": "the run finishes without an unhandled error",
    "output": "a field of the final output equals / contains / is one of / exists",
    "tool_called": "a tool is called (min/max times)",
    "tool_not_called": "a tool is never called",
    "tool_order": "tools are first called in this order",
    "no_tool_errors": "no tool call ends in an error (even if later retried)",
    "max_latency_ms": "total run time stays under a limit",
    "max_tool_calls": "number of tool calls (optionally of one tool) stays under a limit",
    "max_llm_calls": "number of model calls stays under a limit",
}
_PARAMS = {
    "completes": set(), "no_tool_errors": set(),
    "output": {"path", "equals", "not_equals", "contains", "one_of", "exists"},
    "tool_called": {"tool", "min", "max"}, "tool_not_called": {"tool"}, "tool_order": {"sequence"},
    "max_latency_ms": {"value"}, "max_tool_calls": {"value", "tool"}, "max_llm_calls": {"value"},
}
_REQUIRED = {"output": {"path"}, "tool_called": {"tool"}, "tool_not_called": {"tool"}, "tool_order": {"sequence"},
             "max_latency_ms": {"value"}, "max_tool_calls": {"value"}, "max_llm_calls": {"value"}}
_OPS = ("equals", "not_equals", "contains", "one_of", "exists")


def validate_check(ch: Any) -> dict:
    if not isinstance(ch, dict) or "type" not in ch:
        raise ValueError(f"each check needs a `type` (one of {', '.join(CHECK_TYPES)}); got {ch!r}")
    t = ch["type"]
    if t not in CHECK_TYPES:
        raise ValueError(f"unknown check type {t!r}; supported: {', '.join(CHECK_TYPES)}")
    extra = set(ch) - _PARAMS[t] - {"type", "id", "description"}
    if extra:
        raise ValueError(f"check {t!r} doesn't take {sorted(extra)}; it takes {sorted(_PARAMS[t]) or 'no options'}")
    missing = _REQUIRED.get(t, set()) - set(ch)
    if missing:
        raise ValueError(f"check {t!r} needs {sorted(missing)}")
    if t == "output" and sum(op in ch for op in _OPS) != 1:
        raise ValueError(f"output check on {ch['path']!r} needs exactly one of {', '.join(_OPS)}")
    if t == "tool_order" and (not isinstance(ch["sequence"], list) or len(ch["sequence"]) < 2):
        raise ValueError("tool_order needs a `sequence` of at least two tool names")
    return dict(ch)


def check_id(ch: dict) -> str:
    if ch.get("id"):
        return ch["id"]
    t = ch["type"]
    if t == "output":
        op = next(op for op in _OPS if op in ch)
        return f"output:{ch['path']}:{op}"
    if t in ("tool_called", "tool_not_called"):
        return f"{t}:{ch['tool']}"
    if t == "tool_order":
        return "tool_order:" + ">".join(ch["sequence"])
    if t == "max_tool_calls" and ch.get("tool"):
        return f"max_tool_calls:{ch['tool']}"
    return t


def describe(ch: dict) -> str:
    if ch.get("description"):
        return ch["description"]
    t = ch["type"]
    if t == "output":
        op = next(op for op in _OPS if op in ch)
        val = "" if op == "exists" else f" {ch[op]!r}"
        return f"output `{ch['path']}` {op.replace('_', ' ')}{val}"
    if t == "tool_called":
        rng = f" at least {ch.get('min', 1)}x" + (f", at most {ch['max']}x" if "max" in ch else "")
        return f"`{ch['tool']}` is called{rng}"
    if t == "tool_not_called":
        return f"`{ch['tool']}` is never called"
    if t == "tool_order":
        return "tools called in order " + " → ".join(f"`{x}`" for x in ch["sequence"])
    if t == "max_latency_ms":
        return f"run takes at most {ch['value']} ms"
    if t == "max_tool_calls":
        return f"at most {ch['value']} {'`' + ch['tool'] + '` ' if ch.get('tool') else ''}tool calls"
    if t == "max_llm_calls":
        return f"at most {ch['value']} model calls"
    return CHECK_TYPES[t]


def get_path(data: Any, path: str) -> tuple[bool, Any]:
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.lstrip("-").isdigit() and -len(cur) <= int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return False, None
    return True, cur


def _result(ch, passed, detail, evidence=()):
    return {"id": check_id(ch), "type": ch["type"], "description": describe(ch), "passed": passed,
            "detail": detail, "evidence": list(evidence)}


def run_check(ch: dict, trace: Trace, unrecorded: dict[str, int] | None = None) -> dict:
    """`unrecorded` = tool name -> number of model-requested calls with no recorded tool step."""
    unrecorded = unrecorded or {}
    t = ch["type"]
    root = trace.root
    root_ev = [root.id] if root else []

    def blind(tools):
        hit = {x: unrecorded[x] for x in tools if unrecorded.get(x)}
        if hit:
            return _result(ch, None, "can't verify: the model requested " + ", ".join(
                f"`{k}` {v}x" for k, v in hit.items()) + " but no matching tool step was recorded "
                "(see observability warnings)", [])
        return None

    if t == "completes":
        if trace.status == "ok":
            return _result(ch, True, "run completed", root_ev)
        origins = trace.error_origins()
        return _result(ch, False, f"run failed: {trace.error}", ([origins[-1].id] if origins else []) + root_ev)

    if t == "output":
        found, val = get_path(trace.output, ch["path"])
        shown = repr(val) if found else "missing"
        if "exists" in ch:
            ok = found == bool(ch["exists"])
        elif not found:
            ok = False
        elif "equals" in ch:
            ok = val == ch["equals"]
        elif "not_equals" in ch:
            ok = val != ch["not_equals"]
        elif "contains" in ch:
            ok = isinstance(val, (str, list, dict)) and ch["contains"] in val
        else:
            ok = val in ch["one_of"]
        if trace.status != "ok" and not found:
            return _result(ch, False, f"no output because the run failed ({trace.error})", root_ev)
        return _result(ch, ok, f"`{ch['path']}` is {shown}", root_ev)

    if t == "tool_called":
        calls = trace.tool_steps(ch["tool"])
        lo, hi = ch.get("min", 1), ch.get("max")
        ok = len(calls) >= lo and (hi is None or len(calls) <= hi)
        if not ok and (b := blind([ch["tool"]])):
            return b
        return _result(ch, ok, f"`{ch['tool']}` called {len(calls)}x", [s.id for s in calls] or root_ev)

    if t == "tool_not_called":
        calls = trace.tool_steps(ch["tool"])
        if calls:
            return _result(ch, False, f"`{ch['tool']}` was called {len(calls)}x", [s.id for s in calls])
        return blind([ch["tool"]]) or _result(ch, True, f"`{ch['tool']}` not called", [])

    if t == "tool_order":
        firsts = [(name, next(iter(trace.tool_steps(name)), None)) for name in ch["sequence"]]
        missing = [n for n, s in firsts if s is None]
        if missing:
            return blind(missing) or _result(ch, False, "never called: " + ", ".join(f"`{m}`" for m in missing),
                                             [s.id for _, s in firsts if s])
        for (a, sa), (b, sb) in zip(firsts, firsts[1:]):
            if trace.order[sa.id] > trace.order[sb.id]:
                return _result(ch, False, f"`{b}` ({sb.id}) was called before `{a}` ({sa.id})", [sb.id, sa.id])
        return blind(ch["sequence"]) or _result(ch, True, "in order", [s.id for _, s in firsts])

    if t == "no_tool_errors":
        errs = [s for s in trace.tool_steps() if s.status == "error"]
        if errs:
            return _result(ch, False, f"{len(errs)} tool call(s) failed: " + "; ".join(
                f"{s.id}: {s.error}" for s in errs[:3]), [s.id for s in errs])
        return _result(ch, True, "no tool errors", [])

    if t == "max_latency_ms":
        d = trace.duration_ms
        if d is None:
            return _result(ch, None, "run duration not recorded", root_ev)
        return _result(ch, d <= ch["value"], f"took {d:.0f} ms (limit {ch['value']})", root_ev)

    if t == "max_tool_calls":
        calls = trace.tool_steps(ch.get("tool"))
        n = len(calls)
        if n <= ch["value"] and (b := blind([ch["tool"]] if ch.get("tool") else list(unrecorded))):
            return b
        return _result(ch, n <= ch["value"], f"{n} tool calls (limit {ch['value']})",
                       [s.id for s in calls[ch["value"]:]] if n > ch["value"] else [])

    if t == "max_llm_calls":
        calls = trace.of_kind("llm")
        return _result(ch, len(calls) <= ch["value"], f"{len(calls)} model calls (limit {ch['value']})",
                       [s.id for s in calls[ch["value"]:]] if len(calls) > ch["value"] else [])

    raise ValueError(f"unknown check type {t!r}")


def evaluate_trace(trace: Trace, checks: list[dict], unrecorded: dict[str, int] | None = None) -> dict:
    results = [run_check(ch, trace, unrecorded) for ch in checks]
    if any(r["passed"] is False for r in results):
        outcome = "fail"
    elif any(r["passed"] is None for r in results):
        outcome = "unknown"
    else:
        outcome = "pass"
    return {"case_id": trace.case_id, "trial": trace.trial, "outcome": outcome, "checks": results}
