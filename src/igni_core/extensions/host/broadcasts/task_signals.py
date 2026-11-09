"""Tasks, the todo list and the scheduler: from messages, pushes and RPC writes."""

from __future__ import annotations

from typing import Any, ClassVar

from igni_core.extensions.host.broadcasts.push_payloads import SchedulerPush, TodosPush
from igni_core.extensions.host.broadcasts.signal import Handler, PushHandler
from igni_core.extensions.host.signal_values import Emitter, Signal
from igni_core.protocol import messages as msg
from igni_core.protocol.schemas.be_events import TaskSnapshot
from igni_core.protocol.schemas.enums import PushChannel


class TaskSignal(Signal):
    session_id: str
    task_id: str
    #: Named when the task is created; an update does not repeat it.
    title: str = ""
    assignee: str
    status: str


class TaskCreatedSignal(TaskSignal):
    WIRE: ClassVar[str] = "tasks.created"


class TaskUpdatedSignal(TaskSignal):
    WIRE: ClassVar[str] = "tasks.updated"


class TaskIterationSignal(Signal):
    WIRE: ClassVar[str] = "tasks.iteration"

    session_id: str
    iteration: int
    max_iterations: int


class TaskSnapshotSignal(Signal):
    WIRE: ClassVar[str] = "tasks.snapshot"

    session_id: str
    tasks: list[TaskSnapshot]


class TodosChanged(Signal):
    WIRE: ClassVar[str] = "tasks.todos_changed"

    session_id: str
    #: The todo items, as the todo tool writes them.
    todos: list[Any]


class SchedulerSignal(Signal):
    """``scheduler.event`` — from the scheduler's message, its pushes, or a cancel."""

    WIRE: ClassVar[str] = "scheduler.event"

    kind: str
    task_id: str
    description: str
    result: str

    @classmethod
    def from_event(cls, m: msg.SchedulerEvent) -> SchedulerSignal:
        return cls(kind=m.event_type, task_id=m.task_id, description=m.description, result=m.result)

    @classmethod
    def from_push(cls, kind: str, push: SchedulerPush) -> SchedulerSignal:
        return cls(
            kind=kind, task_id=push.task_id, description=push.description, result=push.outcome()
        )

    @classmethod
    def cancelled(cls, task_id: str) -> SchedulerSignal:
        return cls(kind="cancelled", task_id=task_id, description="", result="")


class TaskSignals:
    """Translates the task and scheduler traffic."""

    #: The scheduler pushes, by the kind each one tells.
    SCHEDULER_KINDS: ClassVar[dict[PushChannel, str]] = {
        PushChannel.SCHEDULER_STARTED: "started",
        PushChannel.SCHEDULER_COMPLETED: "completed",
    }

    def __init__(self, emitter: Emitter) -> None:
        self._emitter = emitter

    def handlers(self) -> dict[type[msg.Message], Handler]:
        return {
            msg.TaskCreated: lambda m: self._emitter.send(TaskCreatedSignal.of(m)),
            msg.TaskUpdated: lambda m: self._emitter.send(TaskUpdatedSignal.of(m)),
            msg.TaskIteration: lambda m: self._emitter.send(TaskIterationSignal.of(m)),
            msg.TaskStateUpdated: lambda m: self._emitter.send(TaskSnapshotSignal.of(m)),
            msg.SchedulerEvent: lambda m: self._emitter.send(SchedulerSignal.from_event(m)),
        }

    def push_handlers(self) -> dict[PushChannel, PushHandler]:
        return {
            PushChannel.SCHEDULER_STARTED: self.scheduler,
            PushChannel.SCHEDULER_COMPLETED: self.scheduler,
            PushChannel.TODOS_UPDATED: self.todos,
        }

    def scheduler(self, m: msg.PushNotification) -> None:
        push = SchedulerPush.model_validate(m.payload)
        kind = self.SCHEDULER_KINDS[PushChannel(m.channel)]
        self._emitter.send(SchedulerSignal.from_push(kind, push))

    def todos(self, m: msg.PushNotification) -> None:
        push = TodosPush.model_validate(m.payload)
        self._emitter.send(TodosChanged(session_id=m.session_id, todos=push.todos))

    def cancelled(self, task_id: str) -> None:
        """A scheduled task cancelled through RPC, which pushes nothing of its own."""
        self._emitter.send(SchedulerSignal.cancelled(task_id))


__all__ = [
    "SchedulerSignal",
    "TaskCreatedSignal",
    "TaskIterationSignal",
    "TaskSignal",
    "TaskSignals",
    "TaskSnapshotSignal",
    "TaskUpdatedSignal",
    "TodosChanged",
]
