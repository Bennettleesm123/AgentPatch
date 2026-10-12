# AgentEvalLab

A small Python testbed for comparing the tracing, evaluation, debugging and regression-testing
experience of **LangSmith, Opik, Braintrust and Raindrop**. Every platform gets the same agent, the same
synthetic cases, and the same recorded traces.

- **Agent:** a lead-research agent with three mocked tools: `search_company`, `get_company_details`,
  and `save_assessment`, which needs write permission. It is bounded to 5 LLM turns and 8 tool attempts,
  with per-call timeouts and at most 2 retries per call.
- **Modes:** `offline` uses a deterministic scripted model and is the default. `gemini` does real
  model-driven tool calling, but only when you opt in.
- **Tracing:** OpenTelemetry spans (root agent, then LLM turns and tools, then retry attempts) are
  exported to a canonical JSON trace in `artifacts/traces/`.
- **Evals:** five local deterministic evaluators, plus a baseline-vs-candidate regression diff.
- **Providers:** four adapters replay the saved traces after a run finishes, so no provider can change
  agent behaviour. Uploads are dry-runs unless you approve them.

## Setup

```bash
pip install uv            # if uv is not installed
uv sync                   # core deps only: opentelemetry-sdk (+ pytest)
uv run pytest             # 30 pass + Gemini module skipped (32 with --extra gemini); sockets are blocked
```

Install optional extras only when you need them: `uv sync --extra gemini --extra langsmith --extra opik
--extra braintrust --extra raindrop`, or `uv sync --all-extras`. Note that `uv sync` without extras
removes any extras already installed. The Raindrop adapter uses only the standard library, so the
`raindrop` extra is just for exploring its SDK.

## Commands (all tested)

```bash
uv run agenteval cases                                  # list the scenarios
uv run agenteval run --offline                          # run all cases with the baseline config
uv run agenteval run --offline --config candidate --case conflict
uv run agenteval eval --local                           # local evaluators, baseline + candidate
uv run agenteval compare                                # scenario 5: regression report
uv run agenteval compare --fail-on-regression           # same, but exit 1 on regressions (for CI)
uv run agenteval trace --provider langsmith --case normal   # dry-run: writes payload plan, sends nothing
uv run agenteval trace --provider all --case transient_failure
uv run agenteval eval --native braintrust               # dry-run description of the native eval
```

These commands make hosted calls, and only with the explicit approval flag, a key in `.env`, and the
matching extra installed:

```bash
uv run agenteval run --gemini --case normal --approve-live            # live Gemini (free tier), <=20 requests
uv run agenteval trace --provider opik --case normal --approve-upload # 1 trace; hard cap 3 per provider
uv run agenteval eval --native langsmith --approve-upload             # dataset + 2 experiments (8 rows max)
uv run agenteval trace --provider langsmith --from-file artifacts/traces/baseline__normal__gemini.json --approve-upload
uv run agenteval eval --local --results artifacts/results/baseline__gemini.jsonl
```

## Scenarios

| id | scenario | expected |
|---|---|---|
| `normal` | Successful research | qualified, saved |
| `transient_failure` | `get_company_details` times out, then returns 503, then succeeds | not_qualified, 2 retries |
| `conflict` | Two CRM records with the same name disagree | needs_review, both records fetched |
| `unauthorized` | Write permission denied | qualified, **no** `save_assessment` call |
| `compare` (scenario 5) | The candidate policy fetches every record and always saves | fixes `conflict`, regresses `unauthorized` |

The configs are in `src/agenteval/configs.py`. Each one pairs a natural-language system prompt (used by
Gemini) with an equivalent `policy` dict (used by the scripted model).

## Evaluators (local, deterministic)

| evaluator | checks |
|---|---|
| `decision_correct` | final decision and company_id match the expected values |
| `tool_sequence` | required tools are called in order; forbidden tools are never called |
| `retry_limits` | per-call retries ≤ 2, ≤ 8 tool attempts, ≤ 5 turns, run completed, expected retries happened |
| `conflict_handling` | the conflict is flagged only when expected, and every conflicting record was fetched |
| `no_unauthorized_writes` | no `save_assessment` attempt when write permission is denied |

`compare` diffs the pass/fail result of every (case, evaluator) pair and labels each one as
**fixed**, **regressed** or unchanged. Reports go to `artifacts/reports/`. Local results carry
`"source": "local"`; native results from a provider are saved as `artifacts/reports/native_<provider>.json`
with `"source": "native:<provider>"`.

## Layout

```
src/agenteval/
  data.py        synthetic companies + cases        tools.py    mocked tools + fault injection
  agent.py       bounded loop, timeouts, retries    models.py   scripted model
  gemini.py      google-genai manual function calls tracing.py  OTel -> canonical JSON
  evals.py       evaluators + regression diff       cli.py      CLI
  providers/     langsmith.py opik.py braintrust.py raindrop.py base.py (ledger, replay helpers)
docs/COMPARISON.md   platform comparison matrix + manual review guide
docs/STATUS.md       milestone status and known limitations
```

## Zero-spend safeguards

- Offline is the default, and tests block all socket connections.
- Gemini needs `--gemini --approve-live` and a key. The model defaults to `gemini-3.5-flash-lite`, which
  the pricing page lists as free of charge. SDK-level retries are disabled, and a cap of 20 requests per
  invocation applies. The code never falls back to another model.
- Hosted uploads need `--approve-upload` and one named provider. `artifacts/upload_ledger.json` enforces
  at most 3 trace uploads and 8 native-eval rows per provider.
- Native evals replay recorded offline outputs and score them with the deterministic evaluators.
  No LLM judges are used.
- Secrets live only in the gitignored `.env`. Error messages pass through `redact()`, and dry-run
  payloads contain no keys.
- Free-tier data may be used to improve Google's products, so only synthetic data is used.
