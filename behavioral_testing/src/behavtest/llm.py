"""Optional Gemini Flash-Lite adapter for compilation and explanations.

Only constructed from the CLI with --gemini --approve-live. Model output is treated as untrusted
data: rules must pass the same registry validation as hand-written JSON (otherwise the compiler
abstains), and explanation sentences are kept only if every event they cite is allowed evidence.
Only structured event summaries are sent, never raw trace inputs/outputs.
"""

from __future__ import annotations

import json
import os
import re
import time

from agenteval.gemini import DEFAULT_MODEL
from agenteval.settings import redact, require_env
from pydantic import ValidationError

from .compiler import CompileResult
from .diagnosis import describe
from .events import EventLog
from .rules import CONTEXT_KEYS, ENTITY_PATHS, TOOLS, parse_rule, rule_schema

COMPILE_PROMPT = """You translate one agent requirement into a JSON rule, or abstain.
Tools: {tools}. Entity paths per tool (entity_key, trigger_entity_path): {entities}.
Context keys for forbidden_tool_call.when_context: {context}. Retries = attempts - 1.
Rule JSON schema: {schema}
Return exactly one JSON object: {{"status": "compiled"|"ambiguous"|"unsupported", "rule": <rule or null>,
"issues": [<short strings>]}}. Use "ambiguous" for hedged/vague wording or unnamed tools, "unsupported" when
the requirement (including any condition or qualifier) cannot be fully expressed by the schema. Never drop a
condition to make a rule fit. Omit "id" and "source".
Requirement: {requirement}"""

EXPLAIN_PROMPT = """Explain this behavioral test result to a developer in at most 4 short sentences.
Every sentence must end with the event ids it relies on in square brackets, e.g. [tool.save_assessment#1].
Only use these events; do not speculate beyond them, and label causes as possible, not certain.
Return JSON: {{"sentences": ["...", ...]}}
Requirement: {requirement}
Verdict: {verdict}. Reason: {reason}
Events:
{events}"""

_CITE = re.compile(r"\[([^\]]+)\]\s*\.?\s*$")


class CallBudgetExceeded(SystemExit):
    """Stops the command; deliberately not an Exception so model-failure handlers can't swallow it."""


class GeminiCompiler:
    def __init__(self, model: str | None = None, max_calls: int = 40, client=None, timeout_s: float = 30.0,
                 min_interval_s: float = 4.0):
        self.model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        self.max_calls = max_calls
        self.calls = 0
        self.tokens = 0
        self.failures = 0
        self.dropped_sentences = 0
        self.min_interval_s = min_interval_s
        self._last = 0.0
        if client is None:
            try:
                from google import genai
                from google.genai import types
            except ImportError as exc:
                raise SystemExit("Gemini mode needs the optional extra: uv sync --extra gemini") from exc
            client = genai.Client(api_key=require_env("GEMINI_API_KEY", "GOOGLE_API_KEY"),
                                  http_options=types.HttpOptions(timeout=int(timeout_s * 1000),
                                                                 retry_options=types.HttpRetryOptions(attempts=1)))
        self.client = client

    def _json(self, prompt: str) -> dict:
        if self.calls >= self.max_calls:
            raise CallBudgetExceeded(f"model call cap of {self.max_calls} reached")
        self.calls += 1
        wait = self._last + self.min_interval_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        resp = self.client.models.generate_content(
            model=self.model, contents=prompt,
            config={"response_mime_type": "application/json", "temperature": 0})
        usage = getattr(resp, "usage_metadata", None)
        self.tokens += int(getattr(usage, "total_token_count", 0) or 0)
        data = json.loads(resp.text)
        if not isinstance(data, dict):
            raise ValueError("model returned non-object JSON")
        return data

    def compile(self, requirement: str) -> CompileResult:
        prompt = COMPILE_PROMPT.format(tools=", ".join(TOOLS), entities=json.dumps(ENTITY_PATHS),
                                       context=", ".join(sorted(CONTEXT_KEYS)), schema=json.dumps(rule_schema()),
                                       requirement=requirement)
        try:
            data = self._json(prompt)
        except Exception as exc:  # network, quota, malformed JSON: abstain, never guess
            self.failures += 1
            return CompileResult(requirement=requirement, status="unsupported", method="gemini",
                                 issues=[f"model call failed: {redact(type(exc).__name__ + ': ' + str(exc))[:200]}"])
        status = data.get("status")
        issues = [str(i) for i in data.get("issues") or []][:5]
        if status in ("ambiguous", "unsupported"):
            return CompileResult(requirement=requirement, status=status, issues=issues or ["model abstained"],
                                 method="gemini")
        if status != "compiled" or not isinstance(data.get("rule"), dict):
            return CompileResult(requirement=requirement, status="unsupported", method="gemini",
                                 issues=["model output had no valid status/rule"])
        rule = {k: v for k, v in data["rule"].items() if k not in ("id", "source")}
        try:
            parsed = parse_rule(rule).with_identity(requirement)
        except ValidationError as exc:
            return CompileResult(requirement=requirement, status="unsupported", method="gemini",
                                 issues=[f"model rule failed validation: {exc.errors()[0]['msg']}"])
        return CompileResult(requirement=requirement, status="compiled", rule=parsed.model_dump(), method="gemini")

    def explain_report(self, report: dict, log: EventLog) -> None:
        """Attach cited model explanations to failed/unknown items; uncited sentences are dropped and counted."""
        for item in report["requirements"]:
            if item["verdict"] not in ("fail", "unknown"):
                continue
            allowed = set(item["diagnosis"]["allowed_citations"])
            events = "\n".join(redact(describe(log.by_id[i])) for i in log.sort_ids(allowed) if i in log.by_id)
            try:
                data = self._json(EXPLAIN_PROMPT.format(requirement=item["requirement"], verdict=item["verdict"],
                                                        reason=item["result"]["reason"], events=events))
            except Exception as exc:
                self.failures += 1
                item["model_explanation"] = {"error": redact(f"{type(exc).__name__}: {exc}")[:200]}
                continue
            kept, dropped = [], 0
            for s in data.get("sentences") or []:
                m = _CITE.search(str(s))
                cites = [c.strip() for c in m.group(1).split(",")] if m else []
                if cites and set(cites) <= allowed:
                    kept.append({"text": str(s), "cites": cites})
                else:
                    dropped += 1
            self.dropped_sentences += dropped
            item["model_explanation"] = {"model": self.model, "sentences": kept, "dropped_uncited": dropped}

    def usage(self) -> dict:
        return {"mode": "gemini", "model": self.model, "calls": self.calls, "total_tokens": self.tokens,
                "failed_calls": self.failures, "dropped_explanation_sentences": self.dropped_sentences,
                "max_calls": self.max_calls}
