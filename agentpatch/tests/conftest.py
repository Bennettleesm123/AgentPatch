import socket
from pathlib import Path

import pytest

from agentpatch.integrations import langgraph_available

FIXTURES = Path(__file__).parent / "fixtures"
_real_connect = socket.socket.connect


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if host not in ("127.0.0.1", "localhost", "::1") and not str(host).startswith("/"):
        raise RuntimeError(f"tests must not use the network (tried {address!r})")
    return _real_connect(self, address)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTPATCH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NO_COLOR", "1")


needs_langgraph = pytest.mark.skipif(not langgraph_available(), reason="LangGraph not installed")


@pytest.fixture(scope="session")
def demo_home(tmp_path_factory):
    """The demo's baseline and candidate runs (3 trials x 5 cases each), shared by the slower tests."""
    if not langgraph_available():
        pytest.skip("LangGraph not installed")
    from agentpatch.demo import SUITE_PATH
    from agentpatch.runner import run_suite
    from agentpatch.suite import load_suite

    home = tmp_path_factory.mktemp("demo_home")
    suite = load_suite(SUITE_PATH)
    run_suite(suite, "baseline", target="agentpatch.demo.lead_agent:baseline", root=home)
    run_suite(suite, "candidate", target="agentpatch.demo.lead_agent:candidate", root=home)
    return home


@pytest.fixture(scope="session")
def demo_cmp(demo_home):
    from agentpatch import store
    from agentpatch.compare import compare

    return compare(store.resolve("baseline", demo_home), store.resolve("candidate", demo_home))
