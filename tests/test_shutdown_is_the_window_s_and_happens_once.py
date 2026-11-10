"""Stopping is igni's window's, a session is wound down once, and a
session's teardown leaves igni's background processes alone.

* ``shutdown`` — the call and the message — was open to any connection,
  a program with the lockfile's token included.
* The ``shutdown`` call, the pool evicting a session and igni exiting each
  ran the session's teardown again, so ``SessionEnd`` fired twice.
* Each session's teardown killed every background process igni tracks:
  winding one session down — or the pool evicting an idle one — ended
  another window's build. They are igni's, and end with igni.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from igni_core.backend import standing_policy
from igni_core.backend.server_lifecycle import LifecycleController
from igni_core.backend.supervisor import BackendSupervisor
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod
from igni_core.protocol.schemas.envelope import Standing


def _from(standing: Standing, message: msg.Message) -> msg.Message:
    message.mark_from("ws-1", standing)
    return message


@pytest.mark.parametrize("standing", [Standing.PROGRAM, Standing.APPROVER])
def test_only_igni_s_window_stops_igni(standing: Standing) -> None:
    call = msg.RPCRequest(id="s", method=RpcMethod.SHUTDOWN)
    assert standing_policy.refusal(_from(standing, call))
    assert standing_policy.refusal(_from(standing, msg.Shutdown()))


def test_igni_s_window_may_stop_igni() -> None:
    call = msg.RPCRequest(id="s", method=RpcMethod.SHUTDOWN)
    assert standing_policy.refusal(_from(Standing.WINDOW, call)) is None
    assert standing_policy.refusal(_from(Standing.WINDOW, msg.Shutdown())) is None


def _session() -> SimpleNamespace:
    supervisor = SimpleNamespace(registry=MagicMock())
    return SimpleNamespace(
        session_id="s-1",
        hook_executor=SimpleNamespace(execute=AsyncMock()),
        settings=SimpleNamespace(orchestration=SimpleNamespace(auto_cleanup=False)),
        pool=SimpleNamespace(has_ephemeral=False),
        mcp_manager=SimpleNamespace(disconnect_all=AsyncMock()),
        supervisor=supervisor,
    )


async def test_a_session_is_wound_down_once() -> None:
    session = _session()
    lifecycle = LifecycleController(
        session=session,  # type: ignore[arg-type]
        pending_store=None,
        runs=None,
        rehydrate=MagicMock(),
    )

    first = await lifecycle.shutdown()
    again = await lifecycle.shutdown()

    assert again is first
    assert session.hook_executor.execute.await_count == 1
    assert session.mcp_manager.disconnect_all.await_count == 1


async def test_a_session_s_teardown_leaves_igni_s_background_processes_alone() -> None:
    session = _session()
    lifecycle = LifecycleController(
        session=session,  # type: ignore[arg-type]
        pending_store=None,
        runs=None,
        rehydrate=MagicMock(),
    )

    await lifecycle.shutdown()

    session.supervisor.registry.kill_all.assert_not_called()


async def test_igni_s_exit_ends_its_background_processes(tmp_path) -> None:
    transport = SimpleNamespace(close=AsyncMock())
    supervisor = BackendSupervisor(transport=transport, loop=None, project_dir=tmp_path)
    backend = SimpleNamespace(
        shutdown=AsyncMock(), supervisor=SimpleNamespace(registry=MagicMock())
    )
    supervisor.set_backend_fallback(backend)

    await supervisor.teardown()

    backend.supervisor.registry.kill_all.assert_called_once_with()
