"""``McpFailureCache`` remembers why a connect failed; ``status`` says who is connected.

The coordinator's disconnect/connect ordering is pinned in
``test_session_mcp_ops.py``. Here: the failure cache's rules and the
status rows the ``/mcp`` command and the panel render.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from igni_core.core.mcp.schemas import MCPConnectResult
from igni_core.core.session.mcp_ops import (
    McpFailureCache,
    McpLifecycleCoordinator,
    McpLifecycleDeps,
)


def test_a_failed_connect_is_remembered_with_its_reason():
    cache = McpFailureCache()
    cache.record("srv", MCPConnectResult.failure("denied"))
    assert cache.reason("srv") == "denied"
    assert dict(cache.items()) == {"srv": "denied"}


def test_a_failure_without_a_reason_is_remembered_as_an_empty_one():
    cache = McpFailureCache()
    cache.record("srv", MCPConnectResult(ok=False, client=None, reason=""))
    assert dict(cache.items()) == {"srv": ""}


def test_a_success_clears_the_failure():
    cache = McpFailureCache()
    cache.record("srv", MCPConnectResult.failure("denied"))
    cache.record("srv", MCPConnectResult.success(MagicMock()))
    assert cache.reason("srv") == ""
    assert dict(cache.items()) == {}


def test_a_disconnect_clears_the_failure():
    cache = McpFailureCache()
    cache.record("srv", MCPConnectResult.failure("denied"))
    cache.record("srv", None)
    assert dict(cache.items()) == {}


def test_clearing_an_unknown_server_is_harmless():
    cache = McpFailureCache()
    cache.record("never-seen", None)
    assert cache.reason("never-seen") == ""


def test_one_servers_failure_does_not_touch_anothers():
    cache = McpFailureCache()
    cache.record("a", MCPConnectResult.failure("x"))
    cache.record("b", MCPConnectResult.failure("y"))
    cache.record("a", None)
    assert dict(cache.items()) == {"b": "y"}


def _coordinator(servers: list[str], connected: list[str]) -> McpLifecycleCoordinator:
    manager = SimpleNamespace(list_servers=lambda: servers, list_connected=lambda: connected)
    return McpLifecycleCoordinator(McpLifecycleDeps(mcp_manager=manager, rebuild=MagicMock()))


def test_status_has_one_row_per_configured_server_marked_connected_only_if_it_is():
    rows = _coordinator(["a", "b", "c"], ["b"]).status()
    assert {r.name: r.connected for r in rows} == {"a": False, "b": True, "c": False}
    assert len(rows) == 3


def test_status_with_no_servers_is_empty():
    assert _coordinator([], []).status() == []


def test_a_connected_server_that_is_not_configured_gets_no_row():
    rows = _coordinator(["a"], ["a", "ghost"]).status()
    assert [r.name for r in rows] == ["a"]
