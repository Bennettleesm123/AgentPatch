# AgentPatch

**Know what broke, why it broke, and whether your agent is still observable.**

AgentPatch runs your agent (LangGraph first-class, plain Python too) on a small suite of cases,
before and after a change, and tells you:

1. **What broke:** which cases regressed, consistently or intermittently, judged per case and per
   check. Aggregate success rates can hide a regression behind an unrelated fix.
2. **Why (where to look):** a *likely failure point* in the trace (a specific step id such as
   `tool:save_assessment#1`), the facts that support it with step citations, and the first place the
   run diverged from baseline. It is chosen by fixed, documented rules and is **not** a proven root cause.
3. **Still observable?:** instrumentation gaps that the trace itself proves (*verified*), patterns that
   often mean missing instrumentation (*possible*), and structural drift between runs: nodes or tools
   that appeared or disappeared, and capture rates that dropped.

Everything runs locally. Results go to `.agentpatch/` and the dashboard is a local web page. The
core has no dependencies, and LangGraph support is an optional extra.

## Quickstart (offline demo, about 10 seconds)

```bash
cd agentpatch
uv sync                      # Python 3.11+; installs langgraph for the demo and tests
uv run agentpatch demo       # runs baseline + candidate, prints the comparison, opens the dashboard
```

The demo is a LangGraph lead-research agent with a simulated model and CRM, so it needs no API keys
or network. The "candidate" changes the agent's policy and adds an `enrich` node. AgentPatch reports:

- `unauthorized` **regressed** 3/3 → 0/3. The likely failure point is `tool:save_assessment#1`, a
  write the agent no longer refrains from (cited by the failed check, with a 403 error).
- `noisy` **regressed intermittently** 3/3 → 2/3. The likely failure point is `node:enrich#1`: a 429
  from a funding API that the new node calls without retries.
- `conflict` **fixed**. The aggregate success rate barely moves (80% → 73%), which is exactly why the
  report is per case.
- **Verified gap:** the model requests `get_funding`, but no tool step is ever recorded, because the new
  node calls the API directly instead of through a tool.
- **Possible gap:** `node:enrich` spends about 120 ms that no recorded child step accounts for.
- Drift: new node `enrich`, new tool `get_funding` that no check covers, and the flow change.

Use `agentpatch demo --no-ui` to print only the comparison. Use `agentpatch ui` to reopen the
dashboard later.

## Your LangGraph agent: no code changes

```bash
pip install -e "path/to/AgentPatch/agentpatch[langgraph]"   # into your agent's environment
cd my-agent-project
agentpatch init              # writes agentpatch.toml and detects `graph = builder.compile()` targets
agentpatch inspect           # runs the first case once: shows every captured step + any gaps
agentpatch run --label baseline
# ...change prompts, tools, models or graph...
agentpatch run --label candidate
agentpatch compare           # terminal report; exit code 1 on regression
agentpatch ui                # dashboard at http://127.0.0.1:8765
```

The `[agent] target` can be any of:

- a compiled graph (`my_agent.py:graph` or `package.module:graph`);
- a zero-argument factory that returns one (recommended when the graph holds state between runs);
- a plain function `fn(input) -> output`.

AgentPatch invokes the graph with its LangChain callback handler. Graph nodes, chat-model calls (model,
token usage, requested tool calls), tool runs, routers (conditional edges) and retrievers become
steps. Each run gets its own `thread_id`, so checkpointers don't leak state between trials.

**Multi-agent graphs.** Compiled subgraphs used as nodes (supervisor and worker patterns) are recorded
as one node per agent, with that agent's inner nodes namespaced as `<agent>/<node>`, e.g.
`researcher/think` and `writer/tools`. Declared structure includes subgraph nodes, so drift and coverage
work per agent. See `examples/multi_agent/` (offline):

```bash
uv run agentpatch run --suite examples/multi_agent/agentpatch.toml --label baseline
WRITER_SKIPS_FACTS=1 uv run agentpatch run --suite examples/multi_agent/agentpatch.toml --label candidate
uv run agentpatch compare     # regression; failure point = the writer's model call that stopped requesting fact_check
```

Running a suite calls your agent for real: `cases × repeats` invocations. If it uses a paid model,
that costs money, so start with `repeats = 1` while writing the suite.

## Recording inside your own code

If you'd rather capture real invocations than run a suite:

```python
import agentpatch

with agentpatch.trace("refund_request", input=payload) as rec:
    result = graph.invoke(payload, config={"callbacks": [agentpatch.callback_handler()]})
    rec.set_output(result)
rec.save("agentpatch_traces/")          # -> agentpatch_traces/refund_request__0.json
```

Then `agentpatch import agentpatch_traces/ --label prod --suite agentpatch.toml`. Cases are matched
by id; anything not in the suite only gets the `completes` check.

For plain Python agents (no LangChain), mark the pieces yourself:

```python
@agentpatch.tool                     # each call becomes a tool step (args, result, error)
def search(query): ...

def run(inp):                        # use as [agent] target = "agent.py:run"
    with agentpatch.step("plan"):    # a node step
        agentpatch.log_llm("gpt-4o-mini", input=prompt, output=text,
                           tool_calls=[{"name": "search"}], usage={"total_tokens": 812})
    return {"answer": search(inp["q"])}
```

## Importing existing traces

`agentpatch import <files or dirs> --label <label> [--suite agentpatch.toml]` accepts:

- AgentPatch traces (`agentpatch.trace.v1`, from `rec.save()` or `inspect --save`);
- AgentEvalLab traces (`../artifacts/traces/*.json`). See `examples/agenteval_import.toml`;
- OTLP/JSON exports (`resourceSpans`) using OpenTelemetry GenAI (`gen_ai.*`) or OpenInference
  attributes. Set the case id with the span attribute `agentpatch.case_id`.

If a suite's `output` paths are missing from every imported trace, the import warns you. That is
usually a format mismatch rather than a regression.

## Suite reference (`agentpatch.toml`)

```toml
name = "support-bot"

[agent]
target = "bot.py:graph"
repeats = 3                     # trials per case (1-50); flaky behaviour needs more than one
config = { recursion_limit = 25, configurable = { model = "small" } }   # optional, passed to graph.invoke

[defaults]                      # prepended to every case's checks
checks = [{ type = "completes" }, { type = "max_latency_ms", value = 30000 }]

[[cases]]
id = "refund"
description = "Refund for a damaged item"
input = { messages = [["user", "My order arrived broken, refund please"]] }
checks = [
  { type = "tool_called", tool = "lookup_order" },
  { type = "tool_not_called", tool = "delete_account" },
  { type = "output", path = "messages.-1.content", contains = "refund" },
]

[thresholds]                    # optional; defaults shown
latency_pct = 25                # p50 slowdown worth reporting (%) ...
latency_min_ms = 50             # ... and at least this many ms
tool_calls_pct = 50             # change in calls per run worth reporting
tokens_pct = 25
untraced_min_ms = 100           # "possible gap": node time not covered by child steps
untraced_ratio = 0.5

[coverage]                      # optional: turn silent instrumentation loss into a verified gap
expected_tools = ["lookup_order"]
expected_nodes = ["agent", "tools"]
```

| check | options | passes when |
|---|---|---|
| `completes` | | the run finishes without an unhandled error |
| `output` | `path` + one of `equals`, `not_equals`, `contains`, `one_of`, `exists` | a field of the final output matches (`a.b.0`, `messages.-1.content`) |
| `tool_called` | `tool`, `min` (1), `max` | the tool is called within the bounds |
| `tool_not_called` | `tool` | the tool is never called |
| `tool_order` | `sequence` | the tools are first called in this order |
| `no_tool_errors` | | no tool call errors, even if it was later retried |
| `max_latency_ms` | `value` | total run time ≤ value |
| `max_tool_calls` | `value`, `tool` | tool calls (optionally of one tool) ≤ value |
| `max_llm_calls` | `value` | model calls ≤ value |

Every check result is *pass*, *fail* or **can't verify**. A tool check is "can't verify" when the model
requested that tool but no tool step was recorded, so AgentPatch won't claim it wasn't called. Failed
checks cite the step ids that support them. Invalid suites fail fast with a specific message.

## Reading the results

- **Verdict:** `REGRESSION` (any critical finding), `NEEDS REVIEW` (warnings), `IMPROVEMENT` (fixes
  only) or `NO CHANGE`. Cases that fail in both runs are called out in the headline.
- **Case changes:** *regressed* (the candidate never passes, or its pass rate dropped by ≥ 50 points),
  *intermittent* (a smaller drop), *fixed*, *still failing*, *unchanged*. Trial counts are always shown.
  With fewer than 3 trials, the report notes that flakiness can't be ruled out.
- **Findings** are ranked by severity (critical, warning, info) and deduplicated. A new error that
  explains a regression is folded into that regression, and a tool that disappeared is reported once.
- **Likely failure point**, in order of precedence:
  1. a step cited by a failed check;
  2. where the run-ending error started;
  3. for a required tool that never ran, the model call at the turn where baseline requested it;
  4. the last model call;
  5. the first divergence from baseline.

  The label says whether it is *backed by check evidence* or a *heuristic*.
- **Coverage levels:** *verified* means the trace contradicts itself: a requested tool with no tool step,
  a step whose parent is missing, a step that never ended, an error without a message, an empty trace,
  or a declared expectation that wasn't met. *Possible* means it might be fine: node time not covered by
  children, a declared node never reached, or a check on a tool that never appeared. *Info* covers
  missing token usage or tool input/output.

## CI

```bash
agentpatch run --label candidate
agentpatch compare baseline candidate --md report.md     # exit 1 on regression
agentpatch compare --fail-on warning                     # stricter: also fail on warnings
agentpatch compare --md > comment.md                     # Markdown to stdout, e.g. for a PR comment
agentpatch compare --json > comparison.json
agentpatch coverage                                      # exit 1 if the latest run has verified gaps
```

Exit codes: 0 means OK, 1 means the gate failed, 2 means a usage or configuration error (with a
message saying what to fix). `compare` without arguments uses the latest `baseline` and the latest
`candidate`. Refer to runs by label (the latest with that label), full id, or id prefix
(`agentpatch runs` lists them).

## Dashboard

`agentpatch ui` serves on `127.0.0.1` only. It has four views:

- **Overview:** the verdict, the three questions, ranked findings with evidence links, and per-case
  changes.
- **Case:** the checks per run; why the selected candidate trial failed (failure point, cited facts,
  next steps); a step-by-step baseline/candidate diff with the failure point highlighted and unchanged
  stretches collapsed; and the full step trees with inputs and outputs.
- **Observability:** the gaps table (baseline vs candidate counts), drift, and capture rates.
- **Runs:** pick which runs to compare. Picking the run already on the other side swaps them.

Trace content is always rendered as text, and the page's Content-Security-Policy allows only its own
scripts.

## Data handling

- Traces stay in `.agentpatch/` (add it to `.gitignore`; the repo's `.gitignore` already does). Set
  `AGENTPATCH_HOME` or pass `--home` to move it.
- Values under keys like `api_key`, `token`, `secret`, `password`, `authorization` or `cookie` are
  replaced with `[REDACTED]`. So is any environment-variable value whose name contains `KEY`, `TOKEN`,
  `SECRET` or `PASSWORD`, wherever it appears. Strings are capped at 2000 characters, and lists and
  dicts at 50 items.
- Nothing is uploaded anywhere. The tests block all non-localhost sockets.

## Limitations (honest list)

- **Diagnosis is heuristic.** The likely failure point is where to start reading, chosen by the rules
  above, and the "next steps" are templates. AgentPatch doesn't read prompts semantically and doesn't
  use an LLM judge.
- **Coverage is limited to what touches instrumentation.** Work done outside callbacks and decorators
  (a raw HTTP call inside a node, a background thread) shows up only indirectly, as a *possible* gap
  when it takes ≥ 100 ms, or as a *verified* gap when a model requested it as a tool. Fast or
  unrequested uninstrumented work is invisible.
- **Checks are deterministic.** There is no semantic or LLM-graded output check yet. Use `contains`,
  `one_of` or structured outputs.
- **Trial counts are small by design.** "Intermittent" with 3 trials is a prompt to run more, not a
  statistic. There are no confidence intervals.
- **Async LangGraph** (`ainvoke`) is not exercised by the runner, which calls `invoke`. Callback
  capture should work, but it is untested. Streaming-only graphs are untested too.
- **Tested with** LangGraph 1.x and langchain-core 1.x. OTLP import covers GenAI and OpenInference
  attribute names, not every vendor's conventions.
- **Storage** is JSON files, fine for hundreds of traces, not for production-scale volumes. There is
  no retention or cleanup command yet.

## Layout

```
src/agentpatch/
  schema.py      trace format, readable step ids, redaction       recorder.py  trace/step/tool/log_llm SDK
  integrations/langgraph.py  callback handler, graph structure    ingest.py    AgentEvalLab + OTLP importers
  suite.py       agentpatch.toml parsing/validation               checks.py    deterministic checks with evidence
  coverage.py    verified/possible gaps, drift                    diagnose.py  failure point, facts, alignment
  compare.py     baseline vs candidate findings and verdict       report.py    terminal + Markdown output
  runner.py      targets, suite runs, imports                     store.py     .agentpatch run store
  server.py      local JSON API + static files                    web/         dashboard (vanilla JS, no build)
  cli.py         agentpatch command                               demo/        offline LangGraph demo agent + suite
examples/        multi-agent LangGraph example; suite for AgentEvalLab traces
tests/           uv run pytest (offline; sockets blocked)
```
