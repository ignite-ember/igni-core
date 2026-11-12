"""How the push bridge routes: which run a push names, whose conversation it speaks into,
and what it is wired to.

``test_push_bridge_channels`` pins the payload of every channel. This file
pins the rest of the bridge's job: a push raised inside a run names that
run, a sub-bridge for a pooled session never speaks into the boot
session's conversation, a process finishing is narrated into a
conversation only when there is exactly one it could belong to, and the
boot bridge is bound to every source the backend offers.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from igni_core.backend.push_bridge import PushNotificationBridge
from igni_core.core.activity.background import background
from igni_core.core.run_context import CURRENT_RUN_ID
from igni_core.core.tools.edit import FileEditNotifier, default_file_edit_notifier
from igni_core.core.tools.process_bus import ProcessEventBus
from igni_core.core.tools.process_events import (
    ProcessExitEvent,
    ProcessLineEvent,
    ProcessStartEvent,
)
from igni_core.notifications.schemas import NotificationCounts, NotificationsChanged
from igni_core.protocol.schemas.enums import PushChannel
from igni_core.protocol.schemas.push import PushNotification


class _Transport:
    def __init__(self) -> None:
        self.sent: list[PushNotification] = []

    async def send(self, push: PushNotification) -> None:
        self.sent.append(push)


async def _settle(transport: _Transport, count: int) -> None:
    for _ in range(100):
        if len(transport.sent) >= count:
            return
        await asyncio.sleep(0.01)


def _exit(*, pid: int = 7, code: int = 0, tail: str = "", seconds: float = 2.0) -> ProcessExitEvent:
    return ProcessExitEvent(
        pid=pid, cmd="make", exit_code=code, duration_seconds=seconds, output_tail=tail
    )


async def _bridge(queue: list[str] | None = None) -> tuple[PushNotificationBridge, _Transport]:
    transport = _Transport()
    bridge = PushNotificationBridge(
        transport=transport,
        loop=asyncio.get_running_loop(),
        queue=[] if queue is None else queue,
        file_edit_notifier=FileEditNotifier(),
    )
    return bridge, transport


# ── Which run a push names ─────────────────────────────────────────


class TestThePushNamesItsRun:
    async def test_a_push_raised_inside_a_run_names_that_run(self):
        bridge, transport = await _bridge()
        token = CURRENT_RUN_ID.set("run-9")
        try:
            bridge._on_file_edited("/p/a.py")
        finally:
            CURRENT_RUN_ID.reset(token)
        await _settle(transport, 1)

        [push] = transport.sent
        assert push.run_id == "run-9"

    async def test_the_run_is_read_in_the_caller_not_after_the_hop(self):
        """A thread started by ``to_thread`` carries the caller's context
        copy; the loop callback does not. The bridge must read the run
        where the push is made."""
        bridge, transport = await _bridge()
        token = CURRENT_RUN_ID.set("run-3")
        try:
            await asyncio.to_thread(bridge._on_file_edited, "/p/a.py")
        finally:
            CURRENT_RUN_ID.reset(token)
        await _settle(transport, 1)

        assert [push.run_id for push in transport.sent] == ["run-3"]

    async def test_a_push_outside_any_run_names_none(self):
        bridge, transport = await _bridge()

        bridge._on_file_edited("/p/a.py")
        await _settle(transport, 1)

        [push] = transport.sent
        assert push.run_id == ""

    async def test_a_push_that_already_names_a_run_keeps_it(self):
        bridge, transport = await _bridge()
        named = PushNotification(channel="x", payload={}, run_id="other-run")
        token = CURRENT_RUN_ID.set("run-9")
        try:
            bridge._schedule_push(named)
        finally:
            CURRENT_RUN_ID.reset(token)
        await _settle(transport, 1)

        [push] = transport.sent
        assert push.run_id == "other-run"


# ── Whose conversation a completion speaks into ────────────────────


class TestACompletionSpeaksIntoOneConversation:
    async def test_with_one_session_live_it_is_narrated(self):
        queue: list[str] = []
        bridge, _ = await _bridge(queue)

        bridge._on_process_done(_exit(tail="built"))

        [text] = queue
        assert text == (
            "BACKGROUND PROCESS COMPLETED\n"
            "PID 7: make\n"
            "Status: succeeded  ·  Duration: 2.0s\n\n"
            "Last output (tail):\nbuilt\n\n"
            "For more output: read_process_output(7, tail=N)"
        )

    async def test_without_output_the_text_has_no_tail_block(self):
        queue: list[str] = []
        bridge, _ = await _bridge(queue)

        bridge._on_process_done(_exit(code=2, seconds=0.26))

        assert queue == [
            "BACKGROUND PROCESS COMPLETED\n"
            "PID 7: make\n"
            "Status: failed (exit 2)  ·  Duration: 0.3s\n\n"
            "For more output: read_process_output(7, tail=N)"
        ]

    async def test_with_no_session_counted_it_is_still_narrated(self):
        queue: list[str] = []
        bridge, _ = await _bridge(queue)
        bridge.set_live_session_count(lambda: 0)

        bridge._on_process_done(_exit())

        assert len(queue) == 1

    async def test_with_several_sessions_live_it_is_not_narrated_but_still_pushed(self):
        """The process bus is process-wide and the event names no owner, so
        a build finishing in one window must not speak into another's chat."""
        queue: list[str] = []
        bridge, transport = await _bridge(queue)
        bridge.set_live_session_count(lambda: 2)

        bridge._on_process_done(_exit())
        await _settle(transport, 1)

        assert queue == []
        assert [push.channel for push in transport.sent] == [PushChannel.BACKGROUND_PROCESS_DONE]

    async def test_the_count_is_read_at_each_completion(self):
        queue: list[str] = []
        bridge, _ = await _bridge(queue)
        sessions = [2]
        bridge.set_live_session_count(lambda: sessions[0])

        bridge._on_process_done(_exit(pid=1))
        sessions[0] = 1
        bridge._on_process_done(_exit(pid=2))

        assert len(queue) == 1
        assert "PID 2:" in queue[0]


class TestASubBridge:
    async def test_it_has_its_own_queue(self):
        boot_queue: list[str] = []
        own_queue: list[str] = []
        boot, _ = await _bridge(boot_queue)
        sub = boot.for_transport(_Transport(), queue=own_queue)

        sub._on_process_done(_exit())

        assert len(own_queue) == 1
        assert boot_queue == []

    async def test_it_sends_on_its_own_transport(self):
        boot, boot_transport = await _bridge()
        own = _Transport()
        sub = boot.for_transport(own, queue=[])

        sub._on_file_edited("/p/a.py")
        await _settle(own, 1)

        assert [push.channel for push in own.sent] == [PushChannel.FILE_EDITED]
        assert boot_transport.sent == []

    async def test_it_inherits_the_live_session_probe(self):
        boot, _ = await _bridge()
        boot.set_live_session_count(lambda: 3)
        own_queue: list[str] = []
        sub = boot.for_transport(_Transport(), queue=own_queue)

        sub._on_process_done(_exit())

        assert own_queue == []

    async def test_it_shares_the_loop_and_the_file_edit_notifier(self):
        boot, _ = await _bridge()
        sub = boot.for_transport(_Transport(), queue=[])

        assert sub._loop is boot._loop
        assert sub._file_edit_notifier is boot._file_edit_notifier


# ── What the bridge is bound to ────────────────────────────────────


class TestTheProcessBus:
    async def test_every_process_event_becomes_its_push(self):
        queue: list[str] = []
        bridge, transport = await _bridge(queue)
        bus = ProcessEventBus()
        bridge.bind_to_process_supervisor(SimpleNamespace(registry=SimpleNamespace(bus=bus)))  # type: ignore[arg-type]

        bus.emit(ProcessStartEvent(pid=7, cmd="make", started_at=1.0))
        bus.emit(ProcessLineEvent(pid=7, line="héllo ✓"))
        bus.emit(_exit(code=1))
        await _settle(transport, 4)

        by_channel = {push.channel: push.payload for push in transport.sent}
        assert set(by_channel) == {
            PushChannel.PROCESS_STARTED,
            PushChannel.PROCESS_LINE,
            PushChannel.PROCESS_EXITED,
            PushChannel.BACKGROUND_PROCESS_DONE,
        }
        assert by_channel[PushChannel.PROCESS_LINE] == {"pid": 7, "line": "héllo ✓"}
        assert by_channel[PushChannel.PROCESS_EXITED]["exit_code"] == 1
        assert len(queue) == 1


class TestTheFileEditListener:
    async def test_a_file_the_edit_tool_wrote_is_pushed(self):
        transport = _Transport()
        notifier = FileEditNotifier()
        bridge = PushNotificationBridge(
            transport=transport,
            loop=asyncio.get_running_loop(),
            queue=[],
            file_edit_notifier=notifier,
        )
        bridge.bind_to_file_edit_listener()

        notifier.notify(Path("/p/a.py"))
        await _settle(transport, 1)

        [push] = transport.sent
        assert (push.channel, push.payload) == (PushChannel.FILE_EDITED, {"path": "/p/a.py"})

    async def test_a_bridge_given_no_notifier_shares_the_edit_tools_default(self):

        bridge = PushNotificationBridge(transport=_Transport(), loop=None, queue=[])

        assert bridge._file_edit_notifier is default_file_edit_notifier


class TestTheNotificationsChange:
    def test_a_whole_change_goes_out_as_it_is(self):
        scheduled: list[PushNotification] = []
        bridge = PushNotificationBridge(transport=_Transport(), loop=None, queue=[])
        bridge._schedule_push = scheduled.append  # type: ignore[method-assign]
        change = NotificationsChanged(counts=NotificationCounts(unread=3, by_source={"a": 3}))

        bridge.notifications_changed(change)

        [push] = scheduled
        assert push.channel == PushChannel.NOTIFICATIONS_CHANGED
        assert push.payload == change.model_dump()
        assert push.payload["counts"] == {"unread": 3, "by_source": {"a": 3}}


class _Scheduler:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def start(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _Backend:
    """What the bridge reads from a backend: the queue, the supervisor, the hooks."""

    def __init__(self) -> None:
        self.message_queue: list[str] = ["left waiting by the last backend"]
        self.bus = ProcessEventBus()
        self.supervisor = SimpleNamespace(registry=SimpleNamespace(bus=self.bus))
        self.scheduler = _Scheduler()
        self.controllers = SimpleNamespace(loop=SimpleNamespace(scheduler=self.scheduler))
        self.hooked_queue: Any = None
        self.progress: Any = None
        self.broadcast: Any = None

    def wire_queue_hook(self, queue: Any) -> None:
        self.hooked_queue = queue

    def wire_orchestrate_progress(self, callback: Any) -> None:
        self.progress = callback

    def register_broadcast(self, sink: Any) -> None:
        self.broadcast = sink


class TestTheBootBridge:
    async def test_it_runs_on_the_backends_durable_queue_and_binds_every_source(self):
        backend = _Backend()
        transport = _Transport()

        bridge = PushNotificationBridge.wired(
            transport=transport,
            loop=asyncio.get_running_loop(),
            backend=backend,  # type: ignore[arg-type]
        )
        try:
            assert backend.hooked_queue is backend.message_queue
            assert backend.progress == bridge.on_orchestrate_progress
            assert backend.broadcast == bridge._on_broadcast
            backend.bus.emit(_exit())
            assert len(backend.message_queue) == 2
            assert backend.message_queue[0] == "left waiting by the last backend"
        finally:
            bridge._file_edit_notifier.set_listener(None)

    async def test_the_scheduler_is_started_with_the_bridges_callbacks(self):
        backend = _Backend()
        transport = _Transport()
        bridge = PushNotificationBridge(
            transport=transport, loop=asyncio.get_running_loop(), queue=[]
        )

        bridge.start_scheduler(backend)  # type: ignore[arg-type]
        started = backend.scheduler.kwargs["on_task_started"]
        completed = backend.scheduler.kwargs["on_task_completed"]
        started("t-1", "nightly")
        completed("t-1", "nightly", True, "42 rows")

        await background.drain()

        assert [(p.channel, p.payload) for p in transport.sent] == [
            (PushChannel.SCHEDULER_STARTED, {"task_id": "t-1", "description": "nightly"}),
            (
                PushChannel.SCHEDULER_COMPLETED,
                {"task_id": "t-1", "description": "nightly", "result": "42 rows"},
            ),
        ]


class TestASchedulerCompletion:
    async def test_a_success_without_a_detail_says_completed(self):
        bridge, transport = await _bridge()

        bridge._on_scheduler_completed("t-1", "nightly", True)
        await background.drain()

        [push] = transport.sent
        assert push.payload["result"] == "completed"

    async def test_the_runners_positional_call_carries_the_detail_text(self):
        """The runner calls ``(task_id, description, succeeded, detail)``; a
        flag that lands in the text slot made the payload raise and the push
        was never sent."""
        bridge, transport = await _bridge()

        bridge._on_scheduler_completed("t-1", "nightly", False, "exit 3: disk full")
        await background.drain()

        [push] = transport.sent
        assert push.payload["result"] == "exit 3: disk full"


class TestWithoutALoop:
    def test_a_sync_only_bridge_drops_a_scheduled_push_without_raising(self):
        bridge = PushNotificationBridge(transport=_Transport(), loop=None, queue=[])

        bridge._on_process_line(ProcessLineEvent(pid=1, line="x"))
