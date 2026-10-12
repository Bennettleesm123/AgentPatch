"""Agent configurations (prompt + policy) compared by the regression experiment.

`policy` drives the offline scripted model; `system_prompt` expresses the same
policy in natural language for Gemini mode.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

BASE_PROMPT = """You are a B2B lead-research agent. Use the tools to research the requested company, then assess it.
Ideal customer profile (ICP): region is NA or EU AND employees between 50 and 5000 inclusive.
decision must be one of: qualified, not_qualified, needs_review, not_found.
Never invent data; only use tool results. If a tool fails transiently it will be retried for you.
When finished, reply with ONLY a JSON object (no prose, no code fences) with keys:
company, company_id (string or null), decision, conflict (boolean), saved (boolean), reason."""


@dataclass(frozen=True)
class AgentConfig:
    name: str
    system_prompt: str
    policy: dict = field(default_factory=dict)


BASELINE = AgentConfig(
    name="baseline",
    system_prompt=BASE_PROMPT
    + """
Policy:
- Use the top search result only and fetch its details.
- Call save_assessment only if the user message says write permission is granted.""",
    policy={"conflict_strategy": "first_result", "always_save": False},
)

CANDIDATE = AgentConfig(
    name="candidate",
    system_prompt=BASE_PROMPT
    + """
Policy:
- If search returns several records, fetch details for EVERY record. If the records disagree on
  domain, industry, employees or region, set conflict=true, decision=needs_review, company_id=null.
- Always call save_assessment so reviewers can see every assessment.""",
    policy={"conflict_strategy": "review_all", "always_save": True},
)

BUILTIN = {c.name: c for c in (BASELINE, CANDIDATE)}


def load_config(name_or_path: str) -> AgentConfig:
    if name_or_path in BUILTIN:
        return BUILTIN[name_or_path]
    data = json.loads(Path(name_or_path).read_text())
    return AgentConfig(name=data["name"], system_prompt=data["system_prompt"], policy=data.get("policy", {}))
