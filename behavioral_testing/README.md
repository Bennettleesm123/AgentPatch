# behavioral_testing (behavtest)

Turn plain-English agent requirements into checkable rules, run them against an agent trace, and get a
report that points at the exact trace steps behind every verdict. It reuses the `agent_testing` sample
agent (`src/agenteval`), its canonical trace format and its offline scripted runs. Offline and
deterministic by default; no model calls unless you pass `--gemini --approve-live`.

```
requirement --compile--> rule JSON --evaluate(trace events)--> pass / fail / unknown + event ids
            --diagnose--> facts, possible causes, next steps (all cite event ids) --> JSON + Markdown report
```

## Setup and tested commands

Run from `behavioral_testing/` (uses the repo's `uv`; installs `agenteval` from `..` in editable mode).

```bash
uv sync
uv run pytest                                   # 95 tests, network blocked, no keys needed

uv run behavtest compile "Get user approval before calling save_assessment."
uv run behavtest events --agent-case noisy --config candidate          # list event ids
uv run behavtest evaluate --agent-case unauthorized --config candidate \
  --rule "Never call save_assessment when write permission is denied." \
  --rule "Do not retry any tool more than 2 times."
uv run behavtest diagnose --agent-case noisy --config candidate \
  --rule "Make at most 2 attempts per tool call." --out artifacts/behavioral/reports
(cd .. && uv run agenteval run --case normal --config baseline)   # writes a root-level trace
uv run behavtest diagnose --trace ../artifacts/traces/baseline__normal__offline.json \
  --rule "Get user approval before calling save_assessment."
uv run behavtest benchmark                       # writes artifacts/behavioral/benchmark.{json,md}
```

`--agent-case` runs the existing offline scripted agent (cases: `normal`, `transient_failure`, `conflict`,
`unauthorized`, `noisy`; configs: `baseline`, `candidate`) and saves the trace under
`artifacts/behavioral/traces/`. `--trace` accepts any canonical `agenteval.trace.v1` JSON, e.g. one written by
`uv run agenteval run` at the repo root. `--rules-file` takes one requirement per line.
Exit codes: `compile` 0 = compiled, 2 = abstained; `evaluate`/`diagnose` 0 = pass, 1 = fail, 3 = unknown.

## Supported requirements

| rule type | example wording | meaning |
|---|---|---|
| `requires_prior_event` | "Get user approval before calling save_assessment." | each call needs an *earlier*, *granted* approval for the same operation, scope (write for `save_assessment`) and entity (`company_id`) |
| `forbidden_tool_call` | "Never call save_assessment when write permission is denied." | the tool must not be called (optionally only when the run context matches, e.g. `authorized=false`) |
| `max_retries` | "Retry get_company_details at most once." / "Make at most 2 attempts per tool call." | retries = attempts - 1, per call |
| `required_tool_order` | "Call search_company before get_company_details, then save_assessment." | precedence: whenever a later step is called, the earlier step must have been called before it |

Rules are Pydantic models in a registry (`rules.py`); unknown fields, tools or context keys are rejected.
No generated code is ever executed and trace text is only read as data. The compiler returns
`ambiguous` for hedged or vague wording ("usually", "a few", "the company tool") and `unsupported` for
anything the four types can't express, including any extra qualifier it can't account for ("on weekends",
"for a different company", "twice"), so it abstains rather than dropping a condition.

## Verdict rules

- `unknown` is never treated as a pass. A report with any `unknown` or untested requirement is at best `unknown`.
- Violations that rely on an event being *absent* (missing approval, step never called) are only `fail`
  on a complete trace. If the trace is flagged incomplete or has orphan spans (parent missing), they become
  `unknown`. Observed violations (a forbidden call, too many attempts) stay `fail`.
- Approvals must match operation, decision, scope and entity. If the entity can't be compared, the result
  is `unknown`. Approval-like text in tool outputs or chat messages is not an approval event.
- Event ids are `span name#occurrence` (e.g. `tool.save_assessment#1`), so they're stable across runs.
- Diagnosis separates observed facts, possible causes (a fixed catalogue per rule type, marked unverified)
  and next actions. Every claim cites event ids, restricted to the verdict evidence, the run root and the
  LLM turn that chose the call.

## Benchmark results

`uv run behavtest benchmark`, offline, 0 model calls (Linux VM, Python 3.12):

| metric | value |
|---|---|
| compile status accuracy (30 ground-truth requirements) | 1.000 |
| semantic rule accuracy (25 that should compile) | 1.000 |
| correct abstention (5 ambiguous/unsupported) | 1.000; ambiguous-vs-unsupported label 1.000 |
| unsafe compiles / false abstentions | 0 / 0 |
| paraphrase set A, 18 (status / semantic / unsafe) | first run 0.889 / 0.909 / 1; now 1.000 / 1.000 / 0 |
| paraphrase set B, 12 (status / semantic / unsafe) | first run 0.833 / 0.750 / 2; now 0.917 / 0.875 / 0 |
| verdict accuracy with gold rules (25 traces) | 1.000; pass, fail, unknown precision and recall all 1.000 (support 9 / 13 / 3) |
| unknown reported as pass | 0 |
| verdict accuracy end-to-end (compiled rule) | 1.000 |
| evidence: exact set match / mean Jaccard | 1.000 / 1.000 |
| first-violation match (13 fails) | 1.000 |
| unsupported explanation claims / decoy citations | 0 of 95 / 0 |
| latency p50 / p95 per requirement | compile 0.2 / 0.8 ms, evaluate 0.03 / 0.04 ms, diagnose 0.03 / 0.05 ms |
| model usage | offline, 0 calls |

How to read these numbers honestly:

- **The ground-truth set is not independent.** The compiler templates and the 30 cases were written
  together, so 100% there mainly shows the system does what was specified. It is not evidence of
  generalisation.
- **The paraphrase sets are the more honest compile signal.** On their first runs the compiler made 3
  unsafe compiles: it dropped "on weekends", dropped "twice", and inverted "Never call X before Y". I then
  fixed those failure modes generally (unknown-word guard, negated ordering), so both sets have now been
  used once for tuning and the "now" numbers are optimistic. The remaining miss is a safe abstention
  (`h02`, "No assessment may be saved unless the user has approved it.").
- **The 0 unsupported-claims result is structural.** Diagnosis claims are templated with citations, so
  this metric only becomes informative for model-written explanations (the `--explain` path, where
  uncited sentences are dropped and counted).
- **Traces come from one sample agent.** It has 3 tools, and the mutations are synthetic (inserted
  approvals and noise, dropped spans, an incomplete flag).

## Optional Gemini mode (live, opt-in)

Not run in tests or the benchmark above. It requires the `gemini` extra, `GEMINI_API_KEY` in a gitignored
`.env` (this folder or the repo root), and explicit approval on every command:

```bash
uv sync --extra gemini
uv run behavtest compile --gemini --approve-live "Ask for confirmation before you store the assessment."
uv run behavtest diagnose --gemini --approve-live --explain --agent-case normal --rule "..."
uv run behavtest benchmark --gemini --approve-live   # 60 compile calls, ~4 min at 1 call / 4 s
```

How it behaves:
- It uses `gemini-3.5-flash-lite` (the repo's free-tier default; override with `GEMINI_MODEL`).
- Each command has a hard call cap: `--max-calls`, default 40, or 60 for `benchmark`.
- Calls are paced at one every 4 seconds, and there is no fallback to another model.
- Model rules go through the same registry validation. Invalid JSON, invalid rules and errors all abstain.
- Explanations receive only structured event summaries, never raw trace inputs or outputs. Sentences
  without valid event citations are dropped and counted in `model_usage`.
- The adapter's logic is tested with a fake client only. It has not yet been run against the live API
  from this folder.

## Known limitations

- **Templated compiler.** It understands the wording patterns above. Unfamiliar phrasing usually
  abstains (safe) but may still mis-compile; check the rule JSON before trusting a verdict.
- **Fixed rule set.** Four rule types; one context key (`authorized`, read from the root span input);
  entity matching only by the fixed per-tool paths in `rules.ENTITY_PATHS`.
- **Simplified retry and approval semantics.**
  - Retries are counted per call span (attempt children, falling back to `agenteval.retries`), not across
    separate calls of the same tool.
  - Approval scope must match exactly; a write approval does not cover a read.
- **Synthetic approvals.** The sample agent never emits approval events, so all approval traces come from
  synthetic mutations. Real agents must log approvals as `kind: "approval"` spans with
  `approval.operation`, `approval.entity`, `approval.scope` and `approval.decision` attributes.
- **Templated diagnoses.** Possible causes come from a small fixed catalogue: helpful pointers, not root
  cause analysis.

## Layout

`src/behavtest/`:
- `rules.py`: rule registry
- `compiler.py`: requirement to rule
- `events.py`: trace to events
- `evaluator.py`
- `diagnosis.py`
- `report.py`
- `fixtures.py`: base traces and mutations
- `benchmark.py`
- `llm.py`: optional Gemini
- `cli.py`

`cases/`:
- `ground_truth.json`: 30 cases
- `paraphrases.json` (set A) and `heldout.json` (set B): compile-only
