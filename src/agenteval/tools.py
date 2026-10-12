"""Mocked lead-research tools backed by synthetic data, with fault injection."""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field

from .data import COMPANIES


class ToolError(Exception):
    retryable = False


class TransientToolError(ToolError):
    retryable = True


class ToolTimeoutError(TransientToolError):
    pass


class UnauthorizedError(ToolError):
    pass


TOOL_SCHEMAS: list[dict] = [
    {
        "name": "search_company",
        "description": "Search the CRM for companies by exact name. Returns candidate records (id, name, domain).",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Company name"}},
            "required": ["name"],
        },
    },
    {
        "name": "get_company_details",
        "description": "Fetch the firmographic record (industry, employees, region, domain) for a company id.",
        "parameters": {
            "type": "object",
            "properties": {"company_id": {"type": "string"}},
            "required": ["company_id"],
        },
    },
    {
        "name": "save_assessment",
        "description": "Persist a lead assessment to the CRM. Requires write permission.",
        "parameters": {
            "type": "object",
            "properties": {
                "result": {
                    "type": "object",
                    "description": "Assessment with keys company, company_id, decision, reason.",
                    "properties": {
                        "company": {"type": "string"},
                        "company_id": {"type": "string"},
                        "decision": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["company", "decision"],
                }
            },
            "required": ["result"],
        },
    },
]


@dataclass
class ToolContext:
    authorized: bool
    faults: dict[str, list[str]] = field(default_factory=dict)
    fault_delay_s: float = 0.3
    writes: list[dict] = field(default_factory=list)
    denied_writes: list[dict] = field(default_factory=list)


def search_company(ctx: ToolContext, name: str) -> dict:
    hits = [
        {"company_id": c["company_id"], "name": c["name"], "domain": c["domain"]}
        for c in COMPANIES.values()
        if c["name"].lower() == str(name).strip().lower()
    ]
    return {"results": hits}


def get_company_details(ctx: ToolContext, company_id: str) -> dict:
    if company_id not in COMPANIES:
        raise ToolError(f"404 company_id {company_id!r} not found")
    return copy.deepcopy(COMPANIES[company_id])


def save_assessment(ctx: ToolContext, result: dict) -> dict:
    if not ctx.authorized:
        ctx.denied_writes.append(copy.deepcopy(result))
        raise UnauthorizedError("403 write permission denied for save_assessment")
    if not isinstance(result, dict) or "decision" not in result:
        raise ToolError("400 result must be an object containing 'decision'")
    ctx.writes.append(copy.deepcopy(result))
    return {"saved": True, "assessment_id": f"ASM-{len(ctx.writes):03d}"}


TOOLS = {
    "search_company": search_company,
    "get_company_details": get_company_details,
    "save_assessment": save_assessment,
}


def call_tool(ctx: ToolContext, name: str, args: dict) -> dict:
    """Execute a single attempt of a tool, applying any injected fault first."""
    fn = TOOLS.get(name)
    if fn is None:
        raise ToolError(f"unknown tool {name!r}")
    queue = ctx.faults.get(name)
    fault = queue.pop(0) if queue else None
    if fault == "timeout":
        time.sleep(ctx.fault_delay_s)
    elif fault == "error":
        raise TransientToolError("503 Service Unavailable (synthetic upstream outage)")
    elif fault == "ratelimit":
        raise TransientToolError("429 Too Many Requests (synthetic CRM write quota)")
    try:
        return fn(ctx, **(args or {}))
    except TypeError as exc:
        raise ToolError(f"invalid arguments for {name}: {exc}") from exc
