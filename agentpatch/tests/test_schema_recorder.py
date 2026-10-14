import pytest

import agentpatch
from agentpatch.recorder import Recorder
from agentpatch.schema import Trace, jsonable, read_json, write_json
from helpers import S, make_trace


def test_step_ids_are_readable_and_numbered_per_kind_and_name():
    t = make_trace(S("a", "node", "plan", start=1, end=2), S("b", "tool", "search", "a", 1.1, 1.5),
                   S("c", "node", "plan", start=3, end=4), S("d", "tool", "search", "c", 3.1, 3.2))
    assert [s.id for s in t.steps] == ["agent:agent#1", "node:plan#1", "tool:search#1", "node:plan#2", "tool:search#2"]
    assert t.by_id["tool:search#2"].parent_id == "node:plan#2"


def test_unknown_parent_becomes_missing_marker():
    t = make_trace(S("x", "tool", "search", parent="ghost"))
    assert t.by_id["tool:search#1"].parent_id == "missing:ghost"


def test_error_origins_are_the_innermost_errors():
    t = make_trace(S("n", "node", "tools", status="error", error="boom"),
                   S("t", "tool", "save", "n", status="error", error="boom"), status="error", error="boom")
    assert [s.id for s in t.error_origins()] == ["tool:save#1"]


def test_round_trip_and_schema_guard(tmp_path):
    t = make_trace(S("a", "node", "plan"), output={"x": 1})
    path = write_json(tmp_path / "t.json", t.to_dict())
    back = Trace.from_dict(read_json(path))
    assert back.to_dict() == t.to_dict()
    with pytest.raises(ValueError):
        Trace.from_dict({"schema": "something.else"})


def test_jsonable_redacts_secrets_and_bounds_size(monkeypatch):
    monkeypatch.setenv("MY_SERVICE_API_KEY", "sk-very-secret-value")
    out = jsonable({"api_key": "abc", "Authorization": "Bearer x", "note": "token sk-very-secret-value here",
                    "long": "x" * 5000, "many": list(range(200)), "blob": b"\x00" * 10})
    assert out["api_key"] == "[REDACTED]" and out["Authorization"] == "[REDACTED]"
    assert "sk-very-secret-value" not in out["note"] and "[REDACTED]" in out["note"]
    assert len(out["long"]) < 2100 and "more chars" in out["long"]
    assert len(out["many"]) == 51 and "more items" in out["many"][-1]
    assert out["blob"] == "<10 bytes>"


def test_sdk_records_steps_tools_and_llm_calls():
    @agentpatch.tool
    def lookup(q):
        return {"hit": q}

    @agentpatch.tool(name="explode")
    def bad():
        raise RuntimeError("nope")

    with agentpatch.trace("case1", input={"q": "hi"}) as rec:
        with agentpatch.step("plan") as st:
            agentpatch.log_llm("gpt-x", input="hi", tool_calls=[{"name": "lookup", "args": {}}],
                               usage={"total_tokens": 7})
            st.set_output("planned")
        lookup("hi")
        with pytest.raises(RuntimeError):
            bad()
        rec.set_output({"answer": 42})
    t = rec.trace
    ids = [s.id for s in t.steps]
    assert ids == ["agent:agent#1", "node:plan#1", "llm:gpt-x#1", "tool:lookup#1", "tool:explode#1"]
    assert t.by_id["llm:gpt-x#1"].parent_id == "node:plan#1"
    assert t.by_id["llm:gpt-x#1"].attributes["requested_tools"] == ["lookup"]
    assert t.by_id["tool:lookup#1"].input == {"q": "hi"} and t.by_id["tool:lookup#1"].output == {"hit": "hi"}
    assert t.by_id["tool:explode#1"].status == "error" and "RuntimeError: nope" in t.by_id["tool:explode#1"].error
    assert t.status == "ok" and t.output == {"answer": 42}


def test_trace_records_and_reraises_agent_errors():
    with pytest.raises(ValueError):
        with agentpatch.trace("boom") as rec:
            raise ValueError("bad input")
    assert rec.trace.status == "error" and rec.trace.error == "ValueError: bad input"


def test_tool_decorator_is_a_noop_outside_a_trace():
    @agentpatch.tool
    def f(x):
        return x + 1

    assert f(1) == 2


def test_callback_handler_requires_a_recorder():
    with pytest.raises(RuntimeError):
        agentpatch.callback_handler()


def test_recorder_end_is_idempotent():
    rec = Recorder()
    rec.start("k", "x", "node")
    rec.end("k", output=1)
    rec.end("k", output=2, error="late")
    t = rec.finish()
    assert t.by_id["node:x#1"].output == 1 and t.by_id["node:x#1"].status == "ok"
