"""Opik via the `opik` SDK (client.trace / trace.span for traces, opik.evaluation.evaluate for experiments).

Docs: https://www.comet.com/docs/opik/tracing/log_traces
      https://www.comet.com/docs/opik/evaluation/evaluate_your_llm
"""

from __future__ import annotations

import os

from . import dataset_items, score, task_output
from .base import as_dict, dt, span_metadata, trace_metadata, walk

NAME = "opik"
ENV = ("OPIK_API_KEY",)
DOCS = "https://www.comet.com/docs/opik"
SPAN_TYPES = {"llm": "llm", "tool": "tool", "tool_attempt": "tool"}
PROVIDER_NAMES = {"gemini": "google_ai"}
DATASET = "agenteval-lead-research"


def _project() -> str:
    return os.environ.get("OPIK_PROJECT_NAME", "agenteval-lab")


def _error_info(span: dict):
    if not span["error"]:
        return None
    etype, _, msg = span["error"].partition(": ")
    return {"exception_type": etype, "message": msg or span["error"], "traceback": ""}


def _span_kwargs(span: dict) -> dict:
    kw = {"name": span["name"], "type": SPAN_TYPES.get(span["kind"], "general"),
          "start_time": dt(span["start_ns"]), "end_time": dt(span["end_ns"]), "input": as_dict(span["input"]),
          "output": as_dict(span["output"]), "metadata": span_metadata(span), "error_info": _error_info(span),
          "provider": None}
    if span["kind"] == "llm":
        u = span["usage"] or {}
        system = span["attributes"].get("gen_ai.system")
        kw |= {"model": span["attributes"].get("gen_ai.response.model"),
               "provider": PROVIDER_NAMES.get(system, system),
               "usage": {"prompt_tokens": u.get("input_tokens", 0), "completion_tokens": u.get("output_tokens", 0),
                         "total_tokens": u.get("total_tokens", 0)}}
    return kw


def plan(trace: dict) -> list[dict]:
    rows = []
    for span, parent in walk(trace):
        call = "client.trace" if parent is None else ("trace.span" if parent["parent_id"] is None else "span.span")
        kw = _span_kwargs(span)
        rows.append({"call": call, "parent": parent and parent["name"], "name": kw["name"],
                     "type": None if parent is None else kw["type"], "start_time": kw["start_time"].isoformat(),
                     "end_time": kw["end_time"].isoformat(), "error_info": kw["error_info"], "usage": kw.get("usage")})
    return rows


def upload(trace: dict) -> str:
    import opik

    client = opik.Opik(project_name=_project())
    root = _replay(trace, client)
    client.flush()
    return f"project={_project()} trace_id={root.id}"


def _replay(trace: dict, client, trace_id: str | None = None, parent_span_id: str | None = None):
    """Log `trace` as a new Opik trace, or as spans under an existing trace/span when trace_id is given."""
    objs, root = {}, None
    for span, parent in walk(trace):
        kw = _span_kwargs(span)
        if parent is None and trace_id is None:
            obj = root = client.trace(name=kw["name"], start_time=kw["start_time"], end_time=kw["end_time"],
                                      input=kw["input"], output=kw["output"], error_info=kw["error_info"],
                                      metadata=kw["metadata"] | trace_metadata(trace),
                                      tags=[trace["config"], trace["case_id"], trace["mode"]])
        elif parent is None:
            obj = root = client.span(trace_id=trace_id, parent_span_id=parent_span_id,
                                     **kw | {"metadata": kw["metadata"] | trace_metadata(trace)})
        else:
            obj = objs[parent["span_id"]].span(**kw)
        objs[span["span_id"]] = obj
    return root


def native_eval(run_fn, configs: list[str], case_ids: list[str]) -> dict:
    """run_fn(config, case_id) -> (record, trace) re-runs the offline agent inside each eval task."""
    import opik
    from opik import opik_context
    from opik.evaluation import evaluate
    from opik.evaluation.metrics import base_metric, score_result

    from ..evals import EVALUATORS

    client = opik.Opik(project_name=_project())
    ds = client.get_or_create_dataset(name=DATASET, description="AgentEvalLab synthetic lead-research cases")
    have = {(item.get("input") or {}).get("case_id") for item in ds.get_items()}
    missing = [i for i in dataset_items(case_ids) if i["input"]["case_id"] not in have]
    if missing:
        ds.insert([{"input": i["input"], "expected_output": i["expected"], **i["metadata"]} for i in missing])

    def task_for(config):
        def task(item: dict) -> dict:
            record, trace = run_fn(config, item["input"]["case_id"])
            span, tr = opik_context.get_current_span_data(), opik_context.get_current_trace_data()
            if span is not None:
                _replay(trace, client, trace_id=span.trace_id, parent_span_id=span.id)
            elif tr is not None:
                _replay(trace, client, trace_id=tr.id)
            return {"output": task_output(record, trace)}
        return task

    class LocalEvaluator(base_metric.BaseMetric):
        def score(self, output, **ignored):
            r = score(self.name, output)
            return score_result.ScoreResult(name=self.name, value=r["score"], reason=r["detail"])

    out = {}
    for config in configs:
        res = evaluate(dataset=ds, task=task_for(config), scoring_metrics=[LocalEvaluator(name=n, track=False) for n in EVALUATORS],
                       experiment_name=f"native-{config}", project_name=_project(),
                       experiment_config={"config": config, "model": "scripted-v1 (offline)"}, task_threads=1)
        out[config] = {"experiment": res.experiment_name, "url": res.experiment_url}
    client.flush()
    return {"dataset": DATASET, "experiments": out,
            "compare": "UI: Evaluation -> Experiments -> select both -> Compare"}
