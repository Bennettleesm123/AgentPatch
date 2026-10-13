"""agentpatch command line."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from . import store
from .report import Style, compare_markdown, compare_text, coverage_text, steps_tree
from .schema import write_json

TEMPLATE = '''# AgentPatch suite. Docs: https://github.com/Bennettleesm123/AgentPatch/tree/main/agentpatch
name = "{name}"

[agent]
# A compiled LangGraph (`module:graph`), a zero-argument factory that returns one, or a plain
# function `fn(input) -> output`. Paths like `agent.py:graph` work too.
target = "{target}"
repeats = 3            # runs per case; flaky behaviour needs more than one

[defaults]             # checks applied to every case
checks = [
  {{ type = "completes" }},
  {{ type = "max_latency_ms", value = 30000 }},
]

[[cases]]
id = "example"
description = "Replace with a real request your agent handles"
input = {input}
checks = [
  # {{ type = "tool_called", tool = "search" }},
  # {{ type = "tool_not_called", tool = "delete_record" }},
  # {{ type = "output", path = "messages.-1.content", contains = "refund" }},
]

# [coverage]
# expected_tools = ["search"]     # warn if a run never records these
'''


def _detect_targets(root: Path) -> list[str]:
    found = []
    for py in sorted(root.glob("*.py")) + sorted(root.glob("*/*.py")):
        if ".venv" in py.parts or py.name.startswith("test_"):
            continue
        try:
            text = py.read_text(errors="ignore")
        except OSError:
            continue
        if "StateGraph" not in text:
            continue
        for m in re.finditer(r"^(\w+)\s*=\s*\w+\.compile\(", text, re.M):
            found.append(f"{py.relative_to(root).as_posix()}:{m.group(1)}")
        for m in re.finditer(r"^def (\w+)\(\s*\)[^:]*:\s*\n(?:[ \t]+.*\n)*?[ \t]+return [^\n]*\.compile\(", text, re.M):
            found.append(f"{py.relative_to(root).as_posix()}:{m.group(1)}")
    return found


def cmd_init(a, st):
    path = Path(a.suite)
    if path.exists() and not a.force:
        print(f"{path} already exists (use --force to overwrite)")
        return 1
    targets = [a.target] if a.target else _detect_targets(Path.cwd())
    target = targets[0] if targets else "my_agent:graph"
    path.write_text(TEMPLATE.format(name=Path.cwd().name, target=target,
                                    input='{ messages = [["user", "Hello"]] }'))
    print(f"wrote {path}")
    if a.target:
        pass
    elif targets:
        print(f"detected LangGraph target {target}" + (f" (others: {', '.join(targets[1:])})" if targets[1:] else ""))
    else:
        print("no compiled LangGraph found here; edit [agent] target in the file")
    print("next:\n  agentpatch inspect             # run one case, see what gets captured\n"
          "  agentpatch run --label baseline\n  (change your agent)\n  agentpatch run --label candidate\n"
          "  agentpatch compare && agentpatch ui")
    return 0


def _suite(a):
    from .suite import load_suite

    return load_suite(a.suite)


def cmd_inspect(a, st):
    from .coverage import trace_gaps
    from .runner import Agent, load_target, run_once

    suite = None
    if Path(a.suite).is_file():
        suite = _suite(a)
    spec = a.target or (suite.target if suite else None)
    if not spec:
        print("no target: pass --target module:attr or create a suite with `agentpatch init`")
        return 2
    if a.input is not None:
        payload, case_id = json.loads(a.input), "adhoc"
    elif suite:
        case = suite.case(a.case) if a.case else suite.cases[0]
        if case is None:
            print(f"unknown case {a.case!r}")
            return 2
        payload, case_id = case.input, case.id
    else:
        print("give --input '<json>' or a suite with cases")
        return 2
    agent = Agent(load_target(spec, suite.base_dir if suite else None))
    trace = run_once(agent, payload, case_id)
    print(st.bold(f"Captured {len(trace.steps)} steps for `{case_id}` ({agent.kind}, {trace.duration_ms:.0f} ms, "
                  f"{'ok' if trace.status == 'ok' else 'FAILED: ' + str(trace.error)})"))
    print(steps_tree(trace, st))
    gaps = trace_gaps(trace, suite.thresholds if suite else None)
    struct = agent.structure()
    if struct:
        seen = {s.name for s in trace.steps if s.kind == "node"}
        missing = [n for n in struct["nodes"] if n not in seen]
        print(st.dim(f"\nGraph declares nodes: {', '.join(struct['nodes'])}"
                     + (f"; not reached in this run: {', '.join(missing)}" if missing else "")))
    print()
    if not gaps:
        print(st.green("No instrumentation gaps detected in this trace."))
    for g in gaps:
        color = st.red if g["level"] == "verified" else st.yellow if g["level"] == "possible" else st.dim
        print(color(f"[{g['level']}] {g['message']}"))
    if a.save:
        write_json(Path(a.save), trace.to_dict())
        print(st.dim(f"trace written to {a.save}"))
    return 0


def cmd_run(a, st):
    from .runner import run_suite

    suite = _suite(a)
    marks = {"pass": st.green("✓"), "fail": st.red("✗"), "unknown": st.yellow("?")}
    current = {"case": None}

    def progress(case_id, trial, res):
        if current["case"] != case_id:
            if current["case"] is not None:
                print()
            print(f"  {case_id:<28}", end="", flush=True)
            current["case"] = case_id
        print(marks[res["outcome"]], end="", flush=True)

    print(st.bold(f"Running suite `{suite.name}` as `{a.label}`") + st.dim(f" ({a.target or suite.target})"))
    run = run_suite(suite, a.label, target=a.target, repeats=a.repeats, root=a.home, cases=a.case,
                    progress=progress)
    res = run.results()
    passed = sum(r["outcome"] == "pass" for r in res)
    print(f"\n\n{passed}/{len(res)} trials passed. Saved run {run.id}")
    others = [r for r in store.list_runs(a.home) if r.id != run.id]
    if others:
        base = next((r for r in others if r.label == "baseline"), others[0])
        print(st.dim(f"compare with: agentpatch compare {base.label if base.label != a.label else base.id} {run.id}"))
    return 0


def cmd_compare(a, st):
    from .compare import compare

    runs = store.list_runs(a.home)
    if len(runs) < 2 and not (a.baseline and a.candidate):
        print("need two runs to compare; create them with `agentpatch run --label baseline` and "
              "`agentpatch run --label candidate` (or try `agentpatch demo`)")
        return 2
    base = store.resolve(a.baseline, a.home) if a.baseline else _default_pair(runs)[0]
    cand = store.resolve(a.candidate, a.home) if a.candidate else _default_pair(runs)[1]
    cmp = compare(base, cand)
    if a.md == "-":
        print(compare_markdown(cmp), end="")
    elif a.json == "-":
        print(json.dumps(cmp, indent=2, default=str))
    else:
        print(compare_text(cmp, st, limit=a.limit))
        if a.md:
            Path(a.md).write_text(compare_markdown(cmp))
            print(st.dim(f"\nMarkdown report: {a.md}"))
        if a.json:
            write_json(Path(a.json), cmp)
            print(st.dim(f"JSON report: {a.json}"))
        print(st.dim("\nDashboard: agentpatch ui"))
    fail = {"regression": {"regression"}, "warning": {"regression", "needs_review"}, "never": set()}[a.fail_on]
    return 1 if cmp["verdict"] in fail else 0


def _default_pair(runs):
    by_label = {}
    for r in runs:
        by_label.setdefault(r.label, r)
    if "baseline" in by_label and "candidate" in by_label:
        return by_label["baseline"], by_label["candidate"]
    newest = runs[0]
    older = next((r for r in runs[1:] if r.label != newest.label), runs[1])
    return older, newest


def cmd_coverage(a, st):
    from .coverage import run_coverage

    run = store.resolve(a.run, a.home) if a.run else (store.list_runs(a.home) or [None])[0]
    if run is None:
        print("no runs yet; create one with `agentpatch run --label baseline`")
        return 2
    cov = run_coverage(run.traces(), run.info.get("graph"), set(run.info.get("checked_tools") or []),
                       run.info.get("coverage_expectations"), run.info.get("thresholds"))
    print(coverage_text(run.id, cov, st))
    return 1 if cov["summary"]["verified"] else 0


def cmd_import(a, st):
    from .ingest import load_file
    from .runner import import_traces

    files = []
    for p in a.paths:
        path = Path(p)
        files += sorted(path.glob("*.json")) if path.is_dir() else [path]
    if not files:
        print("no trace files found")
        return 2
    traces = []
    for f in files:
        try:
            traces += load_file(f)
        except (ValueError, OSError) as exc:
            print(st.yellow(f"skipped {f}: {exc}"))
    if not traces:
        print("nothing imported")
        return 2
    suite = _suite(a) if a.suite != "agentpatch.toml" or Path(a.suite).is_file() else None
    run = import_traces(traces, a.label, suite, a.home, source=traces[0].source)
    print(f"imported {len(traces)} trace(s) from {len(files)} file(s) as run {run.id}"
          + (f" (checked against `{suite.name}`)" if suite else " (no suite: only 'completes' is checked)"))
    if run.info.get("unmatched_cases"):
        print(st.yellow("cases not in the suite (only 'completes' checked): " + ", ".join(run.info["unmatched_cases"])))
    _warn_missing_paths(run.results(), st)
    return 0


def _warn_missing_paths(results: list[dict], st) -> None:
    """Flag output checks whose path is missing from every trace: usually a suite/trace-format mismatch."""
    found, missing = set(), set()
    for r in results:
        for c in r["checks"]:
            if c["type"] == "output":
                path = c["id"].split(":")[1]
                is_missing = c["passed"] is False and "is missing" in (c.get("detail") or "")
                (missing if is_missing else found).add(path)
    always = sorted(missing - found)
    if always:
        print(st.yellow("output path(s) missing from every imported trace: " + ", ".join(f"`{p}`" for p in always)
                        + ". The suite's paths may not match this trace format; inspect one trace's output with "
                          "`agentpatch ui` or the trace file and adjust `path`."))


def cmd_runs(a, st):
    runs = store.list_runs(a.home)
    if not runs:
        print("no runs yet")
        return 0
    for r in runs:
        res = r.results()
        passed = sum(x["outcome"] == "pass" for x in res)
        git = (r.info.get("git") or {}).get("commit") or ""
        print(f"  {r.id:<36} {r.label:<12} {passed:>3}/{len(res):<3} passed  {r.info.get('target') or r.info.get('source')}"
              f"  {git}")
    return 0


def cmd_ui(a, st):
    from .server import serve

    serve(a.home, port=a.port, open_browser=not a.no_open)
    return 0


def cmd_demo(a, st):
    from .compare import compare
    from .demo import SUITE_PATH
    from .integrations import langgraph_available
    from .runner import run_suite
    from .suite import load_suite

    if not langgraph_available():
        print("the demo needs LangGraph: pip install 'agentpatch[langgraph]' (or `uv sync` in agentpatch/)")
        return 2
    suite = load_suite(SUITE_PATH)
    print(st.bold("AgentPatch demo: an offline LangGraph lead-research agent, before and after a change."))
    print(st.dim("No network or API keys are used; the model and CRM are simulated.\n"))
    runs = []
    for label, factory in (("baseline", "baseline"), ("candidate", "candidate")):
        print(f"  running {label:<10}", end="", flush=True)
        run = run_suite(suite, label, target=f"agentpatch.demo.lead_agent:{factory}", root=a.home,
                        progress=lambda *_: print(".", end="", flush=True))
        print(f" {sum(r['outcome'] == 'pass' for r in run.results())}/{len(run.results())} trials passed")
        runs.append(run)
    cmp = compare(*runs)
    print()
    print(compare_text(cmp, st, limit=8))
    if a.no_ui:
        print(st.dim("\nOpen the dashboard with: agentpatch ui"))
        return 0
    print()
    return cmd_ui(a, st)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="agentpatch", description="Know what broke, why it broke, and whether your "
                                "agent is still observable.")
    p.add_argument("--home", default=None, help="run store directory (default .agentpatch or $AGENTPATCH_HOME)")
    p.add_argument("--no-color", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def suite_arg(sp):
        sp.add_argument("--suite", default="agentpatch.toml", help="suite file (default agentpatch.toml)")

    sp = sub.add_parser("init", help="create agentpatch.toml (detects LangGraph graphs in this folder)")
    suite_arg(sp)
    sp.add_argument("--target")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_init)

    sp = sub.add_parser("inspect", help="run one input and show what was captured, plus instrumentation gaps")
    suite_arg(sp)
    sp.add_argument("--target")
    sp.add_argument("--input", help="JSON input (default: the suite's first case)")
    sp.add_argument("--case")
    sp.add_argument("--save", help="write the captured trace to this file")
    sp.set_defaults(fn=cmd_inspect)

    sp = sub.add_parser("run", help="run the suite and store the results under a label")
    suite_arg(sp)
    sp.add_argument("--label", required=True, help="e.g. baseline or candidate")
    sp.add_argument("--target", help="override [agent] target")
    sp.add_argument("--repeats", type=int)
    sp.add_argument("--case", action="append", help="only this case (repeatable)")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("compare", help="compare two runs (default: latest baseline vs latest candidate)")
    sp.add_argument("baseline", nargs="?")
    sp.add_argument("candidate", nargs="?")
    sp.add_argument("--md", nargs="?", const="-", metavar="FILE",
                    help="write a Markdown report (e.g. for a PR comment); no FILE prints it instead")
    sp.add_argument("--json", nargs="?", const="-", metavar="FILE",
                    help="write the full comparison as JSON; no FILE prints it instead")
    sp.add_argument("--fail-on", choices=("regression", "warning", "never"), default="regression",
                    help="exit 1 when the verdict is at least this bad (for CI)")
    sp.add_argument("--limit", type=int, default=12, help="findings to print")
    sp.set_defaults(fn=cmd_compare)

    sp = sub.add_parser("coverage", help="observability coverage of one run (default: latest)")
    sp.add_argument("run", nargs="?")
    sp.set_defaults(fn=cmd_coverage)

    sp = sub.add_parser("import", help="import existing traces (AgentPatch, AgentEvalLab, OTLP JSON) as a run")
    sp.add_argument("paths", nargs="+")
    sp.add_argument("--label", required=True)
    suite_arg(sp)
    sp.set_defaults(fn=cmd_import)

    sp = sub.add_parser("runs", help="list stored runs")
    sp.set_defaults(fn=cmd_runs)

    sp = sub.add_parser("ui", help="open the dashboard")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--no-open", action="store_true", help="don't open a browser")
    sp.set_defaults(fn=cmd_ui)

    sp = sub.add_parser("demo", help="run the offline LangGraph demo (baseline vs candidate) and open the dashboard")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--no-open", action="store_true")
    sp.add_argument("--no-ui", action="store_true", help="print the comparison only")
    sp.set_defaults(fn=cmd_demo)

    a = p.parse_args(argv)
    st = Style(False if a.no_color else None)
    from .runner import TargetError
    from .suite import SuiteError

    try:
        code = a.fn(a, st)
        sys.stdout.flush()
        return code
    except (SuiteError, TargetError, LookupError, json.JSONDecodeError) as exc:
        print(st.red(f"error: {exc}"), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
