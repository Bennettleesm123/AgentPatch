# Platform comparison: LangSmith, Opik, Braintrust, Raindrop

All four platforms receive the same canonical traces and the same 5-case dataset. Integration facts
below come from the official docs and SDK source, checked in Oct 2026 against `langsmith` 0.14.7,
`opik` 2.2.98, `braintrust` 0.45.0 and `raindrop-ai` 0.0.75. Rows marked **(measure)** are
hands-on observations: fill them in during manual review, because no hosted upload has been made yet
(see `docs/STATUS.md`).

## Matrix

| Dimension | LangSmith | Opik (Comet cloud) | Braintrust | Raindrop |
|---|---|---|---|---|
| Initial onboarding time | **(measure)** Sign up, then create an API key | **(measure)** Sign up, then copy the API key and workspace | **(measure)** Sign up, then create an API key | **(measure)** Sign up, then copy the write key |
| Integration effort (this repo) | SDK `RunTree` + `create_child`. Explicit timestamps; the whole tree is posted once (~40 lines) | SDK `client.trace`, `trace.span`, `span.span`. Must set `provider` explicitly (nested default is `openai`); ids are UUIDv7 (~50 lines) | SDK `start_span` / `log` / `end`. Times are epoch seconds; spans must be ended manually (~40 lines) | OTLP/HTTP JSON (`POST /v1/traces`), standard library only. The Python SDK cannot set timestamps or create manual LLM spans (~60 lines) |
| Span types used | chain / llm / tool | trace + llm / tool / general | task / llm / tool | LLM inferred from `gen_ai.system`; other spans generic |
| Token usage mapping | `usage_metadata` + `ls_model_name` metadata | `usage.prompt_tokens/completion_tokens/total_tokens` + `model` | `metrics.prompt_tokens/completion_tokens/tokens` + `metadata.model` | `gen_ai.usage.input_tokens/output_tokens` + `gen_ai.response.model` |
| Trace navigation / visualization | **(measure)** run tree, waterfall | **(measure)** trace tree, span detail | **(measure)** span tree, timeline | **(measure)** interaction/trace view |
| Dataset creation | `create_dataset` + `create_examples` (inputs/outputs) | `get_or_create_dataset` + `insert` (items are versioned on insert) | Inline `data=` list, or `init_dataset().insert` | Datasets exist; free-plan availability unverified |
| Native evaluation setup | `Client.evaluate(target, data, evaluators=[fn(inputs, outputs)])` | `opik.evaluation.evaluate(dataset, task, scoring_metrics=[BaseMetric])` | `Eval(project, data, task, scores=[fn(input, output, expected)])` | No code-scorer harness. Signals are LLM-drafted classifiers or presets; replay evals need a hosted TypeScript endpoint |
| Regression comparison | UI Compare across experiments (red/green vs a chosen baseline) | UI experiment compare (exact click path unverified) | `base_experiment_name` gives per-score improvements/regressions in the SDK summary and UI | Experiments compare signal rates between cohorts, but need the Pro plan |
| Built-in AI analysis of traces | Side-panel assistant needs your own model API key (observed) | **(measure)** | **(measure)** | Triage Agent, 15 messages/mo on Hobby (docs) |
| API / MCP accessibility | REST + SDK; hosted MCP `https://api.smith.langchain.com/mcp` | REST + SDK; `opik-mcp` package / hosted `.../opik/api/v1/mcp` | REST + SDK; hosted MCP `https://api.braintrust.dev/mcp` | REST + Query SDK; hosted MCP `https://mcp.raindrop.ai/mcp` |
| OTLP ingestion (alternative) | `https://api.smith.langchain.com/otel` (`x-api-key`, `Langsmith-Project`) | `https://www.comet.com/opik/api/v1/private/otel` (`Authorization`, `Comet-Workspace`, `projectName`) | `https://api.braintrust.dev/otel` (`Authorization: Bearer`, `x-bt-parent`) | `https://api.raindrop.ai/v1/traces` (`Authorization: Bearer`) |
| Error messages / UI issues | Offline: SDK logs `Failed to GET /info` and a compression warning, then continues. Hosted: **(measure)** | Offline: background batch processor logs failures but `trace()` still returns. Hosted: **(measure)** | Offline: lazy login fails at flush ("Dropping batch"). Hosted: **(measure)** | Offline: n/a (plain HTTP). Hosted: **(measure)** |
| Free tier (no card required on any of them) | Developer: 1 seat, 5k base traces/mo, 14-day retention | Free cloud: 25k spans/mo, 60-day retention. Billed by span: ~11–13 spans per run here | Starter: 1 GB processed data, 10k scores/mo, 14-day retention | Hobby: 1,000 events/mo, 1 custom signal, no Experiments |

## Hands-on observations

### LangSmith (2026-10-12)

- **Speed:** native eval of 2 experiments × 4 cases finished in under a second. No model calls are
  involved, since recorded outputs are replayed.
- **Compare view:** clear for one baseline against one candidate. With 3 or more experiments it is hard
  to tell what changed and why, because the view shows score differences but no fixed/regressed summary.
- **Trace navigation:** easy at about 11 spans per run. It will likely get cluttered with real agents
  that produce dozens of tool calls; filtering and error-jumping should be tested with a noisy case.
- **No automatic summary of failures:** the side assistant needs your own model API key. An alternative
  is to point your own assistant at the platform's MCP server so it reads the full traces.
- **Filtering gotcha:** the compare view's "Status is error" filter returned nothing, because Status
  means "the task crashed", not "a check failed". Failed checks are feedback scores, so filter on
  `no_unauthorized_writes < 1` instead. This was not obvious to a beginner. Keyword and error-message
  search works but feels complicated, and it didn't surface the error being looked for.
- **Experiment-row traces** were nearly empty in the first run. Fixed: each row now contains the full
  nested agent trace, and its output starts with a plain `summary` (decision, tools called, tool errors).

Sources:
- LangSmith: [annotate-code](https://docs.langchain.com/langsmith/annotate-code), [log-llm-trace](https://docs.langchain.com/langsmith/log-llm-trace), [evaluate](https://docs.langchain.com/langsmith/evaluate-llm-application), [OTel](https://docs.langchain.com/langsmith/trace-with-opentelemetry), [pricing](https://www.langchain.com/pricing)
- Opik: [docs](https://www.comet.com/docs/opik), [SDK source](https://github.com/comet-ml/opik/tree/main/sdks/python), [pricing](https://www.comet.com/site/pricing/)
- Braintrust: [custom traces](https://www.braintrust.dev/docs/guides/traces/customize), [scorers](https://www.braintrust.dev/docs/evaluate/write-scorers), [compare](https://www.braintrust.dev/docs/evaluate/compare-experiments), [pricing](https://www.braintrust.dev/pricing)
- Raindrop: [OpenTelemetry](https://raindrop.ai/docs/sdk/opentelemetry), [Python SDK](https://raindrop.ai/docs/sdk/python), [plans](https://raindrop.ai/docs/platform/plans.md), [signals](https://raindrop.ai/docs/platform/signals.md)

## Manual review procedure (same runs on every platform)

Budget: at most 3 trace uploads and 30 native-eval rows per provider (one native-eval run uses 10). The ledger is
`artifacts/upload_ledger.json`.

1. **Prepare (offline).** Run `uv run agenteval compare` and `uv run agenteval trace --provider all
   --case transient_failure`. Inspect the dry-run payload plans in `artifacts/provider_payloads/`.
2. **Configure one provider.** Copy `.env.example` to `.env`, add that provider's key, and run
   `uv sync --extra <provider>`.
3. **Upload the same 3 traces to each provider.** Approve each one:
   ```bash
   uv run agenteval trace --provider <p> --case normal --approve-upload
   uv run agenteval trace --provider <p> --case transient_failure --approve-upload
   uv run agenteval trace --provider <p> --case unauthorized --config candidate --approve-upload
   ```
   Together these cover the happy path, nested retries with timeout and 503 errors, and a denied write.
4. **Review each trace** and record your observations in the matrix:
   - Is the hierarchy root agent, then LLM turns and tools, then attempt spans?
   - Are tool arguments and outputs readable?
   - Do the error spans stand out?
   - Is latency shown per span, and are token counts on LLM spans?
   - Can you filter by tag or metadata (`config`, `case_id`)?
   - Time how long it takes to find the failing `get_company_details.attempt_1` span.
5. **Native eval** (LangSmith, Opik and Braintrust): run `uv run agenteval eval --native <p>
   --approve-upload`. This creates one dataset and two experiments, `native-baseline` and
   `native-candidate`. Compare them in the UI. The expected result matches `artifacts/reports/compare.md`:
   `conflict` is fixed and `unauthorized` regresses. Any mismatch between native and local results is
   an adapter bug.
6. **Raindrop:** upload the traces, then check whether the preset tool-error signals flag the 503 and
   403 spans. Native code-scored evals are unavailable on Hobby; record that limitation instead of
   working around it.
7. **Optional live run:** run `uv run agenteval run --gemini --case normal --approve-live`, then
   `trace --from-file artifacts/traces/baseline__normal__gemini.json` to compare real token usage
   across UIs. This counts toward the 3-trace cap.
8. **Needle test (clutter):** upload or native-eval the `noisy` case. It has 16 spans, 5 failed
   attempts, and exactly one `429 Too Many Requests`. Time how long it takes to find the 429 span and
   note which UI features helped (search, error filter, collapse). Then try finding "which case
   regressed" in a compare view with 3 or more experiments.
9. **Note friction:** record onboarding time, confusing errors and UI issues in the **(measure)** cells.
