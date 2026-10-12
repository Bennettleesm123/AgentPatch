import pytest

from agenteval.agent import Limits, run_case
from agenteval.configs import BASELINE, CANDIDATE
from agenteval.data import CASES_BY_ID
from agenteval.models import ScriptedModel


def run(case_id, config=BASELINE, out_dir=None, **limits):
    return run_case(CASES_BY_ID[case_id], config, ScriptedModel(config.policy), "offline",
                    Limits(**limits), out_dir=out_dir)


def test_normal_success(out_dir):
    r = run("normal", out_dir=out_dir)
    assert r["status"] == "completed"
    assert r["final"]["decision"] == "qualified"
    assert r["final"]["saved"] is True
    assert [c["name"] for c in r["tool_calls"]] == ["search_company", "get_company_details", "save_assessment"]
    assert r["llm_turns"] == 4 and r["writes"]


def test_transient_failure_recovers_with_bounded_retries(out_dir):
    r = run("transient_failure", out_dir=out_dir)
    details = r["tool_calls"][1]
    assert details["name"] == "get_company_details" and details["attempts"] == 3 and details["status"] == "ok"
    assert r["final"]["decision"] == "not_qualified"


def test_transient_failure_gives_up_when_retries_exhausted(out_dir):
    r = run("transient_failure", out_dir=out_dir, max_retries=1)
    assert r["tool_calls"][1]["status"] == "error"
    assert r["final"]["decision"] == "error"


@pytest.mark.parametrize("config,decision,n_details", [(BASELINE, "qualified", 1), (CANDIDATE, "needs_review", 2)])
def test_conflict_policy(out_dir, config, decision, n_details):
    r = run("conflict", config, out_dir=out_dir)
    assert r["final"]["decision"] == decision
    assert sum(c["name"] == "get_company_details" for c in r["tool_calls"]) == n_details


def test_unauthorized_baseline_does_not_write(out_dir):
    r = run("unauthorized", out_dir=out_dir)
    assert not any(c["name"] == "save_assessment" for c in r["tool_calls"])
    assert r["writes"] == [] and r["denied_writes"] == []


def test_unauthorized_candidate_attempts_write_and_is_denied(out_dir):
    r = run("unauthorized", CANDIDATE, out_dir=out_dir)
    save = [c for c in r["tool_calls"] if c["name"] == "save_assessment"][0]
    assert save["status"] == "error" and "403" in save["error"] and save["attempts"] == 1
    assert r["writes"] == [] and len(r["denied_writes"]) == 1


def test_llm_turn_limit(out_dir):
    r = run("normal", out_dir=out_dir, max_llm_turns=2)
    assert r["status"] == "limit_exceeded" and r["limit_hit"] == "max_llm_turns" and r["llm_turns"] == 2


def test_tool_call_limit(out_dir):
    r = run("transient_failure", out_dir=out_dir, max_tool_calls=3)
    assert r["status"] == "limit_exceeded" and r["tool_attempts"] == 3


def test_offline_is_deterministic(out_dir):
    a, b = run("conflict", CANDIDATE, out_dir=out_dir), run("conflict", CANDIDATE, out_dir=out_dir)
    strip = lambda r: {k: v for k, v in r.items() if k not in ("run_id", "latency_ms")}
    assert strip(a) == strip(b)
