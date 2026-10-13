"""Observability coverage: is the trace capturing what the agent actually did?

Two confidence levels, kept separate on purpose:
- "verified": provable from the trace itself (the model asked for a tool and no tool step exists;
  a step's parent is missing; a step never ended; an error has no message).
- "possible": a signal, not proof (a node spent most of its time outside any recorded child step;
  a declared graph node never ran). These may be legitimate (plain computation, a branch the
  suite never exercises), so they're phrased as questions.
Plus "info" items about partial capture (no token usage, no tool input/output).

Anything that never touches the instrumentation at all (a background thread, a process the agent
shells out to) leaves no trace, and nothing here can detect it.
"""

from __future__ import annotations

from collections import Counter

from .schema import Step, Trace

LEVELS = ("verified", "possible", "info")


def _gap(code, level, trace: Trace, step: Step | None, message: str) -> dict:
    return {"code": code, "level": level, "case_id": trace.case_id, "trial": trace.trial,
            "step": step.id if step else None, "message": message}


def unrecorded_tool_calls(trace: Trace) -> list[tuple[Step, str]]:
    """(llm step, tool name) for each requested tool call with no tool step before the next model call."""
    llms = trace.of_kind("llm")
    crash_points = [trace.order[s.id] for s in trace.error_origins()] if trace.status == "error" else []
    out = []
    for i, llm in enumerate(llms):
        requested = Counter(llm.attributes.get("requested_tools") or [])
        if not requested:
            continue
        lo = trace.order[llm.id]
        if i + 1 == len(llms) and any(p > lo for p in crash_points):
            continue  # the run crashed before the requested calls could run
        hi = trace.order[llms[i + 1].id] if i + 1 < len(llms) else len(trace.steps)
        recorded = Counter(s.name for s in trace.steps[lo + 1:hi] if s.kind == "tool")
        for tool, n in requested.items():
            out += [(llm, tool)] * max(0, n - recorded.get(tool, 0))
    return out


def unrecorded_counts(trace: Trace) -> dict[str, int]:
    return dict(Counter(tool for _, tool in unrecorded_tool_calls(trace)))


def trace_gaps(trace: Trace, thresholds: dict | None = None) -> list[dict]:
    th = {"untraced_min_ms": 100.0, "untraced_ratio": 0.5, **(thresholds or {})}
    gaps = []
    for llm, tool in unrecorded_tool_calls(trace):
        gaps.append(_gap("unrecorded_tool_call", "verified", trace, llm,
                         f"{llm.id} requested tool `{tool}`, but no `{tool}` tool step was recorded before the next "
                         "model call. Either the call never ran, or it ran outside instrumentation (e.g. called "
                         "directly instead of through a LangChain tool)."))
    ids = set(trace.by_id)
    for s in trace.steps:
        if s.parent_id is not None and s.parent_id not in ids:
            gaps.append(_gap("orphan_step", "verified", trace, s,
                             f"{s.id}'s parent step was never recorded, so the call tree is incomplete here."))
        if s.end_ms is None:
            gaps.append(_gap("unclosed_step", "verified", trace, s,
                             f"{s.id} started but never finished; the trace was cut off or the step leaked."))
        if s.status == "error" and not (s.error or "").strip():
            gaps.append(_gap("error_without_message", "verified", trace, s,
                             f"{s.id} is marked as an error but no error message was captured."))
    if len([s for s in trace.steps if s.kind != "agent"]) == 0:
        gaps.append(_gap("empty_trace", "verified", trace, trace.root,
                         "Only the run itself was recorded: no nodes, model calls or tools. The agent may not be "
                         "using the instrumented path (check the callbacks are passed to invoke)."))
    for s in trace.steps:
        if s.kind != "node" or s.duration_ms is None or s.duration_ms < th["untraced_min_ms"]:
            continue
        covered = _covered_ms(trace, s)
        gap = s.duration_ms - covered
        if gap >= th["untraced_min_ms"] and gap / s.duration_ms >= th["untraced_ratio"]:
            gaps.append(_gap("untraced_time", "possible", trace, s,
                             f"{s.id} took {s.duration_ms:.0f} ms but its recorded child steps account for only "
                             f"{covered:.0f} ms. If it calls an API, database or model directly, that work isn't "
                             "instrumented; if it's plain computation, this is fine."))
    llms = trace.of_kind("llm")
    no_usage = [s for s in llms if not (s.attributes.get("usage") or {}).get("total_tokens")]
    if no_usage:
        gaps.append(_gap("llm_usage_missing", "info", trace, no_usage[0],
                         f"{len(no_usage)} of {len(llms)} model calls have no token usage recorded."))
    no_io = [s for s in trace.tool_steps() if s.status == "ok" and (s.input is None or s.output is None)]
    if no_io:
        gaps.append(_gap("tool_io_missing", "info", trace, no_io[0],
                         f"{len(no_io)} tool call(s) are missing their input or output."))
    return gaps


def _covered_ms(trace: Trace, step: Step) -> float:
    spans = sorted((c.start_ms, c.end_ms if c.end_ms is not None else c.start_ms) for c in trace.children(step))
    total, cur_s, cur_e = 0.0, None, None
    for a, b in spans:
        if cur_e is None or a > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = a, b
        else:
            cur_e = max(cur_e, b)
    if cur_e is not None:
        total += cur_e - cur_s
    return total


def structure(traces: list[Trace], graph: dict | None = None) -> dict:
    """What a set of traces shows about the agent's shape."""
    nodes, tools, models, transitions = Counter(), Counter(), Counter(), Counter()
    llm_total = llm_usage = tool_total = tool_io = 0
    for t in traces:
        seq = [s.name for s in t.steps if s.kind == "node"]
        nodes.update(set(seq))
        transitions.update(set(zip(seq, seq[1:])))
        tools.update({s.name for s in t.tool_steps()})
        for s in t.of_kind("llm"):
            llm_total += 1
            llm_usage += bool((s.attributes.get("usage") or {}).get("total_tokens"))
            models[s.attributes.get("model") or s.name] += 1
        for s in t.tool_steps():
            tool_total += 1
            tool_io += s.input is not None and (s.output is not None or s.status == "error")
    requested = Counter(tool for t in traces for _, tool in unrecorded_tool_calls(t))
    return {
        "traces": len(traces),
        "nodes": sorted(nodes), "tools": sorted(tools), "models": sorted(models),
        "unrecorded_tools": sorted(requested),
        "transitions": sorted(f"{a} → {b}" for a, b in transitions),
        "declared_nodes": (graph or {}).get("nodes"),
        "capture": {"llm_usage": _rate(llm_usage, llm_total), "tool_io": _rate(tool_io, tool_total),
                    "llm_calls": llm_total, "tool_calls": tool_total},
    }


def _rate(n, d):
    return None if not d else round(n / d, 3)


def run_coverage(traces: list[Trace], graph: dict | None, suite_tools: set[str] | None = None,
                 expected: dict | None = None, thresholds: dict | None = None) -> dict:
    gaps = [g for t in traces for g in trace_gaps(t, thresholds)]
    st = structure(traces, graph)
    run_level = []
    if graph and graph.get("nodes"):
        for n in sorted(set(graph["nodes"]) - set(st["nodes"])):
            run_level.append({"code": "declared_node_not_observed", "level": "possible", "case_id": None,
                              "trial": None, "step": None,
                              "message": f"Graph node `{n}` is declared but never appeared in any trace. The suite may "
                                         "not exercise it, or its execution isn't being captured."})
    for tool in sorted((suite_tools or set()) - set(st["tools"]) - set(st["unrecorded_tools"])):
        run_level.append({"code": "check_references_unseen_tool", "level": "possible", "case_id": None, "trial": None,
                          "step": None, "message": f"Checks reference tool `{tool}`, but it never appeared in any trace. "
                          "The tool may have been renamed or removed (stale check), or it isn't instrumented."})
    for tool in sorted(set((expected or {}).get("expected_tools") or []) - set(st["tools"])):
        run_level.append({"code": "expected_tool_missing", "level": "verified", "case_id": None, "trial": None,
                          "step": None, "message": f"`{tool}` is listed in [coverage] expected_tools but no trace "
                                                   "recorded a call to it."})
    for node in sorted(set((expected or {}).get("expected_nodes") or []) - set(st["nodes"])):
        run_level.append({"code": "expected_node_missing", "level": "verified", "case_id": None, "trial": None,
                          "step": None, "message": f"`{node}` is listed in [coverage] expected_nodes but no trace "
                                                   "recorded it."})
    all_gaps = run_level + gaps
    return {"structure": st, "gaps": all_gaps, "summary": summarize(all_gaps, len(traces))}


def summarize(gaps: list[dict], n_traces: int) -> dict:
    by_code: dict[str, dict] = {}
    for g in gaps:
        e = by_code.setdefault(g["code"], {"code": g["code"], "level": g["level"], "count": 0, "traces": set(),
                                           "message": g["message"], "examples": []})
        e["count"] += 1
        if g["case_id"] is not None:
            e["traces"].add((g["case_id"], g["trial"]))
        if len(e["examples"]) < 3:
            e["examples"].append({k: g[k] for k in ("case_id", "trial", "step", "message")})
    items = []
    for e in by_code.values():
        e["traces"] = len(e["traces"])
        items.append(e)
    rank = {lvl: i for i, lvl in enumerate(LEVELS)}
    items.sort(key=lambda e: (rank[e["level"]], -e["count"]))
    counts = Counter(g["level"] for g in gaps)
    return {"traces": n_traces, "verified": counts["verified"], "possible": counts["possible"], "info": counts["info"],
            "items": items}


def drift(base: dict, cand: dict, suite_tools: set[str] | None = None) -> list[dict]:
    """Structural changes between two runs' coverage, as findings."""
    out = []
    b, c = base["structure"], cand["structure"]

    def add(code, severity, message, **extra):
        out.append({"code": code, "severity": severity, "message": message, **extra})

    bd, cd = set(b.get("declared_nodes") or []), set(c.get("declared_nodes") or [])
    if b.get("declared_nodes") is not None and c.get("declared_nodes") is not None:
        for n in sorted(cd - bd):
            add("node_added", "info", f"Graph node `{n}` was added.", node=n)
        for n in sorted(bd - cd):
            add("node_removed", "info", f"Graph node `{n}` was removed.", node=n)
    for n in sorted(set(c["nodes"]) - set(b["nodes"]) - (cd - bd)):
        add("node_newly_observed", "info", f"Node `{n}` appears in candidate traces but not in baseline ones.", node=n)
    for n in sorted(set(b["nodes"]) - set(c["nodes"]) - (bd - cd)):
        add("node_no_longer_observed", "warning", f"Node `{n}` ran in baseline traces but never in candidate ones, "
            "although it's still declared (or the graph wasn't captured). Either the agent stopped using it or its "
            "instrumentation was lost.", node=n)
    b_all = set(b["tools"]) | set(b.get("unrecorded_tools") or [])
    c_all = set(c["tools"]) | set(c.get("unrecorded_tools") or [])
    for tname in sorted(c_all - b_all):
        unchecked = suite_tools is not None and tname not in suite_tools
        add("tool_added", "warning" if unchecked else "info",
            f"New tool `{tname}`" + (" isn't covered by any check." if unchecked else "."), tool=tname)
    for tname in sorted(b_all - c_all):
        add("tool_removed", "warning", f"Tool `{tname}` was used in baseline but never in candidate. Removed on "
            "purpose, or no longer captured?", tool=tname)
    for tname in sorted(set(c.get("unrecorded_tools") or []) - set(b.get("unrecorded_tools") or [])):
        add("tool_capture_lost", "warning", f"The model requests `{tname}` in candidate runs but its executions "
            "aren't recorded as tool steps.", tool=tname)
    for key, label in (("llm_usage", "model calls with token usage"), ("tool_io", "tool calls with input/output")):
        br, cr = b["capture"].get(key), c["capture"].get(key)
        if br is not None and cr is not None and cr < br - 0.1:
            add("capture_rate_dropped", "warning", f"Share of {label} dropped from {br:.0%} to {cr:.0%}.", metric=key)
    added_t = sorted(set(c["transitions"]) - set(b["transitions"]))
    removed_t = sorted(set(b["transitions"]) - set(c["transitions"]))
    if added_t or removed_t:
        add("flow_changed", "info", "Node-to-node flow changed: " + "; ".join(
            [f"+ {x}" for x in added_t] + [f"− {x}" for x in removed_t]), added=added_t, removed=removed_t)
    bm, cm = set(b["models"]), set(c["models"])
    if bm != cm and bm and cm:
        add("models_changed", "info", f"Models changed: {', '.join(sorted(bm))} → {', '.join(sorted(cm))}.")
    bcodes = {i["code"] for i in base["summary"]["items"] if i["level"] == "verified"}
    for item in cand["summary"]["items"]:
        if item["level"] == "verified" and item["code"] not in bcodes:
            add("new_verified_gap", "warning", f"New verified instrumentation gap in candidate: {item['code']} "
                f"({item['count']}x in {item['traces']} trace(s)).", gap=item["code"])
    return out
