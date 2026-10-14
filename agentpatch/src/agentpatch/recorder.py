"""Recording API used by the LangGraph handler and by plain-Python agents.

    with agentpatch.trace("my_case", input=x) as rec:
        with agentpatch.step("plan", kind="node"):
            ...
        lookup(...)                 # functions decorated with @agentpatch.tool become tool steps
    rec.trace                        # -> agentpatch.schema.Trace
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import KINDS, Trace, finalize_steps, jsonable, redact_text, write_json

_current: contextvars.ContextVar["Recorder | None"] = contextvars.ContextVar("agentpatch_recorder", default=None)
_parent: contextvars.ContextVar[Any] = contextvars.ContextVar("agentpatch_parent", default=None)


def error_text(exc: BaseException | str | None) -> str | None:
    if exc is None:
        return None
    if isinstance(exc, BaseException):
        msg = str(exc)
        return redact_text(f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__)
    return redact_text(str(exc))


class Recorder:
    """Thread-safe collector of steps for one agent execution."""

    def __init__(self, case_id: str = "adhoc", trial: int = 0, input: Any = None, source: str = "sdk"):
        self.case_id, self.trial, self.source = case_id, trial, source
        self.input = jsonable(input)
        self.started_at = datetime.now(timezone.utc).isoformat()
        self._t0 = time.perf_counter_ns()
        self._lock = threading.Lock()
        self._steps: dict[Any, dict] = {}
        self._open: list[Any] = []
        self.root_key = "root"
        self._steps[self.root_key] = {"key": self.root_key, "name": "agent", "kind": "agent", "parent_key": None,
                                      "start_ms": 0.0, "end_ms": None, "input": self.input, "attributes": {}}
        self.trace: Trace | None = None
        self.meta: dict = {}

    def now_ms(self) -> float:
        return (time.perf_counter_ns() - self._t0) / 1e6

    def start(self, key: Any, name: str, kind: str, parent_key: Any = None, input: Any = None,
              attributes: dict | None = None) -> Any:
        if kind not in KINDS:
            kind = "other"
        with self._lock:
            if parent_key is None:
                parent_key = self._fallback_parent()
            self._steps[key] = {"key": key, "name": str(name), "kind": kind, "parent_key": parent_key,
                                "start_ms": self.now_ms(), "end_ms": None, "input": jsonable(input),
                                "attributes": jsonable(attributes or {})}
            self._open.append(key)
        return key

    def end(self, key: Any, output: Any = None, error: BaseException | str | None = None,
            attributes: dict | None = None) -> None:
        with self._lock:
            s = self._steps.get(key)
            if s is None or s["end_ms"] is not None:
                return
            s["end_ms"] = self.now_ms()
            if output is not None:
                s["output"] = jsonable(output)
            if error is not None:
                s["status"], s["error"] = "error", error_text(error)
            if attributes:
                s["attributes"].update(jsonable(attributes))
            if key in self._open:
                self._open.remove(key)

    def update(self, key: Any, attributes: dict) -> None:
        with self._lock:
            if key in self._steps:
                self._steps[key]["attributes"].update(jsonable(attributes))

    def set_kind(self, key: Any, kind: str) -> None:
        with self._lock:
            if key in self._steps and self._steps[key]["kind"] == "other":
                self._steps[key]["kind"] = kind

    def has(self, key: Any) -> bool:
        return key in self._steps

    def info(self, key: Any) -> dict | None:
        """name, kind, parent_key and attributes of a recorded step (a copy), or None."""
        with self._lock:
            s = self._steps.get(key)
            return None if s is None else {"name": s["name"], "kind": s["kind"], "parent_key": s["parent_key"],
                                           "attributes": dict(s["attributes"])}

    def _fallback_parent(self):
        """Innermost open node/agent step: used when the caller doesn't know its parent."""
        for k in reversed(self._open):
            if self._steps[k]["kind"] in ("node", "agent"):
                return k
        return self.root_key

    def finish(self, output: Any = None, error: BaseException | str | None = None) -> Trace:
        root = self._steps[self.root_key]
        root["end_ms"] = self.now_ms()
        if output is not None:
            root["output"] = jsonable(output)
        if error is not None:
            root["status"], root["error"] = "error", error_text(error)
        with self._lock:
            steps = finalize_steps(list(self._steps.values()))
        self.trace = Trace(trace_id=uuid.uuid4().hex[:16], case_id=self.case_id, trial=self.trial, input=self.input,
                           output=root.get("output"), status="error" if error is not None else "ok",
                           error=error_text(error), started_at=self.started_at,
                           duration_ms=round(root["end_ms"], 3), source=self.source, steps=steps, meta=self.meta)
        return self.trace

    def save(self, directory: str | Path = "agentpatch_traces") -> Path:
        """Write the finished trace as `<case>__<trial>.json`, ready for `agentpatch import <directory>`."""
        from .store import trace_filename

        if self.trace is None:
            raise RuntimeError("the trace isn't finished yet; call save() after the `with agentpatch.trace()` block")
        return write_json(Path(directory) / trace_filename(self.case_id, self.trial), self.trace.to_dict())


def current() -> Recorder | None:
    return _current.get()


@contextmanager
def trace(case_id: str = "adhoc", input: Any = None, trial: int = 0, source: str = "sdk"):
    """Record one agent execution. Exceptions are recorded on the trace and re-raised."""
    rec = Recorder(case_id, trial, input, source)
    token = _current.set(rec)
    ptoken = _parent.set(rec.root_key)
    result: dict = {}
    rec.set_output = lambda value: result.__setitem__("output", value)  # type: ignore[attr-defined]
    try:
        yield rec
    except BaseException as exc:
        rec.finish(result.get("output"), error=exc)
        raise
    else:
        rec.finish(result.get("output"))
    finally:
        _parent.reset(ptoken)
        _current.reset(token)


@contextmanager
def step(name: str, kind: str = "node", input: Any = None, attributes: dict | None = None):
    """Record a block of work as a step. A no-op when nothing is being recorded."""
    rec = _current.get()
    if rec is None:
        yield None
        return
    key = uuid.uuid4().hex
    rec.start(key, name, kind, parent_key=_parent.get(), input=input, attributes=attributes)
    token = _parent.set(key)
    handle = _StepHandle(rec, key)
    try:
        yield handle
    except BaseException as exc:
        rec.end(key, output=handle.output, error=exc)
        raise
    else:
        rec.end(key, output=handle.output)
    finally:
        _parent.reset(token)


class _StepHandle:
    def __init__(self, rec: Recorder, key: Any):
        self.rec, self.key, self.output = rec, key, None

    def set_output(self, value: Any) -> None:
        self.output = value

    def set_attributes(self, attrs: dict) -> None:
        self.rec.update(self.key, attrs)


def tool(fn=None, *, name: str | None = None):
    """Decorator: record each call of `fn` as a tool step with its arguments, result and error."""

    def wrap(f):
        tool_name = name or f.__name__
        sig = inspect.signature(f)

        @functools.wraps(f)
        def inner(*args, **kwargs):
            if _current.get() is None:
                return f(*args, **kwargs)
            try:
                bound = sig.bind_partial(*args, **kwargs)
                call_input = dict(bound.arguments)
            except TypeError:
                call_input = {"args": args, "kwargs": kwargs}
            with step(tool_name, kind="tool", input=call_input) as h:
                out = f(*args, **kwargs)
                h.set_output(out)
                return out

        return inner

    return wrap(fn) if fn is not None else wrap


def log_llm(model: str, input: Any = None, output: Any = None, tool_calls: list[dict] | None = None,
            usage: dict | None = None, duration_ms: float | None = None, error: BaseException | str | None = None):
    """Record a model call made outside LangChain (e.g. a raw provider SDK call)."""
    rec = _current.get()
    if rec is None:
        return
    key = uuid.uuid4().hex
    attrs = {"model": model}
    if tool_calls:
        attrs["requested_tools"] = [c.get("name") for c in tool_calls]
    if usage:
        attrs["usage"] = usage
    rec.start(key, model, "llm", parent_key=_parent.get(), input=input, attributes=attrs)
    if duration_ms:
        rec._steps[key]["start_ms"] = max(0.0, rec.now_ms() - duration_ms)
    out = output if not tool_calls else {"content": output, "tool_calls": tool_calls}
    rec.end(key, output=out, error=error)
