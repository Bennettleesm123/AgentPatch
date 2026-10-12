"""agenteval command-line interface. Offline by default; hosted actions need explicit approval flags."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent import ARTIFACTS, Limits, run_case
from .configs import load_config
from .data import CASES, CASES_BY_ID, SCENARIO_5, get_cases
from .evals import EVALUATORS, compare_markdown, compare_reports, evaluate_records
from .models import build_model
from .settings import load_dotenv, redact, require_env
from .tracing import load_trace

RESULTS = ARTIFACTS / "results"
REPORTS = ARTIFACTS / "reports"


def _write_json(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str))
    return path


def run_configs(config_name: str, mode: str, case_id: str | None, args=None) -> list[dict]:
    config = load_config(config_name)
    kwargs = {"max_calls": args.max_live_calls} if mode == "gemini" else {}
    model = build_model(mode, config, **kwargs)
    limits = Limits(backoff_s=1.0) if mode == "gemini" else Limits()
    records = [run_case(c, config, model, mode, limits) for c in get_cases(case_id)]
    path = RESULTS / f"{config.name}__{mode}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, default=str) + "\n" for r in records))
    return records


def _print_runs(records: list[dict]) -> None:
    print(f"{'case':<18} {'config':<10} {'status':<15} {'decision':<14} {'tools':>5} {'turns':>5} {'ms':>8}  trace")
    for r in records:
        print(f"{r['case_id']:<18} {r['config']:<10} {r['status']:<15} {str((r['final'] or {}).get('decision')):<14} "
              f"{r['tool_attempts']:>5} {r['llm_turns']:>5} {r['latency_ms']:>8.1f}  {r['trace_path']}")
        if r["status"] != "completed":
            print(f"  -> {(r['final'] or {}).get('reason') or r['limit_hit']}")


def _print_report(report: dict) -> None:
    names = list(EVALUATORS)
    print(f"[local evaluators] config={report['config']} mode={report['mode']}")
    print(f"{'case':<18} " + " ".join(f"{n[:14]:>14}" for n in names) + "   overall")
    for cid, c in report["cases"].items():
        cells = " ".join(f"{'pass' if c['evals'][n]['passed'] else 'FAIL':>14}" for n in names)
        print(f"{cid:<18} {cells}   {'PASS' if c['passed'] else 'FAIL'}")
    s = report["summary"]
    print(f"passed {s['passed']}/{s['cases']} cases")


def cmd_run(args) -> int:
    mode = "gemini" if args.gemini else "offline"
    if mode == "gemini":
        if not args.approve_live:
            print("Gemini mode makes live API calls. Re-run with --approve-live to confirm "
                  "(free-tier model only; see docs/STATUS.md). Nothing was sent.")
            return 2
        require_env("GEMINI_API_KEY", "GOOGLE_API_KEY")
    records = run_configs(args.config, mode, args.case, args)
    _print_runs(records)
    return 0


def _load_records(path: str) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def cmd_eval(args) -> int:
    if args.native:
        return _native_eval(args)
    if args.results:
        reports = [evaluate_records(_load_records(p)) for p in args.results]
    else:
        reports = [evaluate_records(run_configs(c, "offline", args.case)) for c in args.config.split(",")]
    for rep in reports:
        _print_report(rep)
        print("report:", _write_json(REPORTS / f"eval_{rep['config']}__{rep['mode']}.json", rep))
    return 0


def cmd_compare(args) -> int:
    if args.results:
        base_recs, cand_recs = _load_records(args.results[0]), _load_records(args.results[1])
    else:
        base_recs = run_configs(args.baseline, "offline", None)
        cand_recs = run_configs(args.candidate, "offline", None)
    base, cand = evaluate_records(base_recs), evaluate_records(cand_recs)
    cmp = compare_reports(base, cand)
    _write_json(REPORTS / "compare.json", cmp)
    md = compare_markdown(cmp)
    (REPORTS / "compare.md").write_text(md)
    print(f"Scenario 5: {SCENARIO_5}\n")
    print(md)
    print(f"fixed:     {', '.join(cmp['fixed']) or '-'}")
    print(f"regressed: {', '.join(cmp['regressed']) or '-'}")
    print(f"report: {REPORTS / 'compare.md'}")
    return 1 if args.fail_on_regression and cmp["regressed"] else 0


def cmd_trace(args) -> int:
    from .providers import PROVIDERS, get_adapter
    from .providers.base import MAX_TRACE_UPLOADS, Ledger

    providers = PROVIDERS if args.provider == "all" else (args.provider,)
    if args.approve_upload and len(providers) > 1:
        print("--approve-upload requires a single --provider.")
        return 2
    if args.from_file:
        trace = load_trace(args.from_file)
    else:
        trace = load_trace(run_configs(args.config, "offline", args.case)[0]["trace_path"])
    print(f"trace {trace['trace_id']} case={trace['case_id']} config={trace['config']} mode={trace['mode']} "
          f"spans={len(trace['spans'])}")
    for name in providers:
        adapter = get_adapter(name)
        if not args.approve_upload:
            path = _write_json(ARTIFACTS / "provider_payloads" / f"{name}__{trace['config']}__{trace['case_id']}.json",
                               adapter.plan(trace))
            print(f"[dry-run] {name}: nothing sent. Payload plan: {path}")
            continue
        require_env(*adapter.ENV)
        Ledger().reserve(name, "traces", 1, MAX_TRACE_UPLOADS)
        try:
            print(f"[uploaded] {name}: {adapter.upload(trace)}")
        except Exception as exc:  # surface provider errors without leaking secrets
            print(f"[error] {name}: {redact(f'{type(exc).__name__}: {exc}')}")
            return 1
    if not args.approve_upload:
        print("Add --approve-upload (single provider) to send this trace. "
              f"Cap: {MAX_TRACE_UPLOADS} trace uploads per provider.")
    return 0


def _native_eval(args) -> int:
    from .providers import NativeEvalUnsupported, get_adapter
    from .providers.base import MAX_EVAL_ROWS, Ledger

    adapter = get_adapter(args.native)
    if not getattr(adapter, "NATIVE_EVAL", True):
        try:
            adapter.native_eval(None, [], [])
        except NativeEvalUnsupported as exc:
            print(f"[native:{args.native}] not available: {exc}")
        return 0
    # The provider dataset always holds every case, so experiments always cover all of them.
    case_ids = [c.id for c in CASES]
    configs = args.config.split(",")
    rows = len(case_ids) * len(configs)
    if not args.approve_upload:
        print(f"[dry-run] native eval on {args.native}: would sync a dataset of {len(case_ids)} items and run "
              f"{len(configs)} experiments ({rows} rows). Each row re-runs the offline scripted agent (no LLM "
              f"calls), nests its full trace, and scores it with {list(EVALUATORS)}. No LLM judges. "
              "Add --approve-upload to run. Nothing was sent.")
        return 0
    require_env(*adapter.ENV)

    def run_fn(config_name: str, case_id: str):
        config = load_config(config_name)
        rec = run_case(CASES_BY_ID[case_id], config, build_model("offline", config), "offline", Limits(),
                       out_dir=ARTIFACTS / "traces" / "native")
        return rec, load_trace(rec["trace_path"])

    try:
        Ledger().reserve(args.native, "eval_rows", rows, MAX_EVAL_ROWS)
        result = adapter.native_eval(run_fn, configs, case_ids)
    except NativeEvalUnsupported as exc:
        print(f"[native:{args.native}] not available: {exc}")
        return 0
    except Exception as exc:
        print(f"[error] {args.native}: {redact(f'{type(exc).__name__}: {exc}')}")
        return 1
    result = {"source": f"native:{args.native}", **result}
    print(json.dumps(result, indent=2, default=str))
    _write_json(REPORTS / f"native_{args.native}.json", result)
    return 0


def cmd_cases(args) -> int:
    for c in CASES:
        print(f"{c.id:<18} authorized={str(c.authorized):<5} expected={c.expected.decision:<13} {c.scenario}")
    print(f"{'(compare)':<18} {SCENARIO_5}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agenteval", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the agent")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--offline", action="store_true", help="deterministic scripted model (default)")
    g.add_argument("--gemini", action="store_true", help="live Gemini tool calling (needs --approve-live)")
    r.add_argument("--case", default="all")
    r.add_argument("--config", default="baseline", help="baseline | candidate | path/to/config.json")
    r.add_argument("--approve-live", action="store_true", help="explicitly allow live model API calls")
    r.add_argument("--max-live-calls", type=int, default=20, help="hard cap on Gemini requests per invocation")
    r.set_defaults(fn=cmd_run)

    e = sub.add_parser("eval", help="evaluate runs (local deterministic evaluators by default)")
    e.add_argument("--local", action="store_true", help="local deterministic evaluators (default)")
    e.add_argument("--native", choices=["langsmith", "opik", "braintrust", "raindrop"],
                   help="run the provider's native eval harness (needs --approve-upload)")
    e.add_argument("--config", default="baseline,candidate")
    e.add_argument("--case", default="all")
    e.add_argument("--results", nargs="+", help="evaluate existing results JSONL instead of re-running offline")
    e.add_argument("--approve-upload", action="store_true")
    e.set_defaults(fn=cmd_eval)

    c = sub.add_parser("compare", help="baseline vs candidate regression detection")
    c.add_argument("--baseline", default="baseline")
    c.add_argument("--candidate", default="candidate")
    c.add_argument("--results", nargs=2, metavar=("BASELINE_JSONL", "CANDIDATE_JSONL"))
    c.add_argument("--fail-on-regression", action="store_true", help="exit 1 if any evaluator regressed")
    c.set_defaults(fn=cmd_compare)

    t = sub.add_parser("trace", help="replay a canonical trace to a provider (dry-run unless approved)")
    t.add_argument("--provider", required=True, choices=["langsmith", "opik", "braintrust", "raindrop", "all"])
    t.add_argument("--case", default="normal")
    t.add_argument("--config", default="baseline")
    t.add_argument("--from-file", help="upload an existing canonical trace JSON instead of running offline")
    t.add_argument("--approve-upload", action="store_true", help="explicitly allow one hosted trace upload")
    t.set_defaults(fn=cmd_trace)

    k = sub.add_parser("cases", help="list scenarios")
    k.set_defaults(fn=cmd_cases)
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
