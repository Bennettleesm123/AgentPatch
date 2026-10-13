"""Test suite configuration (`agentpatch.toml`).

    [agent]
    target = "my_agent:graph"        # compiled LangGraph, a zero-arg factory returning one, or fn(input)
    repeats = 3                      # runs per case; success rates need more than one

    [defaults]                       # checks applied to every case
    checks = [{ type = "completes" }, { type = "max_latency_ms", value = 5000 }]

    [[cases]]
    id = "refund_request"
    input = { messages = [["user", "I want a refund"]] }
    checks = [{ type = "tool_called", tool = "lookup_order" },
              { type = "output", path = "result.decision", equals = "refund" }]

    [thresholds]                     # when a change is worth flagging
    latency_pct = 25
    latency_min_ms = 50

    [coverage]                       # optional: what you expect to see in every trace
    expected_tools = ["lookup_order"]
    expected_nodes = ["agent", "tools"]
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .checks import CHECK_TYPES, check_id, validate_check

DEFAULT_THRESHOLDS = {"latency_pct": 25.0, "latency_min_ms": 50.0, "tool_calls_pct": 50.0, "tokens_pct": 25.0,
                      "untraced_min_ms": 100.0, "untraced_ratio": 0.5}


class SuiteError(ValueError):
    pass


@dataclass
class Case:
    id: str
    input: Any
    checks: list[dict]
    description: str = ""
    has_input: bool = True


@dataclass
class Suite:
    name: str
    path: Path | None
    target: str | None
    repeats: int
    cases: list[Case]
    thresholds: dict = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    coverage: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)

    @property
    def base_dir(self) -> Path:
        return self.path.parent if self.path else Path.cwd()

    def case(self, case_id: str) -> Case | None:
        return next((c for c in self.cases if c.id == case_id), None)

    def referenced_tools(self) -> set[str]:
        out = set()
        for c in self.cases:
            for ch in c.checks:
                if ch.get("tool"):
                    out.add(ch["tool"])
                out.update(ch.get("sequence") or [])
        return out


def load_suite(path: str | Path) -> Suite:
    path = Path(path)
    if not path.is_file():
        raise SuiteError(f"suite file not found: {path} (create one with `agentpatch init`)")
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise SuiteError(f"{path}: invalid TOML: {exc}") from None
    return parse_suite(data, path)


def parse_suite(data: dict, path: Path | None = None) -> Suite:
    where = str(path or "suite")
    agent = data.get("agent") or {}
    unknown = set(data) - {"name", "agent", "defaults", "cases", "thresholds", "coverage"}
    if unknown:
        raise SuiteError(f"{where}: unknown top-level section(s) {sorted(unknown)}")
    repeats = agent.get("repeats", 1)
    if not isinstance(repeats, int) or not 1 <= repeats <= 50:
        raise SuiteError(f"{where}: agent.repeats must be an integer from 1 to 50")
    defaults = (data.get("defaults") or {}).get("checks") or []
    cases_raw = data.get("cases") or []
    if not cases_raw:
        raise SuiteError(f"{where}: no [[cases]] defined")
    cases, seen = [], set()
    for i, c in enumerate(cases_raw):
        cid = c.get("id")
        if not cid or not isinstance(cid, str):
            raise SuiteError(f"{where}: case #{i + 1} needs a string `id`")
        if cid in seen:
            raise SuiteError(f"{where}: duplicate case id {cid!r}")
        seen.add(cid)
        checks = []
        for ch in list(defaults) + list(c.get("checks") or []):
            try:
                checks.append(validate_check(ch))
            except ValueError as exc:
                raise SuiteError(f"{where}: case {cid!r}: {exc}") from None
        ids = [check_id(ch) for ch in checks]
        dupes = {x for x in ids if ids.count(x) > 1}
        if dupes:
            raise SuiteError(f"{where}: case {cid!r} has duplicate checks {sorted(dupes)}; give them distinct `id`s")
        cases.append(Case(id=cid, input=c.get("input"), checks=checks, description=c.get("description", ""),
                          has_input="input" in c))
    thresholds = dict(DEFAULT_THRESHOLDS)
    for k, v in (data.get("thresholds") or {}).items():
        if k not in DEFAULT_THRESHOLDS or not isinstance(v, (int, float)):
            raise SuiteError(f"{where}: unknown or non-numeric threshold {k!r}; known: {sorted(DEFAULT_THRESHOLDS)}")
        thresholds[k] = float(v)
    coverage = data.get("coverage") or {}
    bad = set(coverage) - {"expected_tools", "expected_nodes"}
    if bad:
        raise SuiteError(f"{where}: unknown coverage keys {sorted(bad)}")
    return Suite(name=data.get("name") or (path.parent.name if path else "suite"), path=path,
                 target=agent.get("target"), repeats=repeats, cases=cases, thresholds=thresholds,
                 coverage=coverage, config=data)


__all__ = ["Suite", "Case", "SuiteError", "load_suite", "parse_suite", "CHECK_TYPES"]
