"""Demo: a LangGraph lead-research agent, fully offline (scripted model, simulated CRM API).

Ported from AgentEvalLab's sample agent (same synthetic companies and fault scenarios).

`baseline()` is the current production version. `candidate()` is a proposed change that:
  1. reviews every search hit (fixes the conflicting-records case),
  2. always saves assessments, even without write permission (a regression),
  3. adds funding enrichment through a custom `enrich` node that calls the funding API directly
     instead of through a LangChain tool, so those calls are invisible to tracing, slower, and
     rate-limited for large companies (an intermittent failure).
"""

from __future__ import annotations

import json
import operator
import re
import threading
import time
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import InjectedState, ToolNode
from langgraph.types import RetryPolicy

COMPANIES = {
    "ACME-001": {"company_id": "ACME-001", "name": "Acme Robotics", "domain": "acme-robotics.example",
                 "industry": "robotics", "employees": 420, "region": "NA"},
    "GLBX-001": {"company_id": "GLBX-001", "name": "Globex Analytics", "domain": "globex-analytics.example",
                 "industry": "analytics", "employees": 35, "region": "EU"},
    "INIT-001": {"company_id": "INIT-001", "name": "Initech Systems", "domain": "initech.example",
                 "industry": "software", "employees": 1200, "region": "NA"},
    "INIT-002": {"company_id": "INIT-002", "name": "Initech Systems", "domain": "initech-systems.example",
                 "industry": "consulting", "employees": 18, "region": "APAC"},
    "UMBR-001": {"company_id": "UMBR-001", "name": "Umbrella Labs", "domain": "umbrella-labs.example",
                 "industry": "biotech", "employees": 900, "region": "EU"},
    "HOOL-001": {"company_id": "HOOL-001", "name": "Hooli Holdings", "domain": "hooli-holdings.example",
                 "industry": "fintech", "employees": 3200, "region": "NA"},
}
FUNDING = {"ACME-001": "Series B", "GLBX-001": "Seed", "UMBR-001": "Series C", "HOOL-001": "Public",
           "INIT-001": "Bootstrapped", "INIT-002": "Seed"}
# Faults the simulated CRM injects on successive attempts, per company and tool.
FAULTS = {
    "Globex Analytics": {"get_company_details": ["timeout", "503"]},
    "Hooli Holdings": {"search_company": ["503"], "get_company_details": ["timeout", "503"],
                       "save_assessment": ["503", "429"]},
}
CONFLICT_FIELDS = ("domain", "industry", "employees", "region")


class UpstreamError(Exception):
    """Transient CRM failure (503 / 429 / timeout); the tools node retries these."""


class PermissionDenied(Exception):
    """403 from the CRM: the user has no write permission."""


class RateLimited(Exception):
    """429 from the funding API."""


_fault_lock = threading.Lock()
_fault_state: dict[tuple, int] = {}


def _maybe_fail(company: str, tool_name: str, config: RunnableConfig | None) -> None:
    thread = ((config or {}).get("configurable") or {}).get("thread_id", "default")
    plan = FAULTS.get(company, {}).get(tool_name, [])
    with _fault_lock:
        n = _fault_state.get((thread, company, tool_name), 0)
        _fault_state[(thread, company, tool_name)] = n + 1
    time.sleep(0.004)
    if n < len(plan):
        kind = plan[n]
        if kind == "timeout":
            time.sleep(0.03)
            raise UpstreamError(f"{tool_name} timed out after 30ms")
        raise UpstreamError({"503": f"503 Service Unavailable from CRM ({tool_name})",
                             "429": f"429 Too Many Requests from CRM ({tool_name})"}[kind])


@tool
def search_company(name: str, config: RunnableConfig) -> str:
    """Search the CRM for companies by name."""
    _maybe_fail(name, "search_company", config)
    hits = [{"company_id": c["company_id"], "name": c["name"]} for c in COMPANIES.values() if c["name"] == name]
    return json.dumps({"results": hits})


@tool
def get_company_details(company_id: str, config: RunnableConfig) -> str:
    """Get firmographics for one company id."""
    rec = COMPANIES.get(company_id)
    if rec is None:
        return json.dumps({"error": f"unknown company_id {company_id}"})
    _maybe_fail(rec["name"], "get_company_details", config)
    return json.dumps(rec)


@tool
def save_assessment(result: dict, state: Annotated[dict, InjectedState], config: RunnableConfig) -> str:
    """Save a lead assessment to the CRM. Requires write permission."""
    if not state.get("authorized"):
        raise PermissionDenied("403 Forbidden: write permission denied for save_assessment")
    _maybe_fail(result.get("company", ""), "save_assessment", config)
    return json.dumps({"saved": True, "company_id": result.get("company_id")})


class ScriptedLeadModel(BaseChatModel):
    """Deterministic stand-in for an LLM; its decisions follow `policy`."""

    policy: dict = {}
    model_name: str = "scripted-lead-v1"

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        time.sleep(0.006)
        msg = self._decide(messages)
        prompt_chars = sum(len(str(m.content)) for m in messages)
        out_chars = len(str(msg.content)) + len(json.dumps(msg.tool_calls))
        msg.usage_metadata = {"input_tokens": prompt_chars // 4 + 1, "output_tokens": out_chars // 4 + 1,
                              "total_tokens": prompt_chars // 4 + out_chars // 4 + 2}
        return ChatResult(generations=[ChatGeneration(message=msg)], llm_output={"model_name": self.model_name})

    def _decide(self, messages) -> AIMessage:
        task = next(str(m.content) for m in messages if isinstance(m, HumanMessage))
        company = re.search(r'company "([^"]+)"', task).group(1)
        authorized = bool(re.search(r"Write permission for save_assessment: granted", task))
        results: dict[str, list] = {}
        for m in messages:
            if isinstance(m, ToolMessage):
                try:
                    content = json.loads(m.content)
                except (TypeError, ValueError):
                    content = {"error": str(m.content)}
                if getattr(m, "status", "success") == "error":
                    content = {"error": str(m.content)}
                results.setdefault(m.name, []).append(content)
        n = sum(isinstance(m, AIMessage) for m in messages)

        def call(*calls):
            return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{n}_{i}"}
                                                     for i, (name, args) in enumerate(calls)])

        def final(decision, company_id=None, conflict=False, saved=False, reason=""):
            return AIMessage(content=json.dumps({"company": company, "company_id": company_id, "decision": decision,
                                                 "conflict": conflict, "saved": saved, "reason": reason}))

        if "search_company" not in results:
            return call(("search_company", {"name": company}))
        hits = results["search_company"][-1].get("results", [])
        if not hits:
            return final("not_found", reason="no CRM record")
        if "get_company_details" not in results:
            targets = hits if self.policy.get("conflict_strategy") == "review_all" else hits[:1]
            return call(*[("get_company_details", {"company_id": h["company_id"]}) for h in targets])
        details = results["get_company_details"]
        conflict = len(details) > 1 and any(len({json.dumps(d.get(f)) for d in details}) > 1 for f in CONFLICT_FIELDS)
        if conflict:
            assessment = {"decision": "needs_review", "company_id": None, "conflict": True,
                          "reason": "records disagree on firmographics"}
        else:
            rec = details[0]
            ok = rec["region"] in ("NA", "EU") and 50 <= rec["employees"] <= 5000
            assessment = {"decision": "qualified" if ok else "not_qualified", "company_id": rec["company_id"],
                          "conflict": False, "reason": f"{rec['employees']} employees, region {rec['region']}"}
            if self.policy.get("enrich_funding") and "get_funding" not in results:
                return call(("get_funding", {"company_id": rec["company_id"]}))
            if "get_funding" in results:
                assessment["reason"] += f", funding {results['get_funding'][-1].get('stage', 'unknown')}"
        if (authorized or self.policy.get("always_save")) and "save_assessment" not in results:
            payload = {"company": company, **{k: assessment[k] for k in ("company_id", "decision", "reason")}}
            return call(("save_assessment", {"result": payload}))
        saved = bool(results.get("save_assessment", [{}])[-1].get("saved"))
        return final(saved=saved, **assessment)


class LeadState(TypedDict, total=False):
    company: str
    authorized: bool
    messages: Annotated[list, operator.add]
    result: dict


SYSTEM = "You are a B2B lead-research agent. Use the tools, then reply with a JSON assessment."


def _build(policy: dict, with_enrichment: bool):
    model = ScriptedLeadModel(policy=policy).bind_tools([search_company, get_company_details, save_assessment])
    funding_calls: dict[str, int] = {}

    def plan(state: LeadState) -> dict:
        task = (f'Research the company "{state["company"]}" and assess it as a sales lead. '
                f'Write permission for save_assessment: {"granted" if state.get("authorized") else "denied"}.')
        history = [SystemMessage(SYSTEM), HumanMessage(task)] + list(state.get("messages") or [])
        return {"messages": [model.invoke(history)]}

    def enrich(state: LeadState) -> dict:
        # Calls the funding API directly (no LangChain tool), so tracing only sees the node.
        out = []
        for tc in state["messages"][-1].tool_calls:
            cid = tc["args"]["company_id"]
            time.sleep(0.12)
            funding_calls[cid] = funding_calls.get(cid, 0) + 1
            if COMPANIES[cid]["employees"] > 1000 and funding_calls[cid] % 3 == 0:
                raise RateLimited(f"429 Too Many Requests from funding API for {cid}")
            out.append(ToolMessage(json.dumps({"stage": FUNDING[cid]}), name="get_funding", tool_call_id=tc["id"]))
        return {"messages": out}

    def finalize(state: LeadState) -> dict:
        return {"result": json.loads(state["messages"][-1].content)}

    def route(state: LeadState) -> str:
        calls = state["messages"][-1].tool_calls
        if not calls:
            return "finalize"
        if with_enrichment and any(c["name"] == "get_funding" for c in calls):
            return "enrich"
        return "tools"

    g = StateGraph(LeadState)
    g.add_node("plan", plan)
    g.add_node("tools", ToolNode([search_company, get_company_details, save_assessment],
                                 handle_tool_errors=(PermissionDenied,)),
               retry_policy=RetryPolicy(max_attempts=3, initial_interval=0.005, jitter=False, retry_on=UpstreamError))
    g.add_node("finalize", finalize)
    g.add_edge(START, "plan")
    targets = ["tools", "finalize"]
    if with_enrichment:
        g.add_node("enrich", enrich)
        g.add_edge("enrich", "plan")
        targets.append("enrich")
    g.add_conditional_edges("plan", route, targets)
    g.add_edge("tools", "plan")
    g.add_edge("finalize", END)
    return g.compile()


def baseline():
    return _build({"conflict_strategy": "first_result", "always_save": False}, with_enrichment=False)


def candidate():
    return _build({"conflict_strategy": "review_all", "always_save": True, "enrich_funding": True},
                  with_enrichment=True)
