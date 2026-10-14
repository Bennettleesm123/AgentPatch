import pytest
from pydantic import ValidationError

from behavtest.benchmark import load_cases
from behavtest.compiler import compile_requirement
from behavtest.rules import parse_rule

GT = load_cases()


@pytest.mark.parametrize("case", GT, ids=[c["id"] for c in GT])
def test_ground_truth_compiles(case):
    res = compile_requirement(case["requirement"])
    assert res.status == case["expected"]["status"], res.issues
    if res.status == "compiled":
        assert parse_rule(res.rule).semantic() == parse_rule(case["expected"]["rule"]).semantic()
    else:
        assert res.rule is None and res.issues


@pytest.mark.parametrize("text", [
    "Never call save_assessment on weekends.",
    "Never call save_assessment for a different company.",
    "Never call save_assessment twice for the same company.",
    "Get approval before calling save_assessment, unless the lead is small.",
    "__import__('os').system('rm -rf /')",
    "Call the tools in a sensible order.",
    "",
])
def test_abstains_instead_of_dropping_qualifiers(text):
    assert compile_requirement(text).status != "compiled"


def test_negated_order_is_reversed():
    res = compile_requirement("Never call get_company_details before search_company.")
    assert res.rule["sequence"] == ["search_company", "get_company_details"]


def test_attempts_convert_to_retries():
    assert compile_requirement("Allow at most 3 attempts for search_company.").rule["max_retries"] == 2


def test_rule_ids_are_stable():
    a = compile_requirement("Retry get_company_details at most once.")
    b = compile_requirement("Retry get_company_details at most once.")
    assert a.rule["id"] == b.rule["id"] and a.rule["id"].startswith("max_retries:")


@pytest.mark.parametrize("bad", [
    {"type": "forbidden_tool_call", "tool": "send_email"},
    {"type": "forbidden_tool_call", "tool": "save_assessment", "when_context": {"weekday": True}},
    {"type": "max_retries", "tool": "*", "max_retries": -1},
    {"type": "required_tool_order", "sequence": ["search_company"]},
    {"type": "required_tool_order", "sequence": ["search_company", "search_company"]},
    {"type": "requires_prior_event", "trigger_tool": "save_assessment", "operation": "save_assessment",
     "scope": "write", "entity_key": "company_id"},
    {"type": "max_retries", "tool": "*", "max_retries": 1, "code": "print(1)"},
    {"type": "python", "src": "print(1)"},
])
def test_registry_rejects_invalid_rules(bad):
    with pytest.raises(ValidationError):
        parse_rule(bad)
