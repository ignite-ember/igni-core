"""Every channel the push bridge owns, and the payload each one carries.

The bridge is the one place where the backend's events become wire
pushes. Every channel it owns is a separate producer with its own
payload shape, so each one gets its own test here: a channel that
loses its payload, or gains another channel's, must fail loudly.

Two kinds of channel live side by side:

* the ones the bridge **awaits** — the login flow and the session
  naming — go straight to the transport;
* the ones it **schedules** — everything that can fire from a reader
  thread, a listener thread or a tool callback — are handed to
  :meth:`PushNotificationBridge._schedule_push`, which hops them onto
  the event loop.

The last test pins the hop itself: a push raised on another thread
still has to arrive, because the reader thread and the file-edit
listener are not the loop's thread.
"""

from __future__ import annotations

import asyncio

from igni_core.backend.push_bridge import PushNotificationBridge
from igni_core.backend.schemas_workflow_events import WorkflowEventRecord
from igni_core.backend.workspace_trust_controller import WorkspaceTrustStatus
from igni_core.core.activity.background import background
from igni_core.core.broadcast_schema import BroadcastEvent
from igni_core.core.tools.orchestrate_events import ToolStartedEvent
from igni_core.core.tools.process_events import (
    ProcessExitEvent,
    ProcessLineEvent,
    ProcessStartEvent,
)
from igni_core.notifications.schemas import NotificationCounts
from igni_core.protocol.schemas.enums import PushChannel
from igni_core.protocol.schemas.push import PushNotification


class _Transport:
    """Records the pushes the bridge sends."""

    def __init__(self) -> None:
        self.sent: list[PushNotification] = []

    async def send(self, push: PushNotification) -> None:
        self.sent.append(push)


class _Bridge:
    """A bridge whose scheduled pushes are captured instead of sent."""

    def __init__(self) -> None:
        self.transport = _Transport()
        self.queue: list[str] = []
        self.scheduled: list[PushNotification] = []
        self.bridge = PushNotificationBridge(
            transport=self.transport,
            loop=None,
            queue=self.queue,
        )
        self.bridge._schedule_push = self.scheduled.append  # type: ignore[method-assign]

    @property
    def sent(self) -> list[PushNotification]:
        """Every push the bridge produced, in the order it produced them."""
        return [*self.transport.sent, *self.scheduled]


def _exit_event(output_tail: str = "last lines") -> ProcessExitEvent:
    return ProcessExitEvent(
        pid=42,
        cmd="pytest",
        exit_code=0,
        duration_seconds=1.5,
        output_tail=output_tail,
    )


# ── The channels the bridge awaits ─────────────────────────────────


class TestTheAwaitedChannels:
    async def test_the_login_status_carries_its_text(self):
        bridge = _Bridge()

        await bridge.bridge.on_login_status("checking your credentials")

        [push] = bridge.transport.sent
        assert push.channel == PushChannel.LOGIN_STATUS
        assert push.payload == {"text": "checking your credentials", "flow_id": ""}

    async def test_the_login_status_carries_the_sign_in_it_is_about(self):
        """The sign-in a status is about is named by the ``flow_id``
        the ``login`` call returned, so a client can tell two sign-ins
        apart."""
        bridge = _Bridge()

        await bridge.bridge.on_login_status("checking your credentials", "flow-7")

        [push] = bridge.transport.sent
        assert push.payload["flow_id"] == "flow-7"

    async def test_the_login_result_carries_the_outcome(self):
        bridge = _Bridge()

        await bridge.bridge.on_login_result(True, "logged in")

        [push] = bridge.transport.sent
        assert push.channel == PushChannel.LOGIN_RESULT
        assert push.payload == {"success": True, "result": "logged in", "flow_id": ""}

    async def test_the_login_result_carries_the_sign_in_it_ends(self):
        bridge = _Bridge()

        await bridge.bridge.on_login_result(True, "logged in", "flow-7")

        [push] = bridge.transport.sent
        assert push.payload["flow_id"] == "flow-7"

    async def test_the_session_named_carries_the_session_and_the_name(self):
        bridge = _Bridge()

        await bridge.bridge.on_session_named("s-1", "Refactor the parser")

        [push] = bridge.transport.sent
        assert push.channel == PushChannel.SESSION_NAMED
        assert push.payload == {"session_id": "s-1", "name": "Refactor the parser"}


# ── The scheduler channel ──────────────────────────────────────────


class TestTheSchedulerChannel:
    """The scheduler runs on the loop, so its pushes go straight to the
    transport — no hop needed."""

    async def test_the_started_push_names_the_task(self):
        bridge = _Bridge()

        bridge.bridge._on_scheduler_started("t-1", "nightly build")
        await background.drain()

        [push] = bridge.transport.sent
        assert push.channel == PushChannel.SCHEDULER_STARTED
        assert push.payload == {"task_id": "t-1", "description": "nightly build"}

    async def test_the_completed_push_carries_the_detail(self):
        bridge = _Bridge()

        bridge.bridge._on_scheduler_completed("t-1", "nightly build", True, "42 rows")
        await background.drain()

        [push] = bridge.transport.sent
        assert push.channel == PushChannel.SCHEDULER_COMPLETED
        assert push.payload == {
            "task_id": "t-1",
            "description": "nightly build",
            "result": "42 rows",
        }

    async def test_a_completion_without_a_detail_says_how_it_ended(self):
        bridge = _Bridge()

        bridge.bridge._on_scheduler_completed("t-1", "nightly build", False)
        await background.drain()

        [push] = bridge.transport.sent
        assert push.payload["result"] == "failed"

    async def test_a_failed_completion_says_so(self):
        bridge = _Bridge()

        bridge.bridge._on_scheduler_completed("t-1", "nightly build", False)
        await background.drain()

        [push] = bridge.transport.sent
        assert push.payload["result"] == "failed"


# ── The process channels ───────────────────────────────────────────


class TestTheProcessChannels:
    def test_the_started_push_carries_the_process(self):
        bridge = _Bridge()

        bridge.bridge._on_process_started(ProcessStartEvent(pid=42, cmd="pytest", started_at=12.5))

        [push] = bridge.scheduled
        assert push.channel == PushChannel.PROCESS_STARTED
        assert push.payload == {"pid": 42, "cmd": "pytest", "started_at": 12.5}

    def test_the_line_push_carries_the_line(self):
        bridge = _Bridge()

        bridge.bridge._on_process_line(ProcessLineEvent(pid=42, line="1 passed"))

        [push] = bridge.scheduled
        assert push.channel == PushChannel.PROCESS_LINE
        assert push.payload == {"pid": 42, "line": "1 passed"}

    def test_the_exited_push_carries_the_exit(self):
        bridge = _Bridge()

        bridge.bridge._on_process_exited(_exit_event())

        [push] = bridge.scheduled
        assert push.channel == PushChannel.PROCESS_EXITED
        assert push.payload == {
            "pid": 42,
            "cmd": "pytest",
            "exit_code": 0,
            "duration_seconds": 1.5,
        }

    def test_the_completion_push_carries_the_exit(self):
        bridge = _Bridge()

        bridge.bridge._on_process_done(_exit_event())

        [push] = bridge.scheduled
        assert push.channel == PushChannel.BACKGROUND_PROCESS_DONE
        assert push.payload == {
            "pid": 42,
            "cmd": "pytest",
            "exit_code": 0,
            "duration_seconds": 1.5,
        }

    def test_the_completion_also_talks_to_the_conversation(self):
        """The queue append is how the agent learns the run finished."""
        bridge = _Bridge()

        bridge.bridge._on_process_done(_exit_event(output_tail="all good"))

        [text] = bridge.queue
        assert "BACKGROUND PROCESS COMPLETED" in text
        assert "PID 42: pytest" in text
        assert "Status: succeeded" in text
        assert "Last output (tail):" in text

    def test_a_completion_with_no_output_still_talks(self):
        bridge = _Bridge()

        bridge.bridge._on_process_done(_exit_event(output_tail=""))

        [text] = bridge.queue
        assert "BACKGROUND PROCESS COMPLETED" in text
        assert "Last output (tail):" not in text

    def test_a_failed_process_says_so(self):
        bridge = _Bridge()
        failed = ProcessExitEvent(
            pid=42,
            cmd="pytest",
            exit_code=1,
            duration_seconds=0.5,
            output_tail="",
        )

        bridge.bridge._on_process_done(failed)

        [text] = bridge.queue
        assert "Status: failed (exit 1)" in text


# ── The file-edit channel ──────────────────────────────────────────


def test_the_file_edit_push_carries_the_path():
    bridge = _Bridge()

    bridge.bridge._on_file_edited("/project/main.py")

    [push] = bridge.scheduled
    assert push.channel == PushChannel.FILE_EDITED
    assert push.payload == {"path": "/project/main.py"}


# ── The open channels ──────────────────────────────────────────────


class TestTheOpenChannels:
    def test_a_broadcast_goes_out_on_its_own_channel(self):
        bridge = _Bridge()

        bridge.bridge._on_broadcast(BroadcastEvent(channel="workspace_changed"))

        [push] = bridge.scheduled
        assert push.channel == "workspace_changed"

    def test_the_orchestrate_progress_goes_out_on_its_channel(self):
        bridge = _Bridge()

        bridge.bridge.on_orchestrate_progress(ToolStartedEvent(agent_path="coder", tool="Read"))

        [push] = bridge.scheduled
        assert push.channel == PushChannel.ORCHESTRATE_EVENT
        assert push.payload == {
            "type": "tool_started",
            "agent_path": "coder",
            "tool": "Read",
            "args": "",
        }

    def test_the_notification_counts_go_out_on_their_channel(self):
        bridge = _Bridge()

        bridge.bridge.notifications_changed(
            NotificationCounts(unread=2, by_source={"igni.artifacts": 2})
        )

        [push] = bridge.scheduled
        assert push.channel == PushChannel.NOTIFICATIONS_CHANGED
        assert push.payload == {"counts": {"unread": 2, "by_source": {"igni.artifacts": 2}}}

    def test_the_workspace_trust_goes_out_on_its_channel(self):
        bridge = _Bridge()
        status = WorkspaceTrustStatus(
            state="ask",
            can_trust=True,
            reason="a new folder",
            path="/project",
            policy_mode="ask",
        )

        bridge.bridge.workspace_trust(status)

        [push] = bridge.scheduled
        assert push.channel == PushChannel.WORKSPACE_TRUST_REQUIRED
        assert push.payload["state"] == "ask"

    def test_the_workflow_event_goes_out_on_its_channel(self):
        bridge = _Bridge()
        record = WorkflowEventRecord(
            workflow_run_id="w-1",
            name="nightly",
            ts=1,
            seq=2,
            type="step",
            payload={"step": "build"},
        )

        bridge.bridge.workflow_event(record)

        [push] = bridge.scheduled
        assert push.channel == PushChannel.WORKFLOW_EVENT
        assert push.payload["workflow_run_id"] == "w-1"
        assert push.payload["payload"] == {"step": "build"}


# ── The hop onto the loop ──────────────────────────────────────────


async def test_a_push_raised_on_another_thread_still_arrives():
    """The reader thread and the file-edit listener are not the loop's
    thread, so the bridge hops onto the loop before sending."""
    transport = _Transport()
    bridge = PushNotificationBridge(
        transport=transport,
        loop=asyncio.get_running_loop(),
        queue=[],
    )

    await asyncio.to_thread(bridge._on_file_edited, "/project/main.py")
    for _ in range(50):
        if transport.sent:
            break
        await asyncio.sleep(0.01)

    assert [push.channel for push in transport.sent] == [PushChannel.FILE_EDITED]


async def test_a_bridge_without_a_loop_drops_the_push_instead_of_crashing():
    """A bridge built without a running loop (a test harness driving
    only the sync RPC handlers) must not blow up on the first push."""
    transport = _Transport()
    bridge = PushNotificationBridge(transport=transport, loop=None, queue=[])

    bridge._on_file_edited("/project/main.py")

    assert transport.sent == []


def test_a_loop_that_has_closed_does_not_take_the_bridge_down():
    """The loop can close under the bridge during shutdown; the push is
    dropped rather than the callback thread crashing."""
    transport = _Transport()
    closed_loop = asyncio.new_event_loop()
    closed_loop.close()
    bridge = PushNotificationBridge(transport=transport, loop=closed_loop, queue=[])

    bridge._on_file_edited("/project/main.py")

    assert transport.sent == []
