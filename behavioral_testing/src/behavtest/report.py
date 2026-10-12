"""Developer report: verdict -> failed requirement -> evidence step -> explanation -> next steps."""

from __future__ import annotations

import json
from pathlib import Path

from agenteval.settings import redact

from .compiler import CompileResult
from .diagnosis import Diagnosis, describe, diagnose
from .evaluator import EvalResult, evaluate
from .events import EventLog
from .rules import parse_rule

_RANK = {"fail": 2, "unknown": 1, "pass": 0}


def check(requirements: list[CompileResult], log: EventLog) -> dict:
    """Evaluate compiled requirements on one trace and assemble the report data."""
    items = []
    for cr in requirements:
        item = {"requirement": cr.requirement, "compile": cr.model_dump()}
        if cr.status == "compiled":
            result = evaluate(parse_rule(cr.rule), log)
            item.update(result=result.model_dump(), diagnosis=diagnose(result, log).model_dump(),
                        verdict=result.verdict)
        else:
            item["verdict"] = "not_tested"
        items.append(item)
    verdicts = [i["verdict"] for i in items]
    overall = max((v if v != "not_tested" else "unknown" for v in verdicts), key=_RANK.get, default="unknown")
    return {"trace": log.meta, "trace_complete": log.complete, "integrity_issues": log.integrity_issues,
            "overall": overall, "counts": {v: verdicts.count(v) for v in ("pass", "fail", "unknown", "not_tested")},
            "requirements": items}


def _step(log: EventLog, event_id: str) -> str:
    e = log.by_id.get(event_id)
    return f"`{event_id}` (missing from trace)" if e is None else describe(e).replace(event_id, f"`{event_id}`", 1)


def to_markdown(report: dict, log: EventLog) -> str:
    m = report["trace"]
    c = report["counts"]
    lines = [f"# Behavioral check: {report['overall'].upper()}", "",
             f"Trace: case `{m.get('case_id')}`, config `{m.get('config')}`, run `{m.get('run_id')}`. "
             f"{c['pass']} passed, {c['fail']} failed, {c['unknown']} unknown, {c['not_tested']} not tested."]
    if report["integrity_issues"]:
        lines.append(f"Trace integrity: {'; '.join(report['integrity_issues'])}.")
    order = sorted(report["requirements"], key=lambda i: -_RANK.get(i["verdict"], 1))
    for item in order:
        v = item["verdict"]
        lines += ["", f"## {v.upper().replace('_', ' ')}: \"{item['requirement']}\""]
        if v == "not_tested":
            lines.append(f"Not compiled ({item['compile']['status']}): {'; '.join(item['compile']['issues'])}. "
                         "Rephrase it as one of the supported rule types.")
            continue
        r, d = EvalResult(**item["result"]), Diagnosis(**item["diagnosis"])
        lines.append(f"**What happened:** {r.reason}. (rule `{r.rule_id}`)")
        steps = (d.first_violation.event_ids if d.first_violation else r.evidence)
        if steps:
            lines += ["", "**Evidence**" + (" (first violation)" if d.first_violation else "") + ":"]
            lines += [f"- {_step(log, i)}" for i in steps]
        if v == "pass":
            continue
        extra = [f.text for f in d.facts[1:] if not any(f.cites == [i] for i in steps)]
        lines += [f"- {t}" for t in extra]
        if d.possible_causes:
            lines += ["", "**Possible causes** (not verified by the trace):"]
            lines += [f"- {f.text} [{', '.join(f.cites)}]" for f in d.possible_causes]
        if d.next_actions:
            lines += ["", "**Suggested investigation**:"] + [f"- {f.text} [{', '.join(f.cites)}]" for f in d.next_actions]
        mx = item.get("model_explanation")
        if mx:
            lines += ["", f"**Model explanation** ({mx.get('model', 'model')}; only sentences citing verdict evidence "
                      f"are kept, {mx.get('dropped_uncited', 0)} dropped):"]
            lines += [f"- {x['text']}" for x in mx.get("sentences", [])] or [f"- (none: {mx.get('error', 'no cited sentences')})"]
        others = [f for f in r.findings if f != d.first_violation]
        if v == "fail" and others:
            lines += ["", f"{len(others)} more finding(s): " + "; ".join(f.message for f in others)]
    return redact("\n".join(lines) + "\n")


def write_report(report: dict, log: EventLog, out_dir: Path, stem: str = "report") -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    jp, mp = out_dir / f"{stem}.json", out_dir / f"{stem}.md"
    jp.write_text(redact(json.dumps(report, indent=2, default=str)))
    mp.write_text(to_markdown(report, log))
    return jp, mp
