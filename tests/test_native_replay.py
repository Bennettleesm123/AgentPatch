"""Nested replay of an agent trace under a provider's eval-task span, using fakes (no network)."""

import pytest

from agenteval.providers import braintrust as bt_adapter
from agenteval.providers import opik as opik_adapter
from agenteval.providers import score, task_output
from agenteval.tracing import load_trace

from test_agent import run


class FakeNode:
    def __init__(self, name="task", **kw):
        self.name, self.kw, self.children, self.logged, self.ended = name, kw, [], {}, None
        self.id = f"id-{name}"

    def start_span(self, name, **kw):  # Braintrust
        child = FakeNode(name, **kw)
        self.children.append(child)
        return child

    def span(self, name, **kw):  # Opik
        return self.start_span(name, **kw)

    def log(self, **kw):
        self.logged.update(kw)

    def end(self, end_time=None):
        self.ended = end_time

    def count(self):
        return 1 + sum(c.count() for c in self.children)


class FakeOpikClient:
    def __init__(self):
        self.roots = []

    def span(self, trace_id, parent_span_id, name, **kw):
        node = FakeNode(name, trace_id=trace_id, parent_span_id=parent_span_id, **kw)
        self.roots.append(node)
        return node


@pytest.fixture
def noisy(out_dir):
    rec = run("noisy", out_dir=out_dir)
    return rec, load_trace(rec["trace_path"])


def test_noisy_case_passes_and_contains_needle(noisy):
    from agenteval.evals import evaluate_record

    rec, trace = noisy
    assert evaluate_record(rec)["passed"] and rec["tool_attempts"] == 8
    out = task_output(rec, trace)
    assert len(out["summary"]["tool_errors"]) == 5
    assert sum("429" in e for e in out["summary"]["tool_errors"]) == 1
    assert score("retry_limits", out)["passed"]


def test_braintrust_nests_full_trace_under_task_span(noisy):
    _, trace = noisy
    task = FakeNode()
    agent = bt_adapter._replay(trace, task)
    assert task.children == [agent] and agent.name == "agent.lead_research"
    assert agent.count() == len(trace["spans"])
    assert agent.ended is not None and all(c.ended is not None for c in agent.children)


def test_opik_nests_full_trace_under_task_span(noisy):
    _, trace = noisy
    client = FakeOpikClient()
    agent = opik_adapter._replay(trace, client, trace_id="t1", parent_span_id="s1")
    assert client.roots == [agent] and agent.kw["parent_span_id"] == "s1"
    assert agent.count() == len(trace["spans"])
    llm = [c for c in agent.children if c.kw["type"] == "llm"]
    assert llm and llm[0].kw["usage"]["total_tokens"] > 0


def test_langsmith_nests_full_trace_under_parent_run(noisy, monkeypatch):
    pytest.importorskip("langsmith")
    from langsmith.run_trees import RunTree

    from agenteval.providers import langsmith as ls_adapter

    posted = []
    monkeypatch.setattr(RunTree, "post", lambda self, exclude_child_runs=True: posted.append(self))
    _, trace = noisy
    parent = RunTree(name="target", run_type="chain", inputs={}, start_time=ls_adapter.dt(trace["spans"][0]["start_ns"]))
    agent = ls_adapter._replay(trace, parent=parent)
    assert posted == [agent] and agent.parent_run_id == parent.id

    def count(r):
        return 1 + sum(count(c) for c in r.child_runs)

    assert count(agent) == len(trace["spans"])
    assert sum(1 for c in agent.child_runs if c.error) == 0  # tool spans recovered; attempts carry errors
