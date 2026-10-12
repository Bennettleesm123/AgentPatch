"""Provider adapters. Each module exposes: NAME, ENV, DOCS, plan(trace), upload(trace), native_eval(records)."""

from __future__ import annotations

import importlib

from ..data import CASES_BY_ID
from ..evals import EVALUATORS

PROVIDERS = ("langsmith", "opik", "braintrust", "raindrop")


class NativeEvalUnsupported(Exception):
    pass


def get_adapter(name: str):
    if name not in PROVIDERS:
        raise SystemExit(f"unknown provider {name!r}; choose from {', '.join(PROVIDERS)}")
    return importlib.import_module(f"{__name__}.{name}")


def dataset_items(case_ids: list[str]) -> list[dict]:
    """Identical dataset rows for every provider's native evaluation."""
    items = []
    for cid in case_ids:
        c = CASES_BY_ID[cid]
        e = c.expected
        items.append({
            "input": {"case_id": c.id, "company": c.company, "authorized": c.authorized},
            "expected": {"decision": e.decision, "company_id": e.company_id, "required_tools": list(e.required_tools),
                         "forbidden_tools": list(e.forbidden_tools), "expect_conflict": e.expect_conflict},
            "metadata": {"scenario": c.scenario},
        })
    return items


def score(evaluator: str, record: dict) -> dict:
    """Run one deterministic local evaluator inside a provider's eval harness."""
    return EVALUATORS[evaluator](record, CASES_BY_ID[record["case_id"]])
