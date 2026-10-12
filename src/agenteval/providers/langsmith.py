"""LangSmith via the `langsmith` SDK (RunTree for traces, Client.evaluate for experiments).

Docs: https://docs.langchain.com/langsmith/annotate-code
      https://docs.langchain.com/langsmith/log-llm-trace
      https://docs.langchain.com/langsmith/evaluate-llm-application
"""

from __future__ import annotations

import os

from . import dataset_items, score
from .base import as_dict, dt, span_metadata, trace_metadata, walk

NAME = "langsmith"
ENV = ("LANGSMITH_API_KEY",)
DOCS = "https://docs.langchain.com/langsmith"
RUN_TYPES = {"agent": "chain", "llm": "llm", "tool": "tool", "tool_attempt": "tool"}
DATASET = "agenteval-lead-research"


def _project() -> str:
    return os.environ.get("LANGSMITH_PROJECT", "agenteval-lab")


def _run_kwargs(span: dict, trace: dict) -> dict:
    meta = span_metadata(span)
    if span["parent_id"] is None:
        meta |= trace_metadata(trace)
    if span["kind"] == "llm":
        meta |= {"ls_provider": span["attributes"].get("gen_ai.system"),
                 "ls_model_name": span["attributes"].get("gen_ai.response.model")}
    return {"name": span["name"], "run_type": RUN_TYPES.get(span["kind"], "chain"), "inputs": as_dict(span["input"]),
            "start_time": dt(span["start_ns"]), "extra": {"metadata": meta},
            "tags": [trace["config"], trace["case_id"], trace["mode"]]}


def plan(trace: dict) -> list[dict]:
    rows = []
    for span, parent in walk(trace):
        kw = _run_kwargs(span, trace)
        rows.append({"call": "RunTree" if parent is None else "create_child", "parent": parent and parent["name"],
                     "name": kw["name"], "run_type": kw["run_type"], "start_time": kw["start_time"].isoformat(),
                     "end_time": dt(span["end_ns"]).isoformat(), "error": span["error"],
                     "usage_metadata": span["usage"] if span["kind"] == "llm" else None})
    return rows


def upload(trace: dict) -> str:
    from langsmith import Client
    from langsmith.run_trees import RunTree

    client = Client()
    runs, root = {}, None
    for span, parent in walk(trace):
        kw = _run_kwargs(span, trace)
        if parent is None:
            rt = root = RunTree(**kw, project_name=_project(), ls_client=client)
        else:
            rt = runs[parent["span_id"]].create_child(**kw)
        if span["kind"] == "llm" and span["usage"]:
            rt.set(usage_metadata={k: span["usage"][k] for k in ("input_tokens", "output_tokens", "total_tokens")
                                   if k in span["usage"]})
        rt.end(outputs=as_dict(span["output"]), error=span["error"], end_time=dt(span["end_ns"]))
        runs[span["span_id"]] = rt
    root.post(exclude_child_runs=False)
    client.flush()
    return f"project={_project()} root_run_id={root.id}"


def native_eval(records: dict[str, dict[str, dict]], case_ids: list[str]) -> dict:
    """records: {config: {case_id: run_record}}. Replays recorded outputs; no model calls."""
    from langsmith import Client

    client = Client()
    if client.has_dataset(dataset_name=DATASET):
        ds = client.read_dataset(dataset_name=DATASET)
    else:
        ds = client.create_dataset(dataset_name=DATASET, description="AgentEvalLab synthetic lead-research cases")
        client.create_examples(dataset_id=ds.id, examples=[
            {"inputs": i["input"], "outputs": i["expected"], "metadata": i["metadata"]} for i in dataset_items(case_ids)])

    def make(name):
        def evaluator(inputs: dict, outputs: dict) -> dict:
            r = score(name, outputs)
            return {"key": name, "score": r["score"], "comment": r["detail"]}
        evaluator.__name__ = name
        return evaluator

    from ..evals import EVALUATORS

    out = {}
    for config, by_case in records.items():
        res = client.evaluate(lambda inputs, _b=by_case: _b[inputs["case_id"]], data=DATASET,
                              evaluators=[make(n) for n in EVALUATORS], experiment_prefix=f"native-{config}",
                              metadata={"config": config, "replay": "recorded offline outputs"}, max_concurrency=1)
        out[config] = res.experiment_name
    client.flush()
    return {"dataset": DATASET, "experiments": out,
            "compare": "UI: Datasets & Experiments -> dataset -> select both experiments -> Compare"}
