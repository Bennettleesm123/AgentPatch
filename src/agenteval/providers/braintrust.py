"""Braintrust via the `braintrust` SDK (init_logger + start_span for traces, Eval for experiments).

Docs: https://www.braintrust.dev/docs/guides/traces/customize
      https://www.braintrust.dev/docs/evaluate/write-scorers
      https://www.braintrust.dev/docs/evaluate/compare-experiments
"""

from __future__ import annotations

import os

from . import dataset_items, score, task_output
from .base import span_metadata, trace_metadata, walk

NAME = "braintrust"
ENV = ("BRAINTRUST_API_KEY",)
DOCS = "https://www.braintrust.dev/docs"
SPAN_TYPES = {"agent": "task", "llm": "llm", "tool": "tool", "tool_attempt": "tool"}


def _project() -> str:
    return os.environ.get("BRAINTRUST_PROJECT", "agenteval-lab")


def _log_kwargs(span: dict, trace: dict) -> dict:
    meta = span_metadata(span)
    if span["parent_id"] is None:
        meta |= trace_metadata(trace)
    kw = {"input": span["input"], "output": span["output"], "metadata": meta}
    if span["kind"] == "llm":
        u = span["usage"] or {}
        meta["model"] = span["attributes"].get("gen_ai.response.model")
        kw["metrics"] = {"prompt_tokens": u.get("input_tokens", 0), "completion_tokens": u.get("output_tokens", 0),
                         "tokens": u.get("total_tokens", 0)}
    if span["kind"] in ("tool", "tool_attempt"):
        meta["tool_name"] = span["attributes"].get("gen_ai.tool.name", span["name"])
    if span["error"]:
        kw["error"] = span["error"]
    return kw


def plan(trace: dict) -> list[dict]:
    return [{"call": "logger.start_span" if p is None else "span.start_span", "parent": p and p["name"],
             "name": s["name"], "type": SPAN_TYPES.get(s["kind"], "function"), "start_time": s["start_ns"] / 1e9,
             "end_time": s["end_ns"] / 1e9, "error": s["error"], "metrics": _log_kwargs(s, trace).get("metrics")}
            for s, p in walk(trace)]


def upload(trace: dict) -> str:
    import braintrust

    logger = braintrust.init_logger(project=_project(), set_current=False)
    root = _replay(trace, logger)
    logger.flush()
    return f"project={_project()} root_span_id={getattr(root, 'root_span_id', root.id)}"


def _replay(trace: dict, owner):
    """Log `trace` as spans under `owner` (a Logger for a new trace, or a Span to nest under)."""
    open_spans, order = {}, []
    for span, parent in walk(trace):
        parent_obj = owner if parent is None else open_spans[parent["span_id"]]
        bt = parent_obj.start_span(name=span["name"], type=SPAN_TYPES.get(span["kind"], "function"),
                                   start_time=span["start_ns"] / 1e9, set_current=False)
        bt.log(**_log_kwargs(span, trace))
        open_spans[span["span_id"]] = bt
        order.append(span)
    for span in reversed(order):  # children close before parents
        open_spans[span["span_id"]].end(end_time=span["end_ns"] / 1e9)
    return open_spans[order[0]["span_id"]]


def native_eval(run_fn, configs: list[str], case_ids: list[str]) -> dict:
    """run_fn(config, case_id) -> (record, trace) re-runs the offline agent inside each eval task."""
    import braintrust
    from braintrust import Eval, Score

    from ..evals import EVALUATORS

    def make(name):
        def scorer(input, output, expected=None, **kwargs):
            r = score(name, output)
            return Score(name=name, score=r["score"], metadata={"detail": r["detail"]})
        scorer.__name__ = name
        return scorer

    def task_for(config):
        def task(input):
            record, trace = run_fn(config, input["case_id"])
            _replay(trace, braintrust.current_span())
            return task_output(record, trace)
        return task

    data = dataset_items(case_ids)
    out, base_name = {}, None
    for config in configs:
        res = Eval(_project(), data=lambda: data, task=task_for(config), scores=[make(n) for n in EVALUATORS],
                   experiment_name=f"native-{config}", max_concurrency=1,
                   metadata={"config": config, "model": "scripted-v1 (offline)"}, base_experiment_name=base_name)
        base_name = res.summary.experiment_name
        out[config] = {"experiment": base_name, "url": res.summary.experiment_url}
    return {"project": _project(), "experiments": out,
            "compare": "Second experiment is compared against the first (base_experiment_name); see its UI diff."}
