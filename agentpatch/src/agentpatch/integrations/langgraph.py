"""LangGraph / LangChain integration via the standard callback system.

    from agentpatch.integrations.langgraph import AgentPatchCallbackHandler
    graph.invoke(state, config={"callbacks": [AgentPatchCallbackHandler(recorder)]})

Graph nodes become `node` steps, chat-model calls `llm` steps (with model name, token usage and the
tool calls the model requested) and tool runs `tool` steps. Conditional-edge functions become
`router` steps. LangChain internals that aren't nodes (prompt templates, parsers, sequences) are
folded into their parent so the trace stays small.
"""

from __future__ import annotations

from typing import Any

try:
    from langchain_core.callbacks import BaseCallbackHandler
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError("LangGraph support needs: pip install 'agentpatch[langgraph]'") from exc

from ..recorder import Recorder

_INTERNAL = {"LangGraph", "RunnableSequence", "RunnableLambda", "RunnableParallel", "ChannelWrite", "ChannelRead",
             "RunnableCallable", "RunnableAssign", "RunnablePassthrough"}


class AgentPatchCallbackHandler(BaseCallbackHandler):
    raise_error = False  # never break the agent because of instrumentation

    def __init__(self, recorder: Recorder):
        self.rec = recorder
        self._alias: dict[Any, Any] = {}  # folded run ids -> the step they were folded into
        self._root_run: Any = None

    def _parent(self, parent_run_id):
        if parent_run_id is None:
            return self.rec.root_key
        parent_run_id = self._alias.get(parent_run_id, parent_run_id)
        if parent_run_id == self._root_run:
            return self.rec.root_key
        return parent_run_id if self.rec.has(parent_run_id) else f"untracked:{parent_run_id}"

    # chains: graph, nodes, routers
    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, metadata=None, name=None, **kw):
        metadata = metadata or {}
        name = name or (serialized or {}).get("name") or ((serialized or {}).get("id") or ["chain"])[-1]
        if parent_run_id is None and self._root_run is None:
            self._root_run = run_id
            self.rec.update(self.rec.root_key, {"graph": name})
            return
        node = metadata.get("langgraph_node")
        parent = self._parent(parent_run_id)
        if node and name == node:
            p = self.rec.info(parent)
            if p and p["kind"] == "node" and p["attributes"].get("langgraph_node") == name:
                # A compiled subgraph named like the node that runs it: its root chain is the same step.
                self._alias[run_id] = parent
                self.rec.update(parent, {"subgraph": True})
                return
            agent = self._enclosing_node(parent)
            self.rec.start(run_id, f"{agent}/{name}" if agent else name, "node", parent_key=parent, input=inputs,
                           attributes={"langgraph_node": name, "langgraph_step": metadata.get("langgraph_step"),
                                       "triggers": list(metadata.get("langgraph_triggers") or [])})
        elif node and name not in _INTERNAL and not name.startswith(("__", "Runnable")) \
                and not name.endswith(("Template", "Parser", "Prompt")):
            self.rec.start(run_id, name, "other", parent_key=parent, input=None)
        else:
            self._alias[run_id] = parent if parent != self.rec.root_key else self._root_run

    def _enclosing_node(self, key) -> str | None:
        """Name of the nearest node step above `key`: nodes inside a subgraph are named `<agent>/<node>`."""
        for _ in range(50):
            p = self.rec.info(key)
            if p is None or p["kind"] == "agent":
                return None
            if p["kind"] == "node":
                return p["name"]
            key = p["parent_key"]
        return None

    def on_chain_end(self, outputs, *, run_id, **kw):
        if self.rec.has(run_id):
            if _looks_like_route(outputs):
                self.rec.set_kind(run_id, "router")
            self.rec.end(run_id, output=outputs)

    def on_chain_error(self, error, *, run_id, **kw):
        if self.rec.has(run_id):
            self.rec.end(run_id, error=error)

    # models
    def _llm_start(self, serialized, inputs, run_id, parent_run_id, metadata, kw):
        metadata = metadata or {}
        params = kw.get("invocation_params") or {}
        model = (metadata.get("ls_model_name") or params.get("model") or params.get("model_name")
                 or ((serialized or {}).get("id") or ["llm"])[-1])
        self.rec.start(run_id, str(model), "llm", parent_key=self._parent(parent_run_id), input=inputs,
                       attributes={"model": str(model), "provider": metadata.get("ls_provider")})

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, metadata=None, **kw):
        flat = [m for batch in messages for m in batch] if messages and isinstance(messages[0], list) else messages
        self._llm_start(serialized, {"messages": flat}, run_id, parent_run_id, metadata, kw)

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, metadata=None, **kw):
        self._llm_start(serialized, {"prompts": prompts}, run_id, parent_run_id, metadata, kw)

    def on_llm_end(self, response, *, run_id, **kw):
        attrs: dict = {}
        output: Any = None
        try:
            gen = response.generations[0][0]
            msg = getattr(gen, "message", None)
            output = msg if msg is not None else getattr(gen, "text", None)
            calls = getattr(msg, "tool_calls", None) or []
            if calls:
                attrs["requested_tools"] = [c.get("name") for c in calls]
            usage = getattr(msg, "usage_metadata", None)
            if usage:
                attrs["usage"] = {k: usage.get(k) for k in ("input_tokens", "output_tokens", "total_tokens")}
            model = (response.llm_output or {}).get("model_name") if response.llm_output else None
            if model:
                attrs["model"] = model
            if not usage and response.llm_output and response.llm_output.get("token_usage"):
                tu = response.llm_output["token_usage"]
                attrs["usage"] = {"input_tokens": tu.get("prompt_tokens"), "output_tokens": tu.get("completion_tokens"),
                                  "total_tokens": tu.get("total_tokens")}
        except (AttributeError, IndexError, TypeError):
            pass
        self.rec.end(run_id, output=output, attributes=attrs)

    def on_llm_error(self, error, *, run_id, **kw):
        self.rec.end(run_id, error=error)

    # tools
    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, inputs=None, name=None, **kw):
        name = name or (serialized or {}).get("name") or "tool"
        self.rec.start(run_id, name, "tool", parent_key=self._parent(parent_run_id),
                       input=inputs if inputs is not None else input_str)

    def on_tool_end(self, output, *, run_id, **kw):
        if getattr(output, "status", None) == "error":
            self.rec.end(run_id, output=output, error=str(getattr(output, "content", output)))
        else:
            self.rec.end(run_id, output=getattr(output, "content", output))

    def on_tool_error(self, error, *, run_id, **kw):
        self.rec.end(run_id, error=error)

    def on_retriever_start(self, serialized, query, *, run_id, parent_run_id=None, name=None, **kw):
        self.rec.start(run_id, name or "retriever", "retriever", parent_key=self._parent(parent_run_id),
                       input={"query": query})

    def on_retriever_end(self, documents, *, run_id, **kw):
        self.rec.end(run_id, output={"documents": len(documents or [])})

    def on_retriever_error(self, error, *, run_id, **kw):
        self.rec.end(run_id, error=error)


def _looks_like_route(value) -> bool:
    """Conditional-edge functions return node names (or Send objects); other chains return state."""
    if isinstance(value, str):
        return True
    if isinstance(value, (list, tuple)) and value:
        return all(isinstance(v, str) or type(v).__name__ == "Send" for v in value)
    return False


def is_langgraph(obj: Any) -> bool:
    return hasattr(obj, "invoke") and hasattr(obj, "get_graph")


def graph_structure(graph: Any) -> dict | None:
    """Declared nodes and edges of a compiled graph (for drift and coverage checks)."""
    try:
        g = graph.get_graph()
    except Exception:
        return None
    nodes = {n for n in g.nodes if not n.startswith("__")}
    edges = sorted({(e.source, e.target, bool(e.conditional)) for e in g.edges})
    try:  # nodes inside compiled subgraphs, named like the trace does: `<agent>/<node>`
        inner = {n.replace(":", "/") for n in graph.get_graph(xray=True).nodes
                 if ":" in n and not n.rsplit(":", 1)[-1].startswith("__")}
    except Exception:
        inner = set()
    out = {"nodes": sorted(nodes | inner), "edges": [{"source": s, "target": t, "conditional": c} for s, t, c in edges]}
    if inner:
        out["subgraphs"] = sorted({n.split("/")[0] for n in inner})
    return out


def run_graph(graph: Any, input: Any, recorder: Recorder, config: dict | None = None) -> Any:
    from .. import recorder as rec_mod

    cfg = dict(config or {})
    cfg["callbacks"] = list(cfg.get("callbacks") or []) + [AgentPatchCallbackHandler(recorder)]
    token = rec_mod._current.set(recorder)
    ptoken = rec_mod._parent.set(None)
    try:
        return graph.invoke(input, config=cfg)
    finally:
        rec_mod._parent.reset(ptoken)
        rec_mod._current.reset(token)
