"""Evidence-grounded diagnosis: observed facts, *possible* causes and next actions, each citing events.

Causes come from a fixed per-rule-type catalogue and may only cite events that are already evidence
for the verdict (plus the root and the LLM turn that chose the call). Nothing is inferred from
free text inside the trace, so noise or misleading text cannot become an explanation.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .evaluator import EvalResult, Finding
from .events import EventLog, TraceEvent, _error_text, get_path
from .rules import ENTITY_PATHS


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    cites: list[str]


class Diagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: str
    verdict: str
    first_violation: Finding | None = None
    facts: list[Claim] = []
    possible_causes: list[Claim] = []
    next_actions: list[Claim] = []
    allowed_citations: list[str] = []

    def claims(self) -> list[Claim]:
        return self.facts + self.possible_causes + self.next_actions


def describe(e: TraceEvent) -> str:
    """Short factual description of an event built only from structured fields."""
    if e.kind == "tool_call":
        key, path = ENTITY_PATHS.get(e.tool, (None, None))
        entity = get_path(e.input, path) if path else None
        s = f"{e.id} called {e.tool}" + (f" with {key}={entity!r}" if entity is not None else "")
    elif e.kind == "tool_attempt":
        s = f"{e.id} was an attempt of {e.tool}"
    elif e.kind == "approval":
        a = e.attributes
        s = (f"{e.id} recorded a {a.get('approval.decision')!r} {a.get('approval.scope')!r} approval for "
             f"{a.get('approval.operation')!r} on {a.get('approval.entity')!r}")
    elif e.kind == "agent":
        ctx = e.input if isinstance(e.input, dict) else {}
        s = f"{e.id} is the run root (authorized={ctx.get('authorized')!r})"
    else:
        s = f"{e.id} is a {e.kind} event named {e.name!r}"
    s += f"; status {e.status}"
    if e.error:
        s += f" ({_error_text(e.error)})"
    return s


def _catalogue(result: EvalResult, log: EventLog, first: Finding) -> list[Claim]:
    ids = first.event_ids
    events = [log.by_id[i] for i in ids if i in log.by_id]
    calls = [e for e in events if e.kind == "tool_call"]
    call = calls[0] if calls else None
    t = result.rule_type
    if t == "requires_prior_event" and call:
        approvals = [e.id for e in events if e.kind == "approval"]
        if approvals:
            return [Claim(text=f"An approval was recorded but did not match {call.id} (wrong operation, entity, scope, "
                               "timing or decision); the approval step may be requested for the wrong action.",
                          cites=approvals + [call.id])]
        return [Claim(text=f"The agent may proceed to {call.tool} without any approval step in its instructions "
                           "or workflow.", cites=[call.id])]
    if t == "forbidden_tool_call" and call:
        return [Claim(text=f"The agent may not check the run's authorization context before choosing {call.tool}.",
                      cites=[i for i in ids if i in log.by_id])]
    if t == "max_retries" and call:
        errs = [e for e in events if e.kind == "tool_attempt" and e.status == "error"]
        out = [Claim(text=f"The agent's retry limit for {call.tool} may be configured higher than this requirement "
                          "allows.", cites=[call.id])]
        if errs:
            kinds = sorted({_error_text(e.error) for e in errs})
            out.append(Claim(text=f"{call.tool} failed repeatedly ({'; '.join(kinds)}), which is what triggered the "
                                  "retries.", cites=[e.id for e in errs]))
        return out
    if t == "required_tool_order" and call:
        return [Claim(text=f"The agent may skip or reorder earlier steps before {call.tool} (for example when it "
                           "believes it already has their output).", cites=[call.id])]
    return []


def diagnose(result: EvalResult, log: EventLog) -> Diagnosis:
    first = next((f for f in result.findings if f.kind == ("violation" if result.verdict == "fail" else "unknown")),
                 None)
    anchor_ids = (first.event_ids if first else result.evidence)
    anchor = next((log.by_id[i] for i in anchor_ids if i in log.by_id and log.by_id[i].kind == "tool_call"), None)
    turn = log.preceding(anchor, "llm") if anchor else None
    allowed = set(result.evidence) | ({log.root.id} if log.root else set()) | ({turn.id} if turn else set())
    d = Diagnosis(rule_id=result.rule_id, verdict=result.verdict, first_violation=first if result.verdict == "fail"
                  else None, allowed_citations=log.sort_ids(allowed))
    if result.verdict == "pass":
        d.facts = [Claim(text=result.reason, cites=result.evidence)] if result.evidence else \
            [Claim(text=result.reason, cites=[log.root.id] if log.root else [])]
        return d
    focus = first.event_ids if first else result.evidence
    d.facts = [Claim(text=result.reason, cites=focus or ([log.root.id] if log.root else []))]
    d.facts += [Claim(text=describe(log.by_id[i]), cites=[i]) for i in focus if i in log.by_id
                and log.by_id[i].kind != "tool_attempt"]
    if result.verdict == "fail":
        d.possible_causes = _catalogue(result, log, first) if first else []
        if turn and anchor:
            d.next_actions.append(Claim(text=f"Inspect {turn.id}, the model turn that decided to call {anchor.tool}, "
                                             "and the instructions it was given.", cites=[turn.id, anchor.id]))
        if anchor:
            d.next_actions.append(Claim(text=f"Reproduce the run and confirm the fix removes the violation at "
                                             f"{anchor.id}.", cites=[anchor.id]))
    else:
        cites = focus or ([log.root.id] if log.root else [])
        if log.integrity_issues:
            d.facts.append(Claim(text="Trace integrity problems: " + "; ".join(log.integrity_issues),
                                 cites=cites))
            d.next_actions.append(Claim(text="Re-export the complete trace (all spans, no dropped parents) and "
                                             "re-run the check; the verdict cannot be decided from this trace.",
                                        cites=cites))
        else:
            d.next_actions.append(Claim(text="Record the missing field on the cited events (for approvals: "
                                             "operation, scope, decision and the entity id) and re-run the check.",
                                        cites=cites))
    return d


def unsupported_claims(d: Diagnosis) -> list[Claim]:
    allowed = set(d.allowed_citations)
    return [c for c in d.claims() if not c.cites or not set(c.cites) <= allowed]
