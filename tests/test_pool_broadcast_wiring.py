"""Pool-created sessions must receive broadcast callbacks too.

Bug: ``_create_runtime`` in ``backend/__main__.py`` used to spawn a
fresh BackendServer + Session for every session the pool was asked
to materialize, but never registered the broadcast callback that
turns ``session.broadcast(event)`` calls into outgoing
``PushNotification`` messages. The boot-time default session had
the wiring; every pool-created session was a silent black hole.

The visible symptom was the agent's ``exit_plan_mode(plan=..., tasks=...)``
producing a regular markdown reply instead of a dedicated PlanCard:
the BE-side broadcast happened, but with zero subscribers it never
reached the wire and the FE never saw ``plan_submitted``.

These tests pin that the broadcast→PushNotification translation
happens for both the boot path and a pool-equivalent path.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from igni_core.backend.login_coordinator import LoginCoordinator
from igni_core.backend.pool_directories import PoolDirectories
from igni_core.backend.pool_wiring import PoolWiring
from igni_core.backend.push_bridge import PushNotificationBridge
from igni_core.backend.schemas_pool import ProjectDirs
from igni_core.backend.session_pool import SessionStampingTransport
from igni_core.backend.session_runtime_factory import SessionRuntimeFactory
from igni_core.core.broadcast_schema import BroadcastEvent
from igni_core.core.config.settings import Settings
from igni_core.core.db.locator import StorageLocator
from igni_core.core.session.broadcast import BroadcastBus
from igni_core.core.session.core import Session
from igni_core.core.session.schemas import PermissionModeChangedEvent
from igni_core.core.session.session_directories import SessionDirectoryStore
from igni_core.core.tools.plan import PlanModeEnteredEvent, PlanSubmittedEvent
from igni_core.protocol import messages as msg


class _PooledBackend:
    """Stands in for ``BackendServer`` where the pool resumes a session."""

    def __init__(
        self,
        settings: Any,
        *,
        project_dir: Any,
        resume_session_id: str,
        additional_dirs: Any = None,
    ) -> None:
        self.session_id = resume_session_id
        self.project_dir = project_dir
        self.message_queue: list[str] = []
        self.broadcast_bus = BroadcastBus()

    async def startup(self) -> None:
        pass

    def wire_queue_hook(self, queue: Any) -> None:
        pass

    def register_broadcast(self, callback: Any) -> None:
        self.broadcast_bus.register(callback)

    def attach_runtime(self, runtime: Any) -> None:
        self.runtime = runtime


class _PooledRuntimes(SessionRuntimeFactory):
    server_cls = _PooledBackend  # type: ignore[assignment]


class _CapturingTransport:
    """Records every outbound message."""

    def __init__(self) -> None:
        self.sent: list[Any] = []
        self.is_closed = False

    async def send(self, message: Any) -> None:
        self.sent.append(message)


class _StubSession:
    """Mimics the parts of Session that ``register_broadcast_callback``
    and ``broadcast`` touch, without booting all of Agno + persistence.

    Composes a real :class:`BroadcastBus` — the class is a pure
    in-memory data structure with no external deps, so pulling it
    in here keeps the stub honest about the invariant Session
    holds in production."""

    def __init__(self) -> None:
        self.broadcast_bus = BroadcastBus()

    def register_broadcast_callback(self, cb) -> None:
        self.broadcast_bus.register(cb)

    def broadcast(self, event: BroadcastEvent) -> None:
        self.broadcast_bus.emit(event)


def _make_push_sink_under_test(send_through: Any, loop: asyncio.AbstractEventLoop):
    """Mirror of :meth:`PushNotificationBridge._on_broadcast` (serialise
    the event at the transport boundary, hop onto the loop) so the test
    pins the wiring shape without building a whole bridge."""

    def _on_event(event: BroadcastEvent) -> None:
        def _send() -> None:
            asyncio.ensure_future(
                send_through.send(
                    msg.PushNotification(channel=event.channel, payload=event.to_payload())
                )
            )

        loop.call_soon_threadsafe(_send)

    return _on_event


class TestBroadcastSinkShape:
    async def test_plan_submitted_reaches_transport(self) -> None:
        # ``exit_plan_mode`` emits a ``PlanSubmittedEvent``.
        # With the sink registered, a PushNotification must land
        # on the transport.
        loop = asyncio.get_running_loop()
        transport = _CapturingTransport()
        session = _StubSession()
        session.register_broadcast_callback(_make_push_sink_under_test(transport, loop))

        session.broadcast(
            PlanSubmittedEvent(plan="Add a comment.", tasks=[{"content": "x", "status": "pending"}])
        )
        # The callback hops onto the loop via ``call_soon_threadsafe``;
        # let the scheduler run the deferred send.
        await asyncio.sleep(0.01)

        assert len(transport.sent) == 1
        push = transport.sent[0]
        assert isinstance(push, msg.PushNotification)
        assert push.channel == "plan_submitted"
        assert push.payload["plan"] == "Add a comment."
        assert push.payload["tasks"][0]["content"] == "x"

    async def test_permission_mode_changed_uses_same_path(self) -> None:
        # Same wiring serves the badge updates — regression check.
        loop = asyncio.get_running_loop()
        transport = _CapturingTransport()
        session = _StubSession()
        session.register_broadcast_callback(_make_push_sink_under_test(transport, loop))

        session.broadcast(PermissionModeChangedEvent(mode="plan", previous="default"))
        session.broadcast(PlanModeEnteredEvent(mode="plan", source="agent", reason="r"))
        await asyncio.sleep(0.01)

        assert [p.channel for p in transport.sent] == ["permission_mode_changed"] * 2
        assert transport.sent[0].payload == {"mode": "plan", "previous": "default"}
        assert transport.sent[1].payload == {"mode": "plan", "source": "agent", "reason": "r"}

    async def test_zero_subscribers_drops_silently(self) -> None:
        # The pre-fix behavior: no callback registered → broadcast
        # iterates an empty list → silent. Tests the symptom we just
        # fixed, so it stays a regression.
        session = _StubSession()
        session.broadcast(PlanSubmittedEvent(plan="x"))  # must not raise
        assert not session.broadcast_bus.has_callbacks

    async def test_stamped_transport_carries_session_id(self) -> None:
        # The pool callback must use the SessionStampingTransport so
        # the PushNotification's session_id is set; the FE filters
        # views by session_id and would otherwise drop the push or
        # render it in the wrong view.
        class _Backend:
            session_id = "sess-pool-7"

        loop = asyncio.get_running_loop()
        inner = _CapturingTransport()
        stamped = SessionStampingTransport(inner, _Backend())
        session = _StubSession()
        session.register_broadcast_callback(_make_push_sink_under_test(stamped, loop))

        session.broadcast(PlanSubmittedEvent(plan="y"))
        await asyncio.sleep(0.01)

        assert len(inner.sent) == 1
        push = inner.sent[0]
        assert push.session_id == "sess-pool-7"


class TestPostRunDeferral:
    """``exit_plan_mode`` calls ``queue_post_run_broadcast`` so the
    PlanCard appears AFTER the agent's closing reply, not mid-stream
    above it. These tests pin the queue + drain contract."""

    def _bus_session(self) -> Session:
        session = Session.__new__(Session)
        session.broadcast_bus = BroadcastBus()
        return session

    def test_queue_holds_until_drain(self) -> None:
        session = self._bus_session()
        received: list[tuple[str, dict]] = []
        session.broadcast_bus.register(lambda e: received.append((e.channel, e.to_payload())))

        session.queue_post_run_broadcast(PlanSubmittedEvent(plan="x"))

        # Nothing fires until drain — that's the whole point.
        assert received == []
        assert session.broadcast_bus.pending_count == 1

        session.drain_post_run_broadcasts()

        assert received == [("plan_submitted", {"plan": "x", "tasks": []})]
        # Queue empties so a later drain doesn't re-emit.
        assert session.broadcast_bus.pending_count == 0

    def test_drain_with_empty_queue_is_noop(self) -> None:
        session = self._bus_session()
        received: list = []
        session.broadcast_bus.register(lambda e: received.append((e.channel, e.to_payload())))

        session.drain_post_run_broadcasts()
        assert received == []


class TestOrchestratorAndAppWiring:
    """Confirm the broadcast-bus binding runs in BOTH the boot path
    (``BackendApp.setup`` → ``PushNotificationBridge.wire_all``) and
    the pool path (``SessionRuntimeFactory.create`` →
    ``bind_to_broadcast_bus`` on the runtime's own bridge)."""

    def test_boot_wires_broadcast_via_push_bridge(self) -> None:
        # ``wire_all`` is what ``BackendApp.setup`` calls — must
        # include the broadcast-bus bind for the default runtime.
        assert "bind_to_broadcast_bus" in inspect.getsource(PushNotificationBridge.wire_all), (
            "PushNotificationBridge.wire_all must bind the broadcast "
            "bus, or the default runtime silently drops broadcasts"
        )

    async def test_a_pooled_session_s_broadcasts_reach_the_windows_stamped(self, tmp_path) -> None:
        # The pool path: a session the pool resumes is made by
        # ``SessionRuntimeFactory.create``, which must bind its broadcast
        # bus through a bridge on the session's stamped transport.
        loop = asyncio.get_running_loop()
        windows = _CapturingTransport()
        push = PushNotificationBridge(transport=windows, loop=loop, queue=[])
        settings = Settings(storage={"data_dir": str(tmp_path / "data")})
        boot = _PooledBackend(settings, project_dir=tmp_path, resume_session_id="boot")
        directories = PoolDirectories(
            SessionDirectoryStore.from_locator(StorageLocator.from_settings(settings)),
            ProjectDirs(project_dir=tmp_path),
        )
        factory = _PooledRuntimes(
            wiring=PoolWiring(
                transport=windows,
                push_bridge=push,
                login=LoginCoordinator(backend=boot, push_bridge=push),
            ),
            settings=settings,
            dirs=directories,
        )

        runtime = await factory.create("sess-pool-7")
        runtime.backend.broadcast_bus.emit(PlanSubmittedEvent(plan="y"))
        await asyncio.sleep(0.01)

        (pushed,) = windows.sent
        assert isinstance(pushed, msg.PushNotification)
        assert pushed.channel == "plan_submitted"
        assert pushed.session_id == "sess-pool-7"
