"""Reloading plugins with no event loop leaves no coroutine un-awaited.

The plain CLI reloads plugins with no loop running, so the background MCP
disconnect and connect cannot start. The reload says so at debug level, the
person can ``/mcp connect``, and the work that could not start is closed
rather than left to warn when it is collected.
"""

from __future__ import annotations

import inspect
import logging
import warnings

from tests.test_a_plugin_reload_syncs_the_plugins_mcp_servers import Rig


def test_with_no_event_loop_nothing_starts_and_no_coroutine_is_left_un_awaited(
    tmp_path, caplog
) -> None:
    rig = Rig(tmp_path, ["alpha"])
    rig.configs["alpha:old"] = {"command": "old"}
    caplog.set_level(logging.DEBUG, logger="igni_core.core.session.plugin_reload")

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        counts = rig.orchestrator.reload()

    assert counts.plugins == 1
    assert rig.connected == [] and rig.disconnected == []
    assert len(rig.coroutines) == 2
    assert all(inspect.getcoroutinestate(c) == inspect.CORO_CLOSED for c in rig.coroutines)
    messages = [r.getMessage() for r in caplog.records]
    assert any("skipping MCP disconnect" in m and "alpha:old" in m for m in messages)
    assert any("/mcp connect" in m and "alpha:server" in m for m in messages)
