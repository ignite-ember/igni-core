"""One workflow run: its events in, its records out, its phase.

:class:`WorkflowEventSink` is the single place a ``workflow_event`` is
persisted and pushed. :class:`WorkflowRun` drains the runtime's stdout,
answers ``agent()`` calls, and makes sure every run ends on a terminal
record, synthesising one when the process exits without saying.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from igni_core.backend.schemas_workflow_events import (
    AgentRequestEvent,
    WorkflowCompletedEvent,
    WorkflowEventRecord,
    WorkflowFailedEvent,
    WorkflowRuntimeEvent,
)
from igni_core.backend.schemas_workflows import (
    WorkflowCancelOutcome,
    WorkflowRunIdentity,
    WorkflowRunPhase,
)
from igni_core.backend.workflow_agent_bridge import WorkflowAgentBridge
from igni_core.backend.workflow_runtime_process import WorkflowRuntimeProcess
from igni_core.core.activity import background
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.session.event_log_schema import WorkflowEventDraft

if TYPE_CHECKING:
    from igni_core.backend.push_bridge import PushNotificationBridge
    from igni_core.core.session.core import Session

logger = logging.getLogger(__name__)

#: How long :meth:`WorkflowRun.finish` waits for the drain to read the
#: last lines once the process has exited.
DRAIN_FLUSH_SECONDS = 2.0


class WorkflowEventSink:
    """Appends each record to the session log, then pushes it to the windows."""

    def __init__(self, session: Session, push: PushNotificationBridge) -> None:
        self._session = session
        self._push = push

    async def publish(self, record: WorkflowEventRecord) -> None:
        result = await self._session.append_event(WorkflowEventDraft(payload=record))
        if result.error:
            # A failed persist must not stop the push — the windows
            # are watching live. The log gap is reported, not raised.
            logger.warning(
                "workflow event %s not persisted: %s", record.workflow_run_id, result.error
            )
        self._push.workflow_event(record)


class WorkflowRun:
    """A live run, from spawn to its terminal record.

    ``phase`` has one writer, :meth:`_transition_to`, and never leaves a
    terminal value: a cancel that races a completion keeps the completion.
    """

    def __init__(
        self,
        *,
        identity: WorkflowRunIdentity,
        process: WorkflowRuntimeProcess,
        sink: WorkflowEventSink,
        bridge: WorkflowAgentBridge,
    ) -> None:
        self._identity = identity
        self._process = process
        self._sink = sink
        self._bridge = bridge
        self._phase = WorkflowRunPhase.RUNNING
        self._drain_task: asyncio.Future[None] | None = None

    @classmethod
    def for_session(
        cls,
        *,
        identity: WorkflowRunIdentity,
        process: WorkflowRuntimeProcess,
        session: Session,
        push: PushNotificationBridge,
        gate: RunGate,
    ) -> WorkflowRun:
        """A run whose records go to ``session`` and ``push``, and whose
        ``agent()`` calls are answered by ``session``'s team.

        ``gate`` is the run gate of the backend process: while it is
        closed, every ``agent()`` call is refused."""
        return cls(
            identity=identity,
            process=process,
            sink=WorkflowEventSink(session, push),
            bridge=WorkflowAgentBridge(session, gate),
        )

    @property
    def identity(self) -> WorkflowRunIdentity:
        return self._identity

    @property
    def phase(self) -> WorkflowRunPhase:
        return self._phase

    def _transition_to(self, phase: WorkflowRunPhase) -> None:
        if not self._phase.is_terminal:
            self._phase = phase

    def start_drain(self) -> None:
        self._drain_task = background.daemon(
            self.drain(), name=f"workflow-drain-{self._identity.workflow_run_id}"
        )

    async def drain(self) -> None:
        """Publish each event; answer each ``agent_request`` concurrently.

        The script is blocked on an agent call until it gets the reply, so
        the drain keeps reading while the answer is worked out.
        """
        try:
            async for event in self._process.events():
                if isinstance(event, AgentRequestEvent):
                    background.daemon(
                        self._answer(event),
                        name=f"workflow-agent-{self._identity.workflow_run_id}-{event.payload.id}",
                    )
                    continue
                await self._sink.publish(WorkflowEventRecord.from_event(self._identity, event))
                self._settle(event)
        except asyncio.CancelledError:
            raise
        except Exception:  # the drain is a task boundary: log it so finish() still ends the run
            logger.exception("workflow stdout drain failed for %s", self._identity.workflow_run_id)

    def _settle(self, event: WorkflowRuntimeEvent) -> None:
        match event:
            case WorkflowCompletedEvent() | WorkflowFailedEvent():
                self._transition_to(event.phase)

    async def _answer(self, request: AgentRequestEvent) -> None:
        reply = await self._bridge.answer(request)
        if not await self._process.send(reply):
            logger.warning(
                "agent response for %s dropped: subprocess stdin closed",
                self._identity.workflow_run_id,
            )

    async def cancel(self) -> WorkflowCancelOutcome:
        """Stop the run, unless its process has already exited."""
        if self._process.returncode is not None:
            return WorkflowCancelOutcome.ALREADY_EXITED
        self._transition_to(WorkflowRunPhase.CANCELLING)
        await self._process.stop()
        return WorkflowCancelOutcome.CANCELLED

    async def finish(self) -> None:
        """Wait for the process to exit and end the run on a terminal record."""
        exit_code = await self._process.wait()
        if self._drain_task is not None:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._drain_task, timeout=DRAIN_FLUSH_SECONDS)
        if not self._phase.is_terminal:
            # Exited without a terminal event: crashed, killed, or cancelled.
            self._transition_to(self._exit_phase(exit_code))
            await self._sink.publish(
                WorkflowEventRecord.synthetic(self._identity, self._phase, exit_code)
            )
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()

    def _exit_phase(self, exit_code: int) -> WorkflowRunPhase:
        if self._phase is WorkflowRunPhase.CANCELLING:
            return WorkflowRunPhase.CANCELLED
        return WorkflowRunPhase.FAILED if exit_code != 0 else WorkflowRunPhase.COMPLETED
