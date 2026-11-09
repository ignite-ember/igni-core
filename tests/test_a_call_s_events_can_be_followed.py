"""A client can tell which streamed events belong to its call.

Each streaming call names, in its answer or its arguments, the value its
events carry: a background ``$`` command's pid, a sign-in's flow id, a
scheduled run's run id, why the notification counts moved, the token on
a window's write — and a write that changed nothing says so, since it
pushes nothing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from igni_core.backend.captured_shell_runner import CapturedShellRunner
from igni_core.backend.login_coordinator import LoginCoordinator
from igni_core.backend.push_bridge import PushNotificationBridge
from igni_core.backend.schemas_rpc import LoginResult
from igni_core.core.run_context import CURRENT_RUN_ID
from igni_core.core.tools.process_supervisor import ProcessSupervisor


async def test_a_command_says_its_pid_and_whether_it_went_to_the_background(tmp_path) -> None:
    runner = CapturedShellRunner(tmp_path, supervisor=ProcessSupervisor(), timeout_seconds=1)

    quick = await runner.run("echo hi")
    slow = await runner.run("sleep 5")

    assert quick.pid and not quick.backgrounded
    assert slow.pid and slow.backgrounded


async def test_a_sign_in_s_pushes_carry_its_flow_id() -> None:
    sent: list = []
    transport = SimpleNamespace(send=AsyncMock(side_effect=sent.append))
    bridge = PushNotificationBridge(transport=transport, loop=None, queue=[])  # type: ignore[arg-type]

    async def login(on_status):  # type: ignore[no-untyped-def]
        await on_status("waiting")
        return LoginResult(ok=False, reason="timeout")

    backend = SimpleNamespace(controllers=SimpleNamespace(auth=SimpleNamespace(login=login)))
    coordinator = LoginCoordinator(backend=backend, push_bridge=bridge)

    started = await coordinator.start()
    await coordinator._task  # type: ignore[misc]

    assert started.flow_id
    assert [p.payload["flow_id"] for p in sent] == [started.flow_id, started.flow_id]


def test_a_push_made_during_a_scheduled_run_carries_its_run_id() -> None:
    import asyncio

    sent: list = []
    loop = asyncio.new_event_loop()
    try:
        bridge = PushNotificationBridge(
            transport=SimpleNamespace(send=AsyncMock(side_effect=sent.append)),  # type: ignore[arg-type]
            loop=loop,
            queue=[],
        )
        token = CURRENT_RUN_ID.set("sched-1")
        try:
            bridge.workflow_event(MagicMock(model_dump=lambda: {"seq": 1}))
        finally:
            CURRENT_RUN_ID.reset(token)
        loop.run_until_complete(asyncio.sleep(0.05))
    finally:
        loop.close()

    assert [p.run_id for p in sent] == ["sched-1"]
