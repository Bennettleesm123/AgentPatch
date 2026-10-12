"""Natural-language requirement -> validated rule, using deterministic templates.

The compiler abstains instead of guessing: hedged wording is "ambiguous", anything outside the
four registered rule types (or naming tools/conditions the registry doesn't know) is "unsupported".
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from .rules import (ENTITY_PATHS, TOOLS, WRITE_TOOLS, ForbiddenToolCall, MaxRetries, RequiredToolOrder,
                    RequiresPriorEvent, parse_rule)

Status = Literal["compiled", "ambiguous", "unsupported"]


class CompileResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirement: str
    status: Status
    rule: dict | None = None
    issues: list[str] = []
    method: str = "template"


ALIASES = {
    "search_company": ["search_company", "search for the company", "search the company", "company search",
                       "searching for the company", "look up the company", "search"],
    "get_company_details": ["get_company_details", "fetch the company details", "fetch company details",
                            "fetch details", "get the company details", "get company details", "company details",
                            "details"],
    "save_assessment": ["save_assessment", "save the assessment", "saving the assessment", "save an assessment",
                        "saves the assessment", "saves an assessment", "write the assessment", "store the assessment",
                        "save", "saves", "saving", "saved"],
}
_ALIAS_RE = sorted(((a, t) for t, names in ALIASES.items() for a in names), key=lambda p: -len(p[0]))
VAGUE_TOOL = re.compile(r"\b(?:the|that|this|a|company|lookup|some|other)\s+tools?\b")
UNIVERSAL_TOOL = re.compile(r"\b(?:any|each|every|all|per)\s+(?:tool|tools|tool call|tool calls)\b")
HEDGES = re.compile(r"\b(?:usually|normally|generally|typically|ideally|mostly|often|sometimes|maybe|"
                    r"probably|a few|several|some times|reasonable|appropriate|be careful|try to|"
                    r"if possible|where possible|when possible|as needed|too many|too often|should probably)\b")
APPROVAL = re.compile(r"\b(?:approval|approve[ds]?|consent|sign[- ]off|confirmation|confirm)\b")
RETRY = re.compile(r"\b(?:retr(?:y|ies|ied|ying)|attempts?|tries)\b")
ORDER = re.compile(r"\b(?:before|then|after|first|followed by|prior to)\b")
NEGATION = re.compile(r"\b(?:never|not|don't|dont|must not|mustn't|cannot|can't|no|forbidden|prohibited)\b")
DENIED_CONTEXT = re.compile(
    r"\b(?:write permission is denied|permission is denied|permission denied|not authori[sz]ed|unauthori[sz]ed|"
    r"without (?:write )?permission|lacks? (?:write )?permission|(?:write )?permission (?:is )?not granted|"
    r"no (?:write )?permission)\b")
CONDITION = re.compile(r"\b(?:when|if|unless|while|whenever|without|in case)\b")
SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
NUMBER_WORDS = {"zero": 0, "one": 1, "once": 1, "two": 2, "twice": 2, "three": 3, "thrice": 3, "four": 4,
                "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_NUM = r"(\d+|" + "|".join(NUMBER_WORDS) + r")"


def _num(token: str) -> int:
    return int(token) if token.isdigit() else NUMBER_WORDS[token]


def _tool_hits(text: str) -> list[tuple[int, int, str]]:
    taken = [False] * len(text)
    hits = []
    for alias, tool in _ALIAS_RE:
        for m in re.finditer(rf"\b{re.escape(alias)}\b", text):
            if any(taken[m.start():m.end()]):
                continue
            taken[m.start():m.end()] = [True] * (m.end() - m.start())
            hits.append((m.start(), m.end(), tool))
    return sorted(hits)


def find_tools(text: str) -> list[str]:
    """Registered tools mentioned in text, in order of first mention (longest alias wins)."""
    seen, out = set(), []
    for _, _, tool in _tool_hits(text):
        if tool not in seen:
            seen.add(tool)
            out.append(tool)
    return out


# Words a compiled requirement may contain besides tool names. Anything else (e.g. "on weekends",
# "a different company") is a qualifier no rule type can represent, so the compiler abstains rather
# than silently dropping it.
KNOWN_WORDS = frozenset("""
a an the any each every all per make makes made tool tools call calls calling called use uses using used invoke run runs running
agent assistant it its you your we they must should shall always never not do does don't dont no only may can
cannot can't mustn't is are be been being has have had needs need require requires required requiring
before after then first followed by prior to of
retry retries retried retrying attempt attempts tries try get gets obtain obtains ask asks request requests
approval approvals approve approves approved consent sign off sign-off confirmation confirm confirms explicit
explicitly user user's users human write read scope same company entity when if while whenever without unless
in case permission permissions denied is not authorized authorised unauthorized unauthorised lacks lack granted
and for from on this that which with again allow allows allowed permit permits permitted
""".split())
COUNT_WORDS = frozenset("at most more than over up maximum max times time once twice thrice zero one two three four "
                        "five six seven eight nine ten".split())


def _unexplained_words(text: str, rule_type: str) -> list[str]:
    for start, end, _ in reversed(_tool_hits(text)):
        text = text[:start] + " " + text[end:]
    known = KNOWN_WORDS | COUNT_WORDS if rule_type == "max_retries" else KNOWN_WORDS
    return [w for w in re.findall(r"[a-z][a-z'-]*|\d+", text)
            if w not in known and not (w.isdigit() and rule_type == "max_retries")]


def _abstain(req: str, status: Status, *issues: str) -> CompileResult:
    return CompileResult(requirement=req, status=status, issues=list(issues))


def _single_tool(req, tools) -> str | CompileResult:
    if len(tools) == 1:
        return tools[0]
    if not tools:
        return _abstain(req, "ambiguous", "no registered tool is named; registry has " + ", ".join(TOOLS))
    return _abstain(req, "ambiguous", f"several tools named ({', '.join(tools)}); state one requirement per tool")


def _approval(req, text, tools):
    tool = _single_tool(req, tools)
    if isinstance(tool, CompileResult):
        return tool
    if re.search(r"\b(?:read|view) (?:only )?approval\b", text):
        scope = "read"
    elif re.search(r"\bwrite approval\b", text) or tool in WRITE_TOOLS:
        scope = "write"
    else:
        scope = "read"
    key, path = ENTITY_PATHS[tool]
    return RequiresPriorEvent(trigger_tool=tool, operation=tool, scope=scope, entity_key=key,
                              trigger_entity_path=path)


def _retries(req, text, tools):
    m_attempts = re.search(rf"\b(?:at most|no more than|up to|maximum of|max(?:imum)?)\s+{_NUM}\s+(?:attempts?|tries)\b",
                           text) or re.search(rf"\b{_NUM}\s+(?:attempts?|tries)\s+(?:at most|max(?:imum)?)\b", text)
    m_retry = (re.search(rf"\bretr\w*\b.*?\b(?:more than|over)\s+{_NUM}(?:\s+times?)?\b", text)
               or re.search(rf"\bretr\w*\b.*?\b(?:at most|no more than|up to|maximum of|max)\s+{_NUM}(?:\s+times?)?\b", text)
               or re.search(rf"\b(?:at most|no more than|up to|maximum of|max)\s+{_NUM}\s+retr\w*\b", text)
               or re.search(rf"\bretr\w*\b.*?\b(once|twice|thrice)\b", text))
    if m_attempts:
        n = _num(m_attempts.group(1)) - 1
        if n < 0:
            return _abstain(req, "ambiguous", "zero attempts would forbid calling the tool at all")
    elif m_retry:
        n = _num(m_retry.group(1))
    elif re.search(r"\b(?:never|do not|don't|must not|no)\s+retr\w*\b|\bwithout retrying\b", text):
        n = 0
    else:
        return _abstain(req, "ambiguous", "no explicit retry limit (give a number, e.g. 'at most 2 retries')")
    if len(tools) > 1:
        return _abstain(req, "ambiguous", f"several tools named ({', '.join(tools)}); state one limit per tool")
    if tools:
        tool = tools[0]
    elif UNIVERSAL_TOOL.search(text) or not VAGUE_TOOL.search(text):
        tool = "*"
    else:
        return _abstain(req, "ambiguous", "unclear which tool the retry limit applies to")
    return MaxRetries(tool=tool, max_retries=n)


def _order(req, text, tools):
    if len(tools) < 2:
        return _abstain(req, "ambiguous", "an ordering requirement needs at least two registered tools")
    if NEGATION.search(text):
        # "Never call B before A" means A must come first; other negated orderings don't map to precedence.
        if len(tools) == 2 and re.search(r"\bbefore\b", text) and not re.search(r"\b(?:after|then)\b", text):
            return RequiredToolOrder(sequence=tools[::-1])
        return _abstain(req, "ambiguous", "negated ordering; state the required order positively with 'then'")
    if re.search(r"\b(?:after|prior to)\b", text):
        if len(tools) != 2:
            return _abstain(req, "ambiguous", "'after' with more than two tools; list the steps in order with 'then'")
        if re.search(r"\bafter\b", text):
            tools = tools[::-1]
    return RequiredToolOrder(sequence=tools)


def _forbidden(req, text, tools):
    tool = _single_tool(req, tools)
    if isinstance(tool, CompileResult):
        return tool
    if DENIED_CONTEXT.search(text):
        return ForbiddenToolCall(tool=tool, when_context={"authorized": False})
    if CONDITION.search(text):
        return _abstain(req, "unsupported", "condition is not in the context registry (supported: write permission "
                        "denied / not authorized)")
    return ForbiddenToolCall(tool=tool)


def compile_requirement(requirement: str) -> CompileResult:
    req = requirement.strip()
    text = re.sub(r"\s+", " ", req.lower()).strip(" .!")
    if not text:
        return _abstain(req, "unsupported", "empty requirement")
    unknown = sorted({w for w in SNAKE.findall(text) if w not in TOOLS})
    if unknown:
        return _abstain(req, "unsupported", f"unknown tool(s) {', '.join(unknown)}; registry has {', '.join(TOOLS)}")
    hedge = HEDGES.search(text)
    if hedge:
        return _abstain(req, "ambiguous", f"hedged wording {hedge.group(0)!r} cannot be checked strictly")
    tools = find_tools(text)
    if VAGUE_TOOL.search(text) and not tools:
        return _abstain(req, "ambiguous", "refers to a tool without naming it; registry has " + ", ".join(TOOLS))
    if APPROVAL.search(text):
        built = _approval(req, text, tools)
    elif RETRY.search(text):
        built = _retries(req, text, tools)
    elif ORDER.search(text):
        built = _order(req, text, tools)
    elif NEGATION.search(text) and tools:
        built = _forbidden(req, text, tools)
    else:
        return _abstain(req, "unsupported", "matches none of the supported rule types: requires_prior_event, "
                        "forbidden_tool_call, max_retries, required_tool_order")
    if isinstance(built, CompileResult):
        return built
    extra = _unexplained_words(text, built.type)
    if extra:
        return _abstain(req, "unsupported", f"words not representable by a {built.type} rule: {', '.join(extra)}; "
                        "the requirement may carry a condition the rule would silently drop")
    try:
        rule = parse_rule(built.model_dump()).with_identity(req)
    except ValidationError as exc:
        return _abstain(req, "unsupported", f"rule failed validation: {exc.errors()[0]['msg']}")
    return CompileResult(requirement=req, status="compiled", rule=rule.model_dump())
