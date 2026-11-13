"""``McpInitPhase``: what the first connect reports, records and rebuilds."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from igni_core.core.mcp.schemas import MCPConnectResult
from igni_core.core.session.mcp_ops import McpFailureCache
from igni_core.core.session.startup import McpInitDeps, McpInitPhase, SessionStartupCoordinator


def _manager(results: dict, **extra):
    m = SimpleNamespace(
        list_servers=lambda: list(results),
        list_connected=lambda: [],
        connect=AsyncMock(side_effect=lambda name: results[name]),
        **extra,
    )
    return m


def _phase(manager, pool=None):
    pool = pool if pool is not None else SimpleNamespace(build_agents=MagicMock())
    holder = {"pool": pool}
    deps = McpInitDeps(
        mcp_manager=manager,
        failures=McpFailureCache(),
        display=MagicMock(),
        pool_ref=lambda: pool,
        rebuild_team=MagicMock(),
    )
    return McpInitPhase(deps), deps, holder


async def test_mixed_results_report_names_reasons_and_rebuild_with_only_the_connected():
    ok = MagicMock(functions={})  # a server with zero tools still counts as connected
    manager = _manager(
        {
            "good": MCPConnectResult.success(ok),
            "bad": MCPConnectResult.failure("handshake refused"),
            "mute": MCPConnectResult.failure(""),
        }
    )
    phase, deps, holder = _phase(manager)
    result = await phase.ensure()
    assert result.connected == ["good"]
    assert result.failed == {"bad": "handshake refused", "mute": "unknown error"}
    assert result.rebuilt is True
    assert result.skipped_reason is None
    holder["pool"].build_agents.assert_called_once_with(mcp_clients={"good": ok})
    deps.rebuild_team.assert_called_once_with()


async def test_every_result_goes_into_the_failure_cache_and_failures_are_told_to_the_person():
    manager = _manager(
        {"good": MCPConnectResult.success(MagicMock()), "bad": MCPConnectResult.failure("nope")}
    )
    phase, deps, _ = _phase(manager)
    await phase.ensure()
    assert deps.failures.reason("bad") == "nope"
    assert deps.failures.reason("good") == ""
    deps.display.print_info.assert_called_once_with("MCP 'bad' connection failed: nope")


async def test_a_failure_without_a_reason_is_told_as_an_unknown_error():
    phase, deps, _ = _phase(_manager({"bad": MCPConnectResult.failure("")}))
    await phase.ensure()
    deps.display.print_info.assert_called_once_with("MCP 'bad' connection failed: unknown error")


async def test_a_second_call_while_the_first_is_still_connecting_does_not_connect_again():
    gate = asyncio.Event()
    manager = _manager({"srv": MCPConnectResult.failure("x")})

    async def slow(name):
        await gate.wait()
        return MCPConnectResult.failure("x")

    manager.connect = AsyncMock(side_effect=slow)
    phase, _, _ = _phase(manager)
    first = asyncio.create_task(phase.ensure())
    await asyncio.sleep(0)
    second = await asyncio.wait_for(phase.ensure(), timeout=1)
    gate.set()
    await first
    assert second.skipped_reason == "already_initialized"
    assert manager.connect.await_count == 1


def test_rebuild_current_prefers_the_public_accessor_and_skips_a_missing_client():
    pool = SimpleNamespace(build_agents=MagicMock())
    a = MagicMock()
    manager = _manager({})
    manager.list_connected = lambda: ["a", "gone"]
    manager.get_client = lambda n: {"a": a}.get(n)
    manager._clients = {"a": "private", "gone": "private-too"}
    phase, deps, _ = _phase(manager, pool)
    phase.rebuild_current()
    pool.build_agents.assert_called_once_with(mcp_clients={"a": a})
    deps.rebuild_team.assert_called_once_with()


def test_the_pool_is_read_when_a_rebuild_happens_so_a_replaced_pool_is_used():
    manager = _manager({})
    manager.list_connected = lambda: ["a"]
    manager.get_client = lambda n: "client"
    phase, _, holder = _phase(manager)
    new_pool = SimpleNamespace(build_agents=MagicMock())
    holder["pool"] = new_pool
    phase.rebuild_current()
    new_pool.build_agents.assert_called_once_with(mcp_clients={"a": "client"})


async def test_the_coordinator_flag_round_trips_and_setting_it_false_rearms_ensure():
    manager = _manager({"srv": MCPConnectResult.failure("x")})
    phase, _, _ = _phase(manager)
    coord = SessionStartupCoordinator(MagicMock(), phase)
    assert coord.mcp_initialized is False
    await coord.ensure_mcp()
    assert coord.mcp_initialized is True and phase.initialized is True
    coord.mcp_initialized = False
    assert phase.initialized is False
    await coord.ensure_mcp()
    assert manager.connect.await_count == 2
    coord.mcp_initialized = True
    assert (await coord.ensure_mcp()).skipped_reason == "already_initialized"
    assert manager.connect.await_count == 2


def test_the_coordinator_hands_the_marketplace_start_to_its_phase():
    marketplace = MagicMock()
    SessionStartupCoordinator(marketplace, MagicMock()).start_marketplace_refresh_background()
    marketplace.start_background.assert_called_once_with()
