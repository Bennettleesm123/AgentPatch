import pytest

import agentpatch
from conftest import needs_langgraph

pytestmark = needs_langgraph


def _graph(fail_tool=False, direct_call=False):
    from typing import Annotated, TypedDict

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.tools import tool
    from langgraph.graph import END, START, StateGraph
    from langgraph.graph.message import add_messages
    from langgraph.prebuilt import ToolNode, tools_condition

    @tool
    def weather(city: str) -> str:
        """Weather for a city."""
        if fail_tool:
            raise ValueError("weather API 503")
        return f"sunny in {city}"

    class State(TypedDict):
        messages: Annotated[list, add_messages]

    model = GenericFakeChatModel(messages=iter([
        AIMessage("", tool_calls=[{"name": "weather", "args": {"city": "Paris"}, "id": "c1"}]),
        AIMessage("It is sunny."),
    ]))

    def agent(state: State):
        return {"messages": [model.invoke(state["messages"])]}

    def run_tools_directly(state: State):
        call = state["messages"][-1].tool_calls[0]
        return {"messages": [ToolMessage("sunny (direct)", tool_call_id=call["id"], name=call["name"])]}

    g = StateGraph(State)
    g.add_node("agent", agent)
    g.add_node("tools", run_tools_directly if direct_call else ToolNode([weather], handle_tool_errors=True))
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", tools_condition)
    g.add_edge("tools", "agent")
    return g.compile()


def test_callback_handler_maps_nodes_llm_calls_tools_and_routers():
    graph = _graph()
    with agentpatch.trace("weather", input={"q": "paris"}) as rec:
        out = graph.invoke({"messages": [("user", "weather in Paris?")]},
                           config={"callbacks": [agentpatch.callback_handler()]})
        rec.set_output(out)
    t = rec.trace
    kinds = [(s.kind, s.name) for s in t.steps if s.kind in ("node", "llm", "tool")]
    assert kinds[:3] == [("node", "agent"), ("llm", kinds[1][1]), ("node", "tools")]
    assert ("tool", "weather") in kinds
    llm = t.of_kind("llm")[0]
    assert llm.attributes["requested_tools"] == ["weather"]
    assert t.by_id[t.tool_steps("weather")[0].parent_id].name == "tools"
    assert any(s.kind == "router" for s in t.steps)
    assert t.tool_steps("weather")[0].input == {"city": "Paris"}


def test_handled_tool_error_is_recorded_on_the_tool_step():
    graph = _graph(fail_tool=True)
    with agentpatch.trace("weather") as rec:
        graph.invoke({"messages": [("user", "weather?")]}, config={"callbacks": [agentpatch.callback_handler()]})
    step = rec.trace.tool_steps("weather")[0]
    assert step.status == "error" and "503" in step.error
    assert rec.trace.status == "ok"


def test_tool_executed_outside_langchain_is_a_verified_gap():
    from agentpatch.coverage import trace_gaps

    graph = _graph(direct_call=True)
    with agentpatch.trace("weather") as rec:
        graph.invoke({"messages": [("user", "weather?")]}, config={"callbacks": [agentpatch.callback_handler()]})
    gaps = [g for g in trace_gaps(rec.trace) if g["level"] == "verified"]
    assert [g["code"] for g in gaps] == ["unrecorded_tool_call"]
    assert "`weather`" in gaps[0]["message"]


def test_runner_wraps_a_compiled_graph_and_captures_structure():
    from agentpatch.runner import Agent, run_once

    agent = Agent(_graph())
    assert agent.kind == "langgraph"
    assert set(agent.structure()["nodes"]) == {"agent", "tools"}
    t = run_once(agent, {"messages": [("user", "hi")]}, "c1")
    assert t.status == "ok" and t.source == "langgraph" and t.tool_steps("weather")


def test_graph_exception_becomes_a_failed_trace():
    from langgraph.graph import END, START, StateGraph
    from typing import TypedDict

    from agentpatch.runner import Agent, run_once

    class St(TypedDict):
        x: int

    def boom(state):
        raise KeyError("missing field")

    g = StateGraph(St)
    g.add_node("boom", boom)
    g.add_edge(START, "boom")
    g.add_edge("boom", END)
    t = run_once(Agent(g.compile()), {"x": 1}, "c")
    assert t.status == "error" and "KeyError" in t.error
    assert [s.id for s in t.error_origins()] == ["node:boom#1"]


def test_demo_agent_retries_and_permission_errors_are_visible():
    from agentpatch.demo.lead_agent import baseline, candidate
    from agentpatch.runner import Agent, run_once

    t = run_once(Agent(baseline), {"company": "Globex Analytics", "authorized": True}, "transient_failure")
    assert t.status == "ok"
    details = t.tool_steps("get_company_details")
    assert [s.status for s in details][:2] == ["error", "error"] and details[-1].status == "ok"

    t = run_once(Agent(candidate), {"company": "Umbrella Labs", "authorized": False}, "unauthorized")
    save = t.tool_steps("save_assessment")
    assert save and save[0].status == "error" and "403" in save[0].error
    assert any(s.name == "enrich" for s in t.of_kind("node"))
