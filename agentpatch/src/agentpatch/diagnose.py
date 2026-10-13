"""Failure diagnosis for one failing trace, optionally against a baseline trace of the same case.

Output separates what the trace shows (facts, each citing step ids) from what AgentPatch infers
(the likely failure point, with the rule that picked it). It never claims a root cause: the
"likely failure point" is where to start looking, chosen by fixed, documented rules.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter

from .checks import get_path
from .schema import Step, Trace

ALIGN_KINDS = ("node", "llm", "tool", "retriever")
ERROR_HINTS = [
    (re.compile(r"\b429\b|rate.?limit|too many requests", re.I), "rate limit / HTTP 429"),
    (re.compile(r"\b403\b|forbidden|permission|unauthori[sz]ed", re.I), "permission denied / HTTP 403"),
    (re.compile(r"\b401\b|unauthenticated|invalid api key", re.I), "authentication failure / HTTP 401"),
    (re.compile(r"timed? ?out|timeout|deadline", re.I), "timeout"),
    (re.compile(r"\b5\d\d\b|unavailable|bad gateway", re.I), "upstream server error / 5xx"),
    (re.compile(r"\b404\b|not found", re.I), "not found / HTTP 404"),
    (re.compile(r"recursion limit|GraphRecursionError", re.I), "graph recursion limit (possible loop)"),
    (re.compile(r"validation|JSONDecodeError|parse", re.I), "malformed data / parsing error"),
]


def error_hint(message: str | None) -> str | None:
    for rx, label in ERROR_HINTS:
        if message and rx.search(message):
            return label
    return None


def signature(s: Step) -> str:
    sig = f"{s.kind}:{s.name}"
    if s.kind == "llm":
        sig += "→" + (",".join(s.attributes.get("requested_tools") or []) or "answer")
    if s.status == "error":
        sig += " ✗"
    return sig


def _aligned(trace: Trace) -> list[Step]:
    return [s for s in trace.steps if s.kind in ALIGN_KINDS]


def align(base: Trace, cand: Trace) -> dict:
    """Step-level diff of two traces (by kind, name, requested tools and error status)."""
    b, c = _aligned(base), _aligned(cand)
    sm = difflib.SequenceMatcher(a=[signature(s) for s in b], b=[signature(s) for s in c], autojunk=False)
    blocks, mapping, rows = [], {}, []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        for k in range(max(i2 - i1, j2 - j1)):
            rows.append({"tag": tag, "baseline": b[i1 + k].id if i1 + k < i2 else None,
                         "candidate": c[j1 + k].id if j1 + k < j2 else None})
        if tag == "equal":
            for k in range(i2 - i1):
                mapping[c[j1 + k].id] = b[i1 + k].id
            continue
        blocks.append({"tag": tag, "baseline": [s.id for s in b[i1:i2]], "candidate": [s.id for s in c[j1:j2]],
                       "baseline_sig": [signature(s) for s in b[i1:i2]],
                       "candidate_sig": [signature(s) for s in c[j1:j2]],
                       "baseline_before": b[i1 - 1].id if i1 > 0 else None,
                       "candidate_before": c[j1 - 1].id if j1 > 0 else None})
    return {"blocks": blocks, "mapping": mapping, "rows": rows, "identical": not blocks,
            "baseline_steps": len(b), "candidate_steps": len(c)}


def describe_block(block: dict) -> str:
    bs, cs = block["baseline_sig"], block["candidate_sig"]
    show = lambda xs: ", ".join(f"`{x}`" for x in xs[:4]) + (f" (+{len(xs) - 4} more)" if len(xs) > 4 else "")
    if block["tag"] == "insert":
        return f"candidate added {show(cs)}" + (f" after {block['candidate_before']}" if block["candidate_before"] else "")
    if block["tag"] == "delete":
        return f"candidate skipped baseline's {show(bs)}"
    return f"baseline did {show(bs)}; candidate did {show(cs)}"


def _decision_step(trace: Trace, failure: Step) -> Step | None:
    """The model call that most recently requested the failing tool (or preceded the failure)."""
    before = [s for s in trace.steps[:trace.order[failure.id]] if s.kind == "llm"]
    if failure.kind == "tool":
        asked = [s for s in before if failure.name in (s.attributes.get("requested_tools") or [])]
        if asked:
            return asked[-1]
    return before[-1] if before else None


def _brief(step: Step | None) -> dict | None:
    if step is None:
        return None
    out = {"id": step.id, "kind": step.kind, "name": step.name, "status": step.status, "error": step.error,
           "duration_ms": step.duration_ms, "input": step.input, "output": step.output}
    if step.kind == "llm":
        out["requested_tools"] = step.attributes.get("requested_tools") or []
        out["model"] = step.attributes.get("model")
    return out


def diagnose(cand: Trace, cand_result: dict, base: Trace | None = None, base_result: dict | None = None) -> dict:
    failed = [c for c in cand_result["checks"] if c["passed"] is False]
    unknown = [c for c in cand_result["checks"] if c["passed"] is None]
    root_id = cand.root.id if cand.root else None
    facts: list[dict] = []

    def fact(text, *cites):
        facts.append({"text": text, "cites": [c for c in cites if c]})

    for c in failed:
        fact(f"Check `{c['id']}` failed: {c['detail']}.", *c["evidence"])
    for c in unknown:
        fact(f"Check `{c['id']}` couldn't be verified: {c['detail']}.")
    origins = cand.error_origins()
    fatal = origins[-1] if cand.status == "error" and origins else None
    if fatal is not None:
        hint = error_hint(fatal.error)
        fact(f"{fatal.id} failed with `{fatal.error}` and the run stopped there"
             + (f" (classified as {hint})" if hint else "") + ".", fatal.id)
    handled = [s for s in origins if s is not fatal]
    if handled:
        kinds = Counter(f"`{s.name}`" for s in handled)
        b_handled = len([s for s in base.error_origins() if base.status == "ok" or s is not base.error_origins()[-1]]) \
            if base is not None else None
        fact(f"{len(handled)} {'other ' if fatal is not None else ''}error(s) were handled or retried without stopping the run ("
             + ", ".join(f"{k} {v}x" for k, v in kinds.items()) + ")"
             + (f"; baseline trial {base.trial} had {b_handled}." if base is not None else "."), *[s.id for s in handled[:3]])

    # Likely failure point, by fixed precedence.
    point, rule = None, None
    for c in failed:
        steps = [cand.by_id[i] for i in c["evidence"] if i in cand.by_id and i != root_id]
        if steps:
            point, rule = steps[0], f"cited as evidence by the failed check `{c['id']}`"
            break
    if point is None and cand.status == "error" and origins:
        point, rule = origins[-1], "where the error that ended the run started"
    if point is None and failed:
        llms = cand.of_kind("llm")
        if llms:
            point, rule = llms[-1], "the last model call, which produced the final output that failed the check"

    alignment = align(base, cand) if base is not None else None
    divergence = first_div = None
    if alignment and alignment["blocks"]:
        first_div = alignment["blocks"][0]
        if point is not None:
            idx = cand.order[point.id]
            hits = [b for b in alignment["blocks"] if b["candidate"] and
                    cand.order[b["candidate"][0]] <= idx <= cand.order[b["candidate"][-1]]]
            before = [b for b in alignment["blocks"] if b["candidate"] and cand.order[b["candidate"][0]] <= idx]
            divergence = (hits or before or [first_div])[-1]
        else:
            divergence = first_div
    if point is None and divergence and divergence["candidate"]:
        point, rule = cand.by_id[divergence["candidate"][0]], "the first step where the run differs from baseline"
    decision = _decision_step(cand, point) if point is not None else None

    if base is not None:
        if alignment["identical"]:
            fact(f"The baseline run (trial {base.trial}) took the same steps; the difference is in the data, "
                 "not the path.")
        else:
            fact("First difference from baseline: " + describe_block(first_div) + ".",
                 *(first_div["candidate"][:1] or [first_div["candidate_before"]]))
            if divergence is not first_div:
                fact("Difference at the failure point: " + describe_block(divergence) + ".",
                     *divergence["candidate"][:1])
        if point is not None and point.kind == "tool" and not base.tool_steps(point.name):
            fact(f"Baseline made no `{point.name}` call in this case (trial {base.trial}).", point.id)
        if point is not None and point.status == "error":
            same = [s for s in base.steps if s.kind == point.kind and s.name == point.name and s.status == "error"]
            if same:
                fact(f"Baseline also saw `{point.name}` errors here ({len(same)}x) but its run "
                     f"{'completed' if base.status == 'ok' else 'also failed'}.", point.id)
        for c in failed:
            if c["type"] == "output":
                path = c["id"].split(":")[1]
                bf, bv = get_path(base.output, path)
                cf, cv = get_path(cand.output, path)
                if bf and (not cf or bv != cv):
                    fact(f"Final output `{path}`: baseline {bv!r}, candidate {repr(cv) if cf else 'missing'}.", root_id)

    if decision is not None and point is not None and decision is not point:
        req = decision.attributes.get("requested_tools") or []
        fact(f"{decision.id} decided the next step" + (f" (requested {', '.join(f'`{r}`' for r in req)})" if req
                                                       else " (requested no tools)") + ".", decision.id)
        if base is not None and alignment and decision.id not in alignment["mapping"]:
            b_llms = base.of_kind("llm")
            k = cand.of_kind("llm").index(decision)
            if k < len(b_llms):
                breq = b_llms[k].attributes.get("requested_tools") or []
                fact(f"At the same turn in baseline, {b_llms[k].id} requested "
                     + (", ".join(f"`{r}`" for r in breq) if breq else "no tools (it answered)") + ".",
                     "baseline:" + b_llms[k].id)

    next_steps = _next_steps(cand, point, decision, failed, base)
    return {
        "case_id": cand.case_id, "trial": cand.trial, "baseline_trial": base.trial if base else None,
        "outcome": cand_result["outcome"], "failed_checks": failed, "unverified_checks": unknown,
        "likely_failure_point": None if point is None else {
            "step": _brief(point), "rule": rule, "confidence": "evidence" if rule and "cited" in rule else "heuristic",
            "error_hint": error_hint(point.error)},
        "decision_step": _brief(decision) if decision is not point else None,
        "divergence": divergence, "first_divergence": first_div, "facts": facts, "next_steps": next_steps,
        "alignment": None if alignment is None else {k: alignment[k] for k in ("identical", "blocks")},
    }


def _next_steps(cand: Trace, point: Step | None, decision: Step | None, failed: list[dict], base: Trace | None):
    out = []
    if point is None:
        if failed:
            out.append("No single step explains this failure; compare the final output with the expected value.")
        return out
    if point.kind == "tool" and any(c["type"] == "tool_not_called" for c in failed):
        out.append(f"Look at what {decision.id if decision else 'the model'} was given before it requested "
                   f"`{point.name}`: the prompt/policy or the context it saw changed.")
        out.append(f"If `{point.name}` must never run in this situation, enforce it in code (a guard before the "
                   "tool), not only in the prompt.")
    elif point.status == "error":
        hint = error_hint(point.error)
        out.append(f"Open {point.id} and read its error" + (f" ({hint})" if hint else "") + ".")
        if point.kind == "node" and not cand.children(point):
            out.append(f"`{point.name}` has no recorded child steps, so the trace can't show which call inside it "
                       "failed; wrap its external calls with @agentpatch.tool or a LangChain tool.")
        if hint in ("rate limit / HTTP 429", "timeout", "upstream server error / 5xx"):
            out.append(f"Transient-looking error: check whether `{point.name}` has retries/backoff like the other "
                       "calls, and re-run with more repeats to measure how often it happens.")
    elif decision is not None:
        out.append(f"Compare {decision.id}'s input and output with the baseline run to see where the reasoning "
                   "changed.")
    if any(c["type"] == "output" for c in failed):
        out.append("Check the final output against the expected value, then trace back to the tool result it was "
                   "based on.")
    return out
