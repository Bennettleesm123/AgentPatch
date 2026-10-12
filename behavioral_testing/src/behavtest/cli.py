"""behavtest CLI: compile | events | evaluate | diagnose | benchmark. Offline unless --gemini --approve-live."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agenteval.settings import load_dotenv, redact

from .benchmark import run_benchmark, to_markdown, write_benchmark
from .compiler import CompileResult, compile_requirement
from .events import EventLog, build_events, load_events
from .report import check, to_markdown as report_md, write_report

OUT = Path("artifacts/behavioral")


def _compiler(args):
    if not getattr(args, "gemini", False):
        return compile_requirement, None
    if not args.approve_live:
        raise SystemExit("--gemini makes live model requests; re-run with --approve-live to allow them")
    from .llm import GeminiCompiler

    gc = GeminiCompiler(max_calls=args.max_calls)
    return gc.compile, gc


def _trace(args) -> EventLog:
    if args.trace:
        return load_events(args.trace)
    if not args.agent_case:
        raise SystemExit("give --trace PATH or --agent-case CASE (with --config)")
    from .fixtures import build_trace

    trace = build_trace({"agent_case": args.agent_case, "config": args.config,
                         "mutations": json.loads(Path(args.mutations).read_text()) if args.mutations else []})
    path = OUT / "traces" / f"{args.config}__{args.agent_case}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trace, indent=1))
    print(f"trace: {path}", file=sys.stderr)
    return build_events(trace)


def _requirements(args, compiler) -> list[CompileResult]:
    reqs = list(args.rule or [])
    if args.rules_file:
        reqs += [ln.strip() for ln in Path(args.rules_file).read_text().splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")]
    if not reqs:
        raise SystemExit("give at least one --rule \"...\" or --rules-file")
    return [compiler(r) for r in reqs]


def _print(obj) -> None:
    print(redact(json.dumps(obj, indent=2, default=str)))


def main(argv=None) -> int:
    load_dotenv(".env")
    load_dotenv("../.env")
    p = argparse.ArgumentParser(prog="behavtest", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def live(sp):
        sp.add_argument("--gemini", action="store_true", help="use Gemini Flash-Lite (live, needs --approve-live)")
        sp.add_argument("--approve-live", action="store_true", help="explicitly allow live model requests")
        sp.add_argument("--max-calls", type=int, default=40, help="hard cap on model calls for this command")

    def source(sp):
        sp.add_argument("--trace", help="canonical agenteval trace JSON")
        sp.add_argument("--agent-case", help="run the offline sample agent on this case (normal, noisy, ...)")
        sp.add_argument("--config", default="baseline", help="agent config for --agent-case")
        sp.add_argument("--mutations", help="JSON file with fixture mutations to apply (testing only)")

    c = sub.add_parser("compile", help="requirement -> rule JSON")
    c.add_argument("requirement")
    live(c)
    ev = sub.add_parser("events", help="list trace events and their ids")
    source(ev)
    for name in ("evaluate", "diagnose"):
        sp = sub.add_parser(name, help="check requirements against a trace" if name == "evaluate"
                            else "full evidence-linked report (writes JSON + Markdown)")
        sp.add_argument("--rule", action="append", help="plain-English requirement (repeatable)")
        sp.add_argument("--rules-file", help="file with one requirement per line")
        source(sp)
        live(sp)
        if name == "diagnose":
            sp.add_argument("--out", default=str(OUT / "reports"))
            sp.add_argument("--explain", action="store_true", help="add a Gemini explanation (needs --approve-live)")
    b = sub.add_parser("benchmark", help="accuracy benchmark over cases/ground_truth.json")
    b.add_argument("--out", default=str(OUT))
    live(b)
    args = p.parse_args(argv)

    if args.cmd == "compile":
        compiler, _ = _compiler(args)
        res = compiler(args.requirement)
        _print(res.model_dump())
        return 0 if res.status == "compiled" else 2
    if args.cmd == "events":
        log = _trace(args)
        for e in log.events:
            print(redact(e.brief()))
        if log.integrity_issues:
            print("integrity: " + "; ".join(log.integrity_issues))
        return 0
    if args.cmd in ("evaluate", "diagnose"):
        compiler, client = _compiler(args)
        log = _trace(args)
        report = check(_requirements(args, compiler), log)
        if args.cmd == "evaluate":
            _print({"overall": report["overall"], "results": [
                {"requirement": i["requirement"], "verdict": i["verdict"],
                 **({k: i["result"][k] for k in ("rule_id", "evidence", "reason")} if "result" in i
                    else {"compile": i["compile"]["status"], "issues": i["compile"]["issues"]})}
                for i in report["requirements"]]})
        else:
            if args.explain:
                if not (args.gemini and args.approve_live):
                    raise SystemExit("--explain needs --gemini --approve-live")
                client.explain_report(report, log)
            jp, mp = write_report(report, log, Path(args.out))
            print(report_md(report, log))
            print(f"wrote {jp} and {mp}")
        return {"pass": 0, "fail": 1}.get(report["overall"], 3)
    if args.cmd == "benchmark":
        compiler, client = _compiler(args)
        usage = client.usage() if client else None
        res = run_benchmark(compiler, model_usage=usage)
        if client:
            res["model_usage"] = client.usage()
        jp, mp = write_benchmark(res, Path(args.out))
        print(to_markdown(res))
        print(f"wrote {jp} and {mp}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
