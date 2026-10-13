# Status

## AgentPatch MVP (`agentpatch/`)

| Area | State |
|---|---|
| LangGraph capture (callbacks, subgraph agents, graph structure), Python SDK, AgentEvalLab + OTLP import | done, tested |
| Suites (TOML), deterministic checks with step evidence and "can't verify" | done, tested |
| Baseline vs candidate comparison, failure diagnosis, coverage gaps and drift | done, tested on the demo, the multi-agent example and AgentEvalLab traces |
| CLI with CI exit codes and Markdown/JSON reports; local dashboard | done; the dashboard was exercised in a browser |
| Async (`ainvoke`) and streaming graphs; semantic output checks; retention | not done (see `agentpatch/README.md`, Limitations) |

Tests: `cd agentpatch && uv run pytest`, fully offline.

## AgentEvalLab

| Milestone | State |
|---|---|
| 1. Offline agent, synthetic data, tests | done |
| 2. OpenTelemetry tracing and canonical local JSON traces | done |
| 3. Deterministic evals and regression comparison | done (scenario 5 detected) |
| 4. Optional Gemini integration | done; tested against a fake client, never called live |
| 5. LangSmith, Opik, Braintrust, Raindrop adapters | done; dry-run plans tested, SDK calls checked offline with sockets blocked |
| 6. README and comparison guide | done; LangSmith hands-on notes recorded, other platforms pending |
| 7. Noisy "needle" case and full traces in native-eval rows | done; LangSmith checked offline via `evaluate(upload_results=False)` |

Test suite: `uv run pytest`, fully offline.

## Pending user approval (nothing hosted has been called)

- Live Gemini runs need `--approve-live` and `GEMINI_API_KEY`.
- Hosted trace uploads and native evals need `--approve-upload` and the provider's key.

## Known limitations and blockers

- **Gemini free tier:** `gemini-3.5-flash-lite` is listed as free on the pricing page (checked Oct 2026).
  The docs no longer publish per-model free RPM/RPD limits; check them in AI Studio. Eligibility is
  per project, so confirm the key's project has no billing linked if you need a strict $0 setup.
- **Raindrop native eval:** there is no deterministic code-scorer harness. Signals are LLM-drafted,
  Experiments need Pro, and replay evals need a hosted TypeScript endpoint. `eval --native raindrop`
  reports this and does nothing.
- **Raindrop SDK:** it cannot replay explicit timestamps or LLM spans, so the adapter uses the
  documented OTLP endpoint.
- **Opik docs:** the docs site returned HTTP 451 to the fetcher, so its API was verified from the
  docs source in the `comet-ml/opik` repo and the SDK source.
- **Offline token counts** are estimates (characters ÷ 4) and are flagged with `estimated: true`.
