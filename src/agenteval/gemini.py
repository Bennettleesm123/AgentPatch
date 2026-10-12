"""Gemini Developer API backend using google-genai with manual function calling.

Only constructed when the user explicitly runs `--gemini --approve-live`.
"""

from __future__ import annotations

import os

from .models import ModelError, ModelResponse, ToolCall, TransientModelError
from .settings import redact, require_env

# Listed as "Free of charge" on https://ai.google.dev/gemini-api/docs/pricing (checked 2026-10).
DEFAULT_MODEL = "gemini-3.5-flash-lite"
_SYNTH_ID = "gemini_call_"


class GeminiModel:
    provider = "gemini"

    def __init__(self, model: str | None = None, timeout_s: float = 30.0, max_calls: int = 20):
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise SystemExit("Gemini mode needs the optional extra: uv sync --extra gemini") from exc
        self.types = types
        self.name = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        self.max_calls = max_calls
        self.calls = 0
        # SDK-level retries disabled: the agent loop owns bounded retries.
        self.client = genai.Client(
            api_key=require_env("GEMINI_API_KEY", "GOOGLE_API_KEY"),
            http_options=types.HttpOptions(timeout=int(timeout_s * 1000),
                                           retry_options=types.HttpRetryOptions(attempts=1)),
        )

    def _contents(self, messages: list[dict]) -> list:
        t = self.types
        out, pending = [], []
        for m in messages:
            if m["role"] == "tool":
                content = m["content"] if isinstance(m["content"], dict) else {"result": m["content"]}
                call_id = None if str(m["tool_call_id"]).startswith(_SYNTH_ID) else m["tool_call_id"]
                pending.append(t.Part(function_response=t.FunctionResponse(id=call_id, name=m["name"],
                                                                           response=content)))
                continue
            if pending:
                out.append(t.Content(role="user", parts=pending))
                pending = []
            if m["role"] == "user":
                out.append(t.Content(role="user", parts=[t.Part.from_text(text=m["content"])]))
            else:
                out.append(m["raw"])
        if pending:
            out.append(t.Content(role="user", parts=pending))
        return out

    def generate(self, system: str, messages: list[dict], tools: list[dict]) -> ModelResponse:
        from google.genai import errors

        if self.calls >= self.max_calls:
            raise ModelError(f"live-call safety cap of {self.max_calls} Gemini requests reached")
        self.calls += 1
        t = self.types
        decls = [t.FunctionDeclaration(name=x["name"], description=x["description"],
                                       parameters_json_schema=x["parameters"]) for x in tools]
        cfg = t.GenerateContentConfig(
            system_instruction=system, temperature=0, tools=[t.Tool(function_declarations=decls)],
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
        )
        try:
            resp = self.client.models.generate_content(model=self.name, contents=self._contents(messages), config=cfg)
        except errors.APIError as exc:
            msg = redact(f"Gemini API error {exc.code} {exc.status}: {exc.message}")
            if exc.code == 429 or (exc.code or 0) >= 500:
                raise TransientModelError(msg) from None
            raise ModelError(msg) from None
        calls = [ToolCall(id=fc.id or f"{_SYNTH_ID}{i}", name=fc.name, args=dict(fc.args or {}))
                 for i, fc in enumerate(resp.function_calls or [])]
        text = None
        if not calls and resp.candidates and resp.candidates[0].content:
            text = "".join(p.text or "" for p in resp.candidates[0].content.parts or [])
        um = resp.usage_metadata
        usage = {}
        if um:
            out_tokens = (um.candidates_token_count or 0) + (um.thoughts_token_count or 0)
            usage = {"input_tokens": um.prompt_token_count or 0, "output_tokens": out_tokens,
                     "total_tokens": um.total_token_count or 0}
        raw = resp.candidates[0].content if resp.candidates else None
        return ModelResponse(text=text, tool_calls=calls, model=resp.model_version or self.name, usage=usage, raw=raw)
