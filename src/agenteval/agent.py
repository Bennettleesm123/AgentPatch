"""Bounded tool-calling agent loop. Identical for every model backend and tracing provider."""

from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass
from pathlib import Path

from .configs import AgentConfig
from .data import Case
from .models import Model, ModelError, ModelResponse
from .tools import TOOL_SCHEMAS, ToolContext, ToolError, ToolTimeoutError, call_tool
from .tracing import Tracer, write_trace

ARTIFACTS = Path("artifacts")


@dataclass(frozen=True)
class Limits:
    max_llm_turns: int = 5
    max_tool_calls: int = 8  # counts every attempt, including retries
    max_retries: int = 2
    tool_timeout_s: float = 0.1
    model_timeout_s: float = 60.0
    backoff_s: float = 0.0


class BudgetExceeded(Exception):
    pass


def _with_timeout(fn, timeout_s: float, exc_type, label: str):
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(fn).result(timeout=timeout_s)
    except FutureTimeout:
        raise exc_type(f"{label} timed out after {timeout_s}s") from None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def user_prompt(case: Case) -> str:
    perm = "granted" if case.authorized else "denied"
    return (f'Research the company "{case.company}" and assess it as a sales lead.\n'
            f"Write permission for save_assessment: {perm}.")


def parse_final(text: str | None) -> dict:
    if not text:
        return {"decision": "no_answer"}
    s = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start, end = s.find("{"), s.rfind("}")
    try:
        return json.loads(s[start:end + 1])
    except ValueError:
        return {"decision": "unparseable", "raw": text}


class AgentRun:
    def __init__(self, case: Case, config: AgentConfig, model: Model, tracer: Tracer, limits: Limits):
        self.case, self.config, self.model, self.tracer, self.limits = case, config, model, tracer, limits
        self.ctx = ToolContext(authorized=case.authorized, faults={k: list(v) for k, v in case.faults.items()})
        self.tool_attempts = 0
        self.llm_turns = 0
        self.tool_log: list[dict] = []
        self.usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        self.limit_hit: str | None = None

    def _call_model(self, messages: list[dict]) -> ModelResponse:
        public = [{k: v for k, v in m.items() if k not in ("raw", "meta")} for m in messages]
        attrs = {"gen_ai.operation.name": "chat", "gen_ai.system": self.model.provider,
                 "gen_ai.request.model": self.model.name}
        with self.tracer.span(f"llm.turn_{self.llm_turns}", "llm", input={"system": self.config.system_prompt,
                              "messages": public}, attributes=attrs) as span:
            for attempt in range(1, self.limits.max_retries + 2):
                try:
                    resp = _with_timeout(lambda: self.model.generate(self.config.system_prompt, messages, TOOL_SCHEMAS),
                                         self.limits.model_timeout_s, ModelError, "model call")
                    break
                except ModelError as exc:
                    span.set_attributes({"agenteval.model.retries": attempt - 1})
                    if not exc.retryable or attempt > self.limits.max_retries:
                        span.set_error(exc)
                        raise
                    time.sleep(self.limits.backoff_s * 2 ** (attempt - 1))
            span.set_attributes({"gen_ai.response.model": resp.model, "agenteval.model.retries": attempt - 1})
            span.set_usage(resp.usage)
            span.set_output({"text": resp.text, "tool_calls": [asdict(c) for c in resp.tool_calls]})
            for k in self.usage:
                self.usage[k] += int(resp.usage.get(k) or 0)
            if resp.usage.get("estimated"):
                self.usage["estimated"] = True
            return resp

    def _call_tool(self, name: str, args: dict, call_id: str) -> dict:
        attrs = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name, "gen_ai.tool.call.id": call_id}
        entry = {"name": name, "args": args, "status": "ok", "attempts": 0, "error": None}
        self.tool_log.append(entry)
        with self.tracer.span(f"tool.{name}", "tool", input=args, attributes=attrs) as span:
            while True:
                if self.tool_attempts >= self.limits.max_tool_calls:
                    self.limit_hit = "max_tool_calls"
                    err = BudgetExceeded(f"tool-call budget of {self.limits.max_tool_calls} exhausted")
                    entry.update(status="error", error=str(err))
                    span.set_error(err)
                    return {"error": str(err)}
                self.tool_attempts += 1
                entry["attempts"] += 1
                n = entry["attempts"]
                with self.tracer.span(f"{name}.attempt_{n}", "tool_attempt", input=args,
                                      attributes={"agenteval.attempt": n}) as att:
                    try:
                        result = _with_timeout(lambda: call_tool(self.ctx, name, args), self.limits.tool_timeout_s,
                                               ToolTimeoutError, name)
                        att.set_output(result)
                    except ToolError as exc:
                        att.set_error(exc)
                        last = exc
                    else:
                        span.set_attributes({"agenteval.retries": n - 1})
                        span.set_output(result)
                        return result
                if not last.retryable or n > self.limits.max_retries:
                    entry.update(status="error", error=f"{type(last).__name__}: {last}")
                    span.set_attributes({"agenteval.retries": n - 1})
                    span.set_error(last)
                    return {"error": entry["error"]}
                time.sleep(self.limits.backoff_s * 2 ** (n - 1))

    def execute(self) -> tuple[str, dict | None]:
        messages = [{"role": "user", "content": user_prompt(self.case),
                     "meta": {"company": self.case.company, "authorized": self.case.authorized}}]
        while self.llm_turns < self.limits.max_llm_turns:
            self.llm_turns += 1
            try:
                resp = self._call_model(messages)
            except ModelError as exc:
                return "model_error", {"decision": "error", "reason": str(exc)}
            messages.append({"role": "assistant", "content": resp.text,
                             "tool_calls": [asdict(c) for c in resp.tool_calls], "raw": resp.raw})
            if not resp.tool_calls:
                return "completed", parse_final(resp.text)
            for tc in resp.tool_calls:
                result = self._call_tool(tc.name, tc.args, tc.id)
                messages.append({"role": "tool", "name": tc.name, "tool_call_id": tc.id, "content": result})
            if self.limit_hit:
                return "limit_exceeded", None
        self.limit_hit = "max_llm_turns"
        return "limit_exceeded", None


def run_case(case: Case, config: AgentConfig, model: Model, mode: str, limits: Limits = Limits(),
             out_dir: Path | None = None, processors=()) -> dict:
    tracer = Tracer(processors)
    run = AgentRun(case, config, model, tracer, limits)
    run_id = uuid.uuid4().hex[:12]
    root_attrs = {"agenteval.run_id": run_id, "agenteval.case_id": case.id, "agenteval.config": config.name,
                  "agenteval.mode": mode, "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "lead-research"}
    with tracer.span("agent.lead_research", "agent", input={"case_id": case.id, "company": case.company,
                     "authorized": case.authorized, "prompt": user_prompt(case)}, attributes=root_attrs) as root:
        status, final = run.execute()
        outcome = {"status": status, "final": final, "limit_hit": run.limit_hit}
        root.set_output(outcome)
        root.set_usage(run.usage)
        root.set_attributes({"agenteval.status": status, "agenteval.decision": (final or {}).get("decision")})
        if status != "completed":
            root.set_error(RuntimeError(f"run ended with status {status}"))
    record = {
        "run_id": run_id, "case_id": case.id, "config": config.name, "mode": mode, "model": model.name,
        "status": status, "final": final, "limit_hit": run.limit_hit, "llm_turns": run.llm_turns,
        "tool_attempts": run.tool_attempts, "tool_calls": run.tool_log, "usage": run.usage,
        "authorized": case.authorized, "writes": run.ctx.writes, "denied_writes": run.ctx.denied_writes,
        "limits": asdict(limits),
    }
    trace = tracer.canonical({"run_id": run_id, "case_id": case.id, "config": config.name, "mode": mode,
                              "model": model.name, "outcome": outcome})
    tracer.shutdown()
    record["latency_ms"] = trace["latency_ms"]
    out_dir = out_dir or ARTIFACTS / "traces"
    record["trace_path"] = str(write_trace(trace, out_dir / f"{config.name}__{case.id}__{mode}.json"))
    return record
