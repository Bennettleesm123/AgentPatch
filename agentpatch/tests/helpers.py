"""Build small synthetic traces for unit tests."""

from agentpatch.schema import Trace, finalize_steps


def S(key, kind, name, parent="root", start=0.0, end=1.0, **kw):
    return {"key": key, "kind": kind, "name": name, "parent_key": parent, "start_ms": start, "end_ms": end, **kw}


def make_trace(*steps, case_id="c", trial=0, status="ok", error=None, output=None, duration=None, input=None):
    end = max([s["end_ms"] or 0 for s in steps] + [1.0])
    raw = [{"key": "root", "kind": "agent", "name": "agent", "parent_key": None, "start_ms": 0.0,
            "end_ms": duration if duration is not None else end,
            **({"status": "error", "error": error} if status == "error" else {})}] + list(steps)
    return Trace(trace_id=f"{case_id}-{trial}", case_id=case_id, trial=trial, input=input, output=output,
                 status=status, error=error, duration_ms=duration if duration is not None else end,
                 steps=finalize_steps(raw))


def llm(key, requested=(), parent="root", start=0.0, end=1.0, usage=True, model="m"):
    attrs = {"model": model, "requested_tools": list(requested)}
    if usage:
        attrs["usage"] = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    return S(key, "llm", model, parent, start, end, attributes=attrs)


def tool(key, name, parent="root", start=0.0, end=1.0, error=None, input=None, output=None):
    extra = {"input": {"q": 1} if input is None else input, "output": "ok" if output is None else output}
    if error:
        extra.update(status="error", error=error, output=None)
    return S(key, "tool", name, parent, start, end, **extra)
