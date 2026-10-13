"""Framework integrations. Each one is optional and imported lazily."""

import importlib.util


def langgraph_available() -> bool:
    return importlib.util.find_spec("langchain_core") is not None
