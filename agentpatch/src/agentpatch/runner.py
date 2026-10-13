"""Run a suite against an agent (or import existing traces) and store the results as a run."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from . import recorder as rec_mod
from .checks import evaluate_trace
from .coverage import unrecorded_counts
from .recorder import Recorder
from .schema import Trace
from .store import Run, finish_run, new_run, save_trace
from .suite import Suite, SuiteError


class TargetError(ValueError):
    pass


def load_target(spec: str, base_dir: Path | None = None) -> Any:
    """`package.module:attr` or `path/to/file.py:attr`."""
    if ":" not in spec:
        raise TargetError(f"target {spec!r} must look like `module:attribute` (e.g. `my_agent:graph`)")
    mod_name, attr = spec.rsplit(":", 1)
    for p in (str(base_dir) if base_dir else None, str(Path.cwd())):
        if p and p not in sys.path:
            sys.path.insert(0, p)
    try:
        if mod_name.endswith(".py"):
            path = Path(mod_name)
            if not path.is_absolute() and base_dir:
                path = base_dir / path
            spec_ = importlib.util.spec_from_file_location(path.stem, path)
            if spec_ is None or not path.is_file():
                raise TargetError(f"target file not found: {path}")
            module = importlib.util.module_from_spec(spec_)
            sys.modules[path.stem] = module
            spec_.loader.exec_module(module)
        else:
            module = importlib.import_module(mod_name)
    except ModuleNotFoundError as exc:
        raise TargetError(f"couldn't import {mod_name!r} for target {spec!r}: {exc}. Run from the directory that "
                          "contains it, or use a file path like `agent.py:graph`.") from None
    except TargetError:
        raise
    except Exception as exc:
        hint = " (a missing environment variable? set it before running AgentPatch)" if isinstance(exc, KeyError) else ""
        raise TargetError(f"importing {mod_name!r} raised {type(exc).__name__}: {exc}{hint}") from None
    try:
        obj = module
        for part in attr.split("."):
            obj = getattr(obj, part)
    except AttributeError:
        raise TargetError(f"{mod_name!r} has no attribute {attr!r}") from None
    return obj


class Agent:
    """Uniform wrapper: invoke(input, recorder, case_id, trial, run_id) -> output."""

    def __init__(self, obj: Any, config: dict | None = None):
        from .integrations import langgraph_available

        self.graph = None
        self.fn: Callable | None = None
        self.config = dict(config or {})
        if langgraph_available() and _is_graph(obj):
            self.graph = obj
        elif callable(obj) and _zero_arg(obj):
            try:
                built = obj()
            except Exception as exc:
                name = getattr(obj, "__name__", "factory")
                raise TargetError(f"calling the target factory {name}() raised {type(exc).__name__}: {exc}") from None
            if langgraph_available() and _is_graph(built):
                self.graph = built
            elif callable(built):
                self.fn = built
            else:
                raise TargetError("the target factory returned neither a compiled LangGraph nor a callable")
        elif callable(obj):
            self.fn = obj
        else:
            raise TargetError(f"target is a {type(obj).__name__}; expected a compiled LangGraph, a factory returning "
                              "one, or a function that takes the case input")
        self.kind = "langgraph" if self.graph is not None else "python"

    def structure(self) -> dict | None:
        if self.graph is None:
            return None
        from .integrations.langgraph import graph_structure

        return graph_structure(self.graph)

    def invoke(self, input: Any, recorder: Recorder, case_id: str, trial: int, run_id: str) -> Any:
        if self.graph is not None:
            from .integrations.langgraph import run_graph

            extra = dict(self.config)
            cfg = {**extra, "configurable": {**(extra.get("configurable") or {}),
                                             "thread_id": f"agentpatch:{run_id}:{case_id}:{trial}"},
                   "metadata": {**(extra.get("metadata") or {}), "agentpatch_case": case_id,
                                "agentpatch_trial": trial}, "run_name": case_id}
            return run_graph(self.graph, input, recorder, cfg)
        token = rec_mod._current.set(recorder)
        ptoken = rec_mod._parent.set(recorder.root_key)
        try:
            return self.fn(input)
        finally:
            rec_mod._parent.reset(ptoken)
            rec_mod._current.reset(token)


def _is_graph(obj) -> bool:
    return hasattr(obj, "invoke") and hasattr(obj, "get_graph")


def _zero_arg(fn) -> bool:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return not any(p.default is p.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) for p in params)


def run_once(agent: Agent, input: Any, case_id: str = "adhoc", trial: int = 0, run_id: str = "adhoc") -> Trace:
    rec = Recorder(case_id, trial, input, source=agent.kind)
    try:
        out = agent.invoke(input, rec, case_id, trial, run_id)
    except Exception as exc:  # the agent failing is a result, not a crash
        return rec.finish(error=exc)
    return rec.finish(out)


def git_info(cwd: Path) -> dict | None:
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=cwd, capture_output=True, text=True,
                                timeout=5)
        if commit.returncode != 0:
            return None
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, timeout=5)
        return {"commit": commit.stdout.strip(), "dirty": bool(dirty.stdout.strip())}
    except (OSError, subprocess.SubprocessError):
        return None


def run_suite(suite: Suite, label: str, target: str | None = None, repeats: int | None = None,
              root: str | Path | None = None, cases: list[str] | None = None,
              progress: Callable[[str, int, dict], None] | None = None) -> Run:
    spec = target or suite.target
    if not spec:
        raise TargetError("no agent target: set [agent] target in the suite or pass --target module:attr")
    agent = Agent(load_target(spec, suite.base_dir), suite.agent_config)
    selected = [c for c in suite.cases if not cases or c.id in cases]
    if cases and len(selected) != len(set(cases)):
        missing = sorted(set(cases) - {c.id for c in selected})
        raise SuiteError(f"unknown case(s) {missing}; suite has {[c.id for c in suite.cases]}")
    no_input = [c.id for c in selected if not c.has_input]
    if no_input:
        raise SuiteError(f"case(s) {no_input} have no `input`, so they can only be imported, not run")
    n = repeats or suite.repeats
    run = new_run(label, {"suite": suite.name, "suite_path": str(suite.path) if suite.path else None,
                          "target": spec, "agent_kind": agent.kind, "repeats": n,
                          "cases": [c.id for c in selected], "graph": agent.structure(),
                          "git": git_info(suite.base_dir), "source": agent.kind,
                          "thresholds": suite.thresholds, "coverage_expectations": suite.coverage,
                          "checked_tools": sorted(suite.referenced_tools())}, root)
    results = []
    for case in selected:
        for trial in range(n):
            trace = run_once(agent, case.input, case.id, trial, run.id)
            save_trace(run, trace)
            res = evaluate_trace(trace, case.checks, unrecorded_counts(trace))
            res["duration_ms"] = trace.duration_ms
            results.append(res)
            if progress:
                progress(case.id, trial, res)
    return finish_run(run, results)


def import_traces(traces: list[Trace], label: str, suite: Suite | None = None, root: str | Path | None = None,
                  source: str = "import") -> Run:
    """Store existing traces as a run. With a suite, cases are matched by case id and checked."""
    seen: dict[str, int] = {}
    for t in traces:
        t.trial = seen.get(t.case_id, 0)
        seen[t.case_id] = t.trial + 1
    run = new_run(label, {"suite": suite.name if suite else None, "target": None, "agent_kind": source,
                          "repeats": max(seen.values(), default=0), "cases": sorted(seen), "graph": None,
                          "source": source, "thresholds": suite.thresholds if suite else None,
                          "coverage_expectations": suite.coverage if suite else {},
                          "checked_tools": sorted(suite.referenced_tools()) if suite else []}, root)
    results, unmatched = [], []
    for t in traces:
        save_trace(run, t)
        case = suite.case(t.case_id) if suite else None
        if case is None:
            if suite:
                unmatched.append(t.case_id)
            res = {"case_id": t.case_id, "trial": t.trial, "outcome": "pass" if t.status == "ok" else "fail",
                   "checks": [{"id": "completes", "type": "completes", "description": "the run finishes without an "
                               "unhandled error", "passed": t.status == "ok",
                               "detail": "run completed" if t.status == "ok" else f"run failed: {t.error}",
                               "evidence": [t.root.id] if t.root else []}]}
        else:
            res = evaluate_trace(t, case.checks, unrecorded_counts(t))
        res["duration_ms"] = t.duration_ms
        results.append(res)
    return finish_run(run, results, unmatched_cases=sorted(set(unmatched)))
