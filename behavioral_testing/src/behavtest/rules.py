"""Rule schema and registry. Rules are data only; nothing generated is ever executed."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Union

from agenteval.tools import TOOL_SCHEMAS
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

TOOLS: tuple[str, ...] = tuple(t["name"] for t in TOOL_SCHEMAS)
WRITE_TOOLS = frozenset({"save_assessment"})

# How to read the business entity a call acts on, and the key approvals use for it.
ENTITY_PATHS = {
    "search_company": ("company_name", "name"),
    "get_company_details": ("company_id", "company_id"),
    "save_assessment": ("company_id", "result.company_id"),
}
CONTEXT_KEYS = frozenset({"authorized"})


def _check_tool(name: str, allow_any: bool = False) -> str:
    if allow_any and name == "*":
        return name
    if name not in TOOLS:
        raise ValueError(f"unknown tool {name!r}; registry has {', '.join(TOOLS)}")
    return name


class _Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = ""
    source: str = ""

    def semantic(self) -> dict:
        return self.model_dump(exclude={"id", "source"})

    def with_identity(self, source: str) -> "_Rule":
        digest = hashlib.sha1(json.dumps(self.semantic(), sort_keys=True).encode()).hexdigest()[:8]
        return self.model_copy(update={"id": f"{self.semantic()['type']}:{digest}", "source": source})


class RequiresPriorEvent(_Rule):
    """Every `trigger_tool` call needs an earlier granted approval for the same operation, entity and scope."""

    type: Literal["requires_prior_event"] = "requires_prior_event"
    trigger_tool: str
    event_kind: Literal["approval"] = "approval"
    operation: str
    scope: Literal["read", "write"]
    entity_key: str | None = None
    trigger_entity_path: str | None = None

    _v_tool = field_validator("trigger_tool", "operation")(lambda cls, v: _check_tool(v))

    @model_validator(mode="after")
    def _entity_pair(self):
        if (self.entity_key is None) != (self.trigger_entity_path is None):
            raise ValueError("entity_key and trigger_entity_path must be set together")
        return self


class ForbiddenToolCall(_Rule):
    """`tool` must never be called while every `when_context` condition holds (always, if empty)."""

    type: Literal["forbidden_tool_call"] = "forbidden_tool_call"
    tool: str
    when_context: dict[str, bool] = Field(default_factory=dict)

    _v_tool = field_validator("tool")(lambda cls, v: _check_tool(v))

    @field_validator("when_context")
    @classmethod
    def _known_context(cls, v):
        unknown = set(v) - CONTEXT_KEYS
        if unknown:
            raise ValueError(f"unsupported context keys {sorted(unknown)}")
        return v


class MaxRetries(_Rule):
    """No call of `tool` ("*" = any tool) may be retried more than `max_retries` times."""

    type: Literal["max_retries"] = "max_retries"
    tool: str
    max_retries: int = Field(ge=0, le=20)

    _v_tool = field_validator("tool")(lambda cls, v: _check_tool(v, allow_any=True))


class RequiredToolOrder(_Rule):
    """Precedence: whenever sequence[i+1] is called, sequence[i] must have been called earlier."""

    type: Literal["required_tool_order"] = "required_tool_order"
    sequence: list[str] = Field(min_length=2)

    @field_validator("sequence")
    @classmethod
    def _tools(cls, v):
        for name in v:
            _check_tool(name)
        if len(set(v)) != len(v):
            raise ValueError("sequence must not repeat tools")
        return v


Rule = Annotated[Union[RequiresPriorEvent, ForbiddenToolCall, MaxRetries, RequiredToolOrder],
                 Field(discriminator="type")]
RULE_TYPES = {m.model_fields["type"].default: m
              for m in (RequiresPriorEvent, ForbiddenToolCall, MaxRetries, RequiredToolOrder)}
_ADAPTER = TypeAdapter(Rule)


def parse_rule(data: dict) -> _Rule:
    """Validate untrusted rule JSON (from a file or a model). Raises pydantic.ValidationError."""
    return _ADAPTER.validate_python(data)


def rule_schema() -> dict:
    return _ADAPTER.json_schema()
