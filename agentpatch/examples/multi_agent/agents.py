"""A two-agent LangGraph system (researcher -> writer), offline, for trying AgentPatch on subgraphs.

Each agent is its own compiled subgraph with a model node and a tool node. The "model" is a tiny
deterministic stand-in so this runs without API keys. Set WRITER_SKIPS_FACTS=1 to simulate a
regression where the writer stops using the researcher's facts tool.

    agentpatch run --suite examples/multi_agent/agentpatch.toml --label baseline
    WRITER_SKIPS_FACTS=1 agentpatch run --suite examples/multi_agent/agentpatch.toml --label candidate
    agentpatch compare && agentpatch ui
"""

from __future__ import annotations

import os
from typing import Annotated, Any, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition


class State(TypedDict):
    messages: Annotated[list, add_messages]


@tool
def lookup(company: str) -> str:
    """Look up public facts about a company."""
    return f"{company}: 420 employees, Series B, robotics"


@tool
def fact_check(claim: str) -> str:
    """Check a claim against the research notes."""
    return "supported" if "420" in claim else "unsupported"


class RuleModel(BaseChatModel):
    """Calls `tool_name` once with an argument built from the conversation, then answers."""

    tool_name: str | None = None
    answer: str = "done"

    @property
    def _llm_type(self) -> str:
        return "rule-model"

    def _generate(self, messages, stop=None, run_manager=None, **kw: Any) -> ChatResult:
        used_tool = any(isinstance(m, ToolMessage) and m.name == self.tool_name for m in messages)
        topic = next((m.content for m in messages if isinstance(m, HumanMessage)), "")
        if self.tool_name and not used_tool:
            arg = {"company": topic} if self.tool_name == "lookup" else {"claim": messages[-1].content}
            msg = AIMessage("", tool_calls=[{"name": self.tool_name, "args": arg, "id": f"call_{len(messages)}"}])
        else:
            last_tool = next((m.content for m in reversed(messages) if isinstance(m, ToolMessage)), "")
            msg = AIMessage(f"{self.answer}: {last_tool or topic}")
        msg.usage_metadata = {"input_tokens": 20 * len(messages), "output_tokens": 12, "total_tokens": 20 * len(messages) + 12}
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _agent(name: str, model: RuleModel, tools: list):
    def think(state: State):
        return {"messages": [model.invoke(state["messages"])]}

    g = StateGraph(State)
    g.add_node("think", think)
    g.add_node("tools", ToolNode(tools))
    g.add_edge(START, "think")
    g.add_conditional_edges("think", tools_condition)
    g.add_edge("tools", "think")
    return g.compile(name=name)


def build():
    skip = os.environ.get("WRITER_SKIPS_FACTS") == "1"
    researcher = _agent("researcher", RuleModel(tool_name="lookup", answer="notes"), [lookup])
    writer = _agent("writer", RuleModel(tool_name=None if skip else "fact_check", answer="brief"), [fact_check])
    g = StateGraph(State)
    g.add_node("researcher", researcher)
    g.add_node("writer", writer)
    g.add_edge(START, "researcher")
    g.add_edge("researcher", "writer")
    g.add_edge("writer", END)
    return g.compile()
