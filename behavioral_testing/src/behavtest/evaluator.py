"""Deterministic rule evaluation over trace events. Verdicts: pass / fail / unknown (never a pass).

A violation that depends on the *absence* of an event is only reported as `fail` when the trace is
complete; otherwise the missing event might simply not have been recorded, so the verdict is `unknown`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .events import EventLog, TraceEvent, get_path
from .rules import ForbiddenToolCall, MaxRetries, RequiredToolOrder, RequiresPriorEvent, _Rule

Verdict = Literal["pass", "fail", "unknown"]


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["violation", "unknown"]
    event_ids: list[str]
    message: str


class EvalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: str
    rule_type: str
    requirement: str
    verdict: Verdict
    evidence: list[str]
    reason: str
    findings: list[Finding] = []


def _result(rule: _Rule, log: EventLog, verdict: Verdict, evidence, reason: str, findings=()) -> EvalResult:
    return EvalResult(rule_id=rule.id, rule_type=rule.semantic()["type"], requirement=rule.source, verdict=verdict,
                      evidence=log.sort_ids(evidence), reason=reason, findings=list(findings))


def _incomplete(log: EventLog) -> str:
    return "trace is incomplete (" + "; ".join(log.integrity_issues) + ")"


def _approval_fields(e: TraceEvent) -> dict:
    a = e.attributes
    return {"operation": a.get("approval.operation"), "scope": a.get("approval.scope"),
            "decision": a.get("approval.decision"), "entity": a.get("approval.entity")}


def _requires_prior_event(rule: RequiresPriorEvent, log: EventLog) -> EvalResult:
    triggers = log.tool_calls(rule.trigger_tool)
    approvals = log.approvals()
    if not triggers:
        if not log.complete:
            return _result(rule, log, "unknown", [], f"no {rule.trigger_tool} call observed, but {_incomplete(log)}")
        return _result(rule, log, "pass", [], f"{rule.trigger_tool} was never called, so no approval was needed")
    findings, satisfied = [], []
    for trig in triggers:
        target = get_path(trig.input, rule.trigger_entity_path) if rule.trigger_entity_path else None
        reasons, uncomparable, match = [], [], None
        for ap in approvals:
            f = _approval_fields(ap)
            if ap.index > trig.index:
                reasons.append(f"{ap.id} was recorded after {trig.id}")
                continue
            if f["operation"] != rule.operation:
                reasons.append(f"{ap.id} approves operation {f['operation']!r}, not {rule.operation!r}")
                continue
            if f["decision"] != "granted":
                reasons.append(f"{ap.id} decision is {f['decision']!r}, not 'granted'")
                continue
            if f["scope"] != rule.scope:
                reasons.append(f"{ap.id} has scope {f['scope']!r}, but {rule.scope!r} is required")
                continue
            if rule.entity_key:
                got = (f["entity"] or {}).get(rule.entity_key) if isinstance(f["entity"], dict) else None
                if got is None or target is None:
                    uncomparable.append(ap)
                    continue
                if got != target:
                    reasons.append(f"{ap.id} is for {rule.entity_key} {got!r}, but {trig.id} acts on {target!r}")
                    continue
            match = ap
            break
        if match:
            satisfied += [match.id, trig.id]
        elif uncomparable:
            ids = [a.id for a in uncomparable] + [trig.id]
            findings.append(Finding(kind="unknown", event_ids=log.sort_ids(ids),
                                    message=f"cannot compare the approved {rule.entity_key} with {trig.id}'s "
                                            f"({target!r}): the value is missing on "
                                            + ", ".join(a.id for a in uncomparable)))
        else:
            detail = "; ".join(reasons) if reasons else "no approval event exists in the trace"
            findings.append(Finding(kind="violation", event_ids=log.sort_ids([trig.id] + [a.id for a in approvals]),
                                    message=f"{trig.id} has no earlier granted {rule.scope} approval for "
                                            f"{rule.operation} on {rule.entity_key or 'this call'} {target!r}: {detail}"))
    violations = [f for f in findings if f.kind == "violation"]
    if violations and log.complete:
        return _result(rule, log, "fail", [i for f in violations for i in f.event_ids], violations[0].message, findings)
    if findings or not log.complete:
        if not log.complete:
            findings = [f.model_copy(update={"kind": "unknown"}) for f in findings]
        reason = findings[0].message if findings else ""
        if not log.complete:
            reason = (reason + "; " if reason else "") + f"an approval may be missing because {_incomplete(log)}"
        return _result(rule, log, "unknown", [i for f in findings for i in f.event_ids] or [t.id for t in triggers],
                       reason, findings)
    return _result(rule, log, "pass", satisfied,
                   f"every {rule.trigger_tool} call has an earlier matching granted approval")


def _forbidden_tool_call(rule: ForbiddenToolCall, log: EventLog) -> EvalResult:
    root = log.root
    ctx_ids = [root.id] if (rule.when_context and root) else []
    if rule.when_context:
        ctx = root.input if root and isinstance(root.input, dict) else {}
        missing = [k for k in rule.when_context if k not in ctx]
        if missing:
            return _result(rule, log, "unknown", ctx_ids, f"context {', '.join(missing)} is not recorded on the root event",
                           [Finding(kind="unknown", event_ids=ctx_ids, message=f"missing context {missing}")])
        unmet = {k: ctx[k] for k, v in rule.when_context.items() if ctx[k] != v}
        if unmet:
            return _result(rule, log, "pass", ctx_ids, "rule not applicable: run has "
                           + ", ".join(f"{k}={v}" for k, v in unmet.items()) + " "
                           "(rule applies only when " + ", ".join(f"{k}={v}" for k, v in rule.when_context.items()) + ")")
    calls = log.tool_calls(rule.tool)
    cond = (" while " + ", ".join(f"{k}={v}" for k, v in rule.when_context.items())) if rule.when_context else ""
    if calls:
        findings = [Finding(kind="violation", event_ids=ctx_ids + [c.id],
                            message=f"{c.id} called {rule.tool}{cond} (call status: {c.status})") for c in calls]
        return _result(rule, log, "fail", ctx_ids + [c.id for c in calls], findings[0].message, findings)
    if not log.complete:
        return _result(rule, log, "unknown", ctx_ids, f"no {rule.tool} call observed, but {_incomplete(log)}")
    return _result(rule, log, "pass", ctx_ids, f"{rule.tool} was never called{cond}")


def _max_retries(rule: MaxRetries, log: EventLog) -> EvalResult:
    calls = log.tool_calls(rule.tool)
    findings, unknown_ids = [], []
    for c in calls:
        attempts = log.attempts(c)
        retries = len(attempts) - 1 if attempts else c.attributes.get("agenteval.retries")
        if not isinstance(retries, int):
            unknown_ids.append(c.id)
            findings.append(Finding(kind="unknown", event_ids=[c.id], message=f"retry count of {c.id} is not recorded"))
        elif retries > rule.max_retries:
            findings.append(Finding(kind="violation", event_ids=[c.id] + [a.id for a in attempts],
                                    message=f"{c.id} was retried {retries} times ({len(attempts)} attempts); "
                                            f"limit is {rule.max_retries}"))
    violations = [f for f in findings if f.kind == "violation"]
    scope = "any tool" if rule.tool == "*" else rule.tool
    if violations:
        return _result(rule, log, "fail", [i for f in violations for i in f.event_ids], violations[0].message, findings)
    if unknown_ids or not log.complete:
        reason = findings[0].message if findings else _incomplete(log)
        return _result(rule, log, "unknown", unknown_ids or [c.id for c in calls], reason, findings)
    if not calls:
        return _result(rule, log, "pass", [], f"no {scope} calls, so no retries")
    return _result(rule, log, "pass", [c.id for c in calls],
                   f"all {len(calls)} {scope} call(s) stayed within {rule.max_retries} retries")


def _required_tool_order(rule: RequiredToolOrder, log: EventLog) -> EvalResult:
    first = {t: (log.tool_calls(t) or [None])[0] for t in rule.sequence}
    findings = []
    for a, b in zip(rule.sequence, rule.sequence[1:]):
        fb, fa = first[b], first[a]
        if fb is None:
            continue
        if fa is None:
            hints = [o.id for o in log.orphans() if o.tool == a]
            msg = f"{fb.id} called {b}, but {a} was never called before it"
            if hints:
                msg += f" (orphan events {', '.join(hints)} suggest a {a} call whose span is missing)"
            findings.append(Finding(kind="violation", event_ids=[fb.id] + hints, message=msg))
        elif fa.index > fb.index:
            findings.append(Finding(kind="violation", event_ids=[fb.id, fa.id],
                                    message=f"{fb.id} called {b} before {a} was first called ({fa.id})"))
    if findings and log.complete:
        return _result(rule, log, "fail", [i for f in findings for i in f.event_ids], findings[0].message, findings)
    if findings or not log.complete:
        findings = [f.model_copy(update={"kind": "unknown"}) for f in findings]
        reason = (findings[0].message + "; " if findings else "") + f"cannot prove order because {_incomplete(log)}"
        ids = [i for f in findings for i in f.event_ids] or [e.id for e in first.values() if e]
        return _result(rule, log, "unknown", ids, reason, findings)
    called = [e.id for e in first.values() if e]
    return _result(rule, log, "pass", called, "every called step was preceded by the step before it in "
                   + " -> ".join(rule.sequence))


_EVALUATORS = {"requires_prior_event": _requires_prior_event, "forbidden_tool_call": _forbidden_tool_call,
               "max_retries": _max_retries, "required_tool_order": _required_tool_order}


def evaluate(rule: _Rule, log: EventLog) -> EvalResult:
    return _EVALUATORS[rule.semantic()["type"]](rule, log)
