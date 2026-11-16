"""A pooled session's sub-agent progress must reach the window live.

Bug (campaign R665/R666): the boot session's team got its live
progress callback from ``PushNotificationBridge.wire_all``. A session
the pool resumed (``SessionRuntimeFactory.create``) has its own
backend and its own team, and got none: the factory mirrored the
boot's queue wiring and its broadcast-bus binding, but not the
progress wiring. So the sub-agent's events reached the event log (the
card recorder is wired per backend) and no push ever reached the
window.

The visible symptom was a team card and its charts that appeared only
after the chat was reopened — the history replay drew what the log
held — and never while the team was working.

These tests pin that a pooled session's progress events go out live on
``orchestrate_event``, stamped with the session's own id, exactly as
the boot session's do.
"""

from __future__ import annotations

import asyncio

from igni_core.backend.login_coordinator import LoginCoordinator
from igni_core.backend.pool_directories import PoolDirectories
from igni_core.backend.pool_wiring import PoolWiring
from igni_core.backend.push_bridge import PushNotificationBridge
from igni_core.backend.schemas_pool import ProjectDirs
from igni_core.backend.session_runtime_factory import SessionRuntimeFactory
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.config.settings import Settings
from igni_core.core.db.locator import StorageLocator
from igni_core.core.session.session_directories import SessionDirectoryStore
from igni_core.core.tools.orchestrate_events import AgentStartedEvent
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.tools.process_supervisor_locator import SupervisorLocator
from igni_core.protocol import messages as msg
from tests.shared_tables import SharedTables
from tests.test_pool_broadcast_wiring import (
    _CapturingTransport,
    _PooledBackend,
    _PooledRuntimes,
)


async def _pooled_runtime(tmp_path, windows):
    """Make one pooled session, the way the pool does, and return it."""
    loop = asyncio.get_running_loop()
    push = PushNotificationBridge(transport=windows, loop=loop, queue=[])
    settings = Settings(storage={"data_dir": str(tmp_path / "data")})
    boot = _PooledBackend(settings, project_dir=tmp_path, resume_session_id="boot")
    directories = PoolDirectories(
        SessionDirectoryStore.from_locator(StorageLocator.from_settings(settings)),
        ProjectDirs(project_dir=tmp_path),
    )
    tables = SharedTables()
    factory = _PooledRuntimes(
        wiring=PoolWiring(
            transport=windows,
            push_bridge=push,
            login=LoginCoordinator(backend=boot, push_bridge=push),
            supervisors=SupervisorLocator(),
            gate=RunGate(),
            contributions=tables.contributions,
            prompt_loader=tables.prompt_loader,
            tool_hints=tables.tool_hints,
            extensions=tables.extensions,
        ),
        settings=settings,
        dirs=directories,
    )
    return await factory.create("sess-pool-7")


class TestPooledProgressReachesTheWindow:
    async def test_a_pooled_session_is_given_a_progress_callback(self, tmp_path) -> None:
        windows = _CapturingTransport()
        runtime = await _pooled_runtime(tmp_path, windows)

        callback = getattr(runtime.backend, "progress_callback", None)
        assert callback is not None, (
            "a pooled session must be given the live progress callback; "
            "without it no orchestrate_event push ever reaches the window"
        )

    async def test_a_progress_event_goes_out_live_on_orchestrate_event(self, tmp_path) -> None:
        windows = _CapturingTransport()
        runtime = await _pooled_runtime(tmp_path, windows)

        runtime.backend.progress_callback(
            AgentStartedEvent(agent_path="root.worker", agent="worker", card_id="card-1")
        )
        await asyncio.sleep(0.01)

        (pushed,) = windows.sent
        assert isinstance(pushed, msg.PushNotification)
        assert pushed.channel == "orchestrate_event"
        assert pushed.payload["type"] == "agent_started"
        assert pushed.payload["agent_path"] == "root.worker"
        assert pushed.payload["card_id"] == "card-1"

    async def test_the_push_carries_the_pooled_session_s_own_id(self, tmp_path) -> None:
        # The window filters pushes by session id: a push filed under
        # another chat's id is dropped, and the card never shows.
        windows = _CapturingTransport()
        runtime = await _pooled_runtime(tmp_path, windows)

        runtime.backend.progress_callback(
            AgentStartedEvent(agent_path="root.worker", agent="worker", card_id="card-1")
        )
        await asyncio.sleep(0.01)

        (pushed,) = windows.sent
        assert pushed.session_id == "sess-pool-7"

    async def test_the_boot_session_is_wired_the_same_way(self, tmp_path) -> None:
        # The boot path must keep its wiring: the pool fix must not
        # replace it with the pooled one.
        import inspect

        source = inspect.getsource(PushNotificationBridge.wire_all)
        assert "wire_orchestrate_progress" in source

    async def test_the_pool_factory_wires_the_progress_callback(self, tmp_path) -> None:
        # The wiring itself, pinned on the factory: a session the pool
        # resumes has its own team, so its own backend's wiring is the
        # one that matters.
        import inspect

        source = inspect.getsource(SessionRuntimeFactory.create)
        assert "wire_orchestrate_progress" in source

    async def test_a_pooled_session_keeps_the_process_supervisor(self, tmp_path) -> None:
        # Unchanged by this fix, but it is the same wiring block — a
        # guard against the fix disturbing it.
        windows = _CapturingTransport()
        runtime = await _pooled_runtime(tmp_path, windows)
        assert isinstance(runtime.backend.supervisor, ProcessSupervisor)
