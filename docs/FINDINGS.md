# AgentEvalLab findings: LangSmith vs Opik vs Braintrust vs Raindrop (Oct 2026)

## What was tested

- **The agent:** a small lead-research agent. It has 3 mocked tools: search a company, get its
  details, and save an assessment, which requires permission. It runs with a deterministic scripted
  model (free and repeatable) and was also run once live on Gemini Flash-Lite (free tier).
- **Five test cases:**
  - a normal run
  - a temporary tool failure (a timeout, then a 503, then recovery)
  - conflicting company records
  - a save attempt without permission
  - a "noisy" run where every tool fails at least once, with exactly one 429 error hidden as a needle
- **A regression experiment:** a "candidate" prompt change fixes the conflicting-records case but
  breaks the permission rule.
- **The same data on every platform:** the same traces and the same five deterministic checks were sent
  to all four. Each platform's native evaluation compared baseline against candidate.

## Did each platform catch the regression and the needle?

| | LangSmith | Braintrust | Opik | Raindrop |
|---|---|---|---|---|
| Regression caught | Yes (red/green compare) | Yes (per-row diff, plus a terminal summary "1 improvement, 1 regression") | Yes | n/a: no code-based evals on the free plan |
| Found the 429 needle | No: the compare view is too cramped and errors aren't marked | Yes, fast: red error icons inline | Yes: error-first sorting, warning icons | Yes, fast: errors in red, auto-created tool signals |
| Built-in AI help | Needs your own API key | Has an assistant | Ollie: the best plain-English root cause and fix, but usage-limited | Triage Agent, 15 messages/month free |
| Biggest friction | "Status" filter means "crashed", not "check failed" | Misleading metrics (+26,400% tokens; more LLM calls shown as an "improvement") | Hard to reach the experiment; 23 spans for a 4-step run, a third of them scorer bookkeeping | Traces were invisible until sent as "events", a separate concept |
| Personal take | Nicest UI | Most "alert": points you at problems | Liked the UI | Clean and simple, but the most information at once |

## Cross-platform themes

1. **Built for AI engineers, not beginners.** Answering "what went wrong?" took platform-specific
   knowledge every time.
2. **No link from "this check failed" to "this step caused it".** Viewing the trace and the scores side
   by side is cramped on every platform.
3. **Information overload.** A 4-step agent produces 11–23 spans. Real agents produce hundreds.
   Humans want this order: verdict, then which check failed, then which step caused it, then the raw
   detail on request.
4. **Full detail suits AI reviewers, not humans.** An agent can read the raw traces through the
   APIs and MCP servers that all four platforms offer.
5. **The incumbents are adding AI assistants** (Ollie, Braintrust's assistant, Raindrop's Triage
   Agent), but none shows how accurate its explanations are.

## Opportunity to brainstorm

Agents are going mainstream. PMs, founders and ordinary people are building agents with Claude or
similar tools, for example a personal assistant. They want to test whether a workflow works without
learning spans, experiments and feedback scores.

- **Possible product:** a beginner-first agent testing tool.
  - Describe what the agent should and shouldn't do in plain language. It turns that into
    deterministic checks.
  - Run the tests and get a one-page plain-English report: verdict → what changed → the one step that
    caused it → a suggested fix.
  - Pass or fail comes from deterministic checks. AI only explains, and the explanations' accuracy is
    measured against known answers.
  - It sits on top of any platform (LangSmith, Opik, Braintrust, Raindrop) or none.

**Risks and open questions:**
- Platform assistants (Ollie and similar) or Claude itself could absorb this. What stays defensible:
  working across platforms, a verdict-first UX for non-engineers, or proven explanation accuracy?
- Which first user matters most: a PM reviewing agent changes, a non-technical founder, or a hobbyist
  building a personal assistant?
- Will beginners write test cases at all, or do the tests need to be generated from example
  conversations?
- How do you make money when the incumbents' free tiers are generous (5k–25k traces a month)?

**Cheap next step:** an `agenteval explain` prototype that produces the plain-English report from
this repo's traces and checks. Show it next to the four dashboards to 3–5 target users and measure who
understands the failure faster.
