"""AgentPatch: know what broke, why it broke, and whether your agent is still observable."""

from .recorder import log_llm, step, tool, trace

__version__ = "0.1.0"
__all__ = ["trace", "step", "tool", "log_llm", "callback_handler"]


def callback_handler(recorder=None):
    """LangChain/LangGraph callback handler recording into `recorder` (or the active `agentpatch.trace`)."""
    from .integrations.langgraph import AgentPatchCallbackHandler
    from .recorder import current

    rec = recorder or current()
    if rec is None:
        raise RuntimeError("no active recorder: use `with agentpatch.trace(...) as rec:` or pass a Recorder")
    return AgentPatchCallbackHandler(rec)
