"""Scheduler (recurring / one-shot) RPC handlers."""

from __future__ import annotations

from igni_core.backend.rpc_binding import NoArgs, RpcArgs, rpc
from igni_core.backend.rpc_handlers.base import RpcHandler
from igni_core.backend.rpc_streams import TOOL_PUSHES
from igni_core.core.scheduler.models import ScheduledTask
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod, RpcScope
from igni_core.protocol.schemas.enums import PushChannel


class ExecuteScheduledTaskArgs(RpcArgs):
    """``execute_scheduled_task``: what the task asks the agent to do."""

    description: str


class ScheduledTaskArgs(RpcArgs):
    """``cancel_scheduled_task``: the task to cancel."""

    task_id: str


class ScheduledTasksArgs(RpcArgs):
    """``get_scheduled_tasks``: whether finished tasks are listed too."""

    include_done: bool = True


class SchedulerRpcHandler(RpcHandler):
    """Task execution, cancellation, listing, and scheduler
    (re)start. Starting the scheduler is delegated to the
    :class:`PushNotificationBridge` on the shared context, matching
    the boot-time auto-start path."""

    @rpc(
        RpcMethod.EXECUTE_SCHEDULED_TASK,
        args=ExecuteScheduledTaskArgs,
        scope=RpcScope.SESSION,
        streams=TOOL_PUSHES,
    )
    async def execute_scheduled_task(self, args: ExecuteScheduledTaskArgs) -> str:
        """Run a scheduled task's work now, in this session, and wait for the answer.

        ``description`` is what the task asks the agent to do (a scheduled
        task's ``description``). The agent runs it as a stored task firing,
        not as a live message: its run is not streamed to the windows, and
        the task's own record is not touched. What its tools push while
        they work still goes out. Returns the agent's answer as text.
        Fails when the organisation's group policy refuses the run, or the
        run fails.
        """
        return await self._controllers.loop.scheduler.execute(args.description)

    @rpc(RpcMethod.CANCEL_SCHEDULED_TASK, args=ScheduledTaskArgs, scope=RpcScope.SESSION)
    async def cancel_scheduled_task(self, args: ScheduledTaskArgs) -> msg.Info:
        """Cancel a scheduled task of this session's project.

        ``task_id`` is the task's ``id`` (from ``get_scheduled_tasks``). The
        task is marked cancelled, so it does not run again; a run already
        going is not stopped, but its outcome is not recorded. Returns an
        ``info`` message, sent in place of an ``rpc_response``. An unknown
        id is not detected.
        """
        return await self._controllers.loop.scheduler.cancel(args.task_id)

    @rpc(RpcMethod.GET_SCHEDULED_TASKS, args=ScheduledTasksArgs, scope=RpcScope.SESSION)
    async def get_scheduled_tasks(self, args: ScheduledTasksArgs) -> list[ScheduledTask]:
        """List the scheduled tasks of this session's project.

        ``include_done`` (default ``true``) lists finished, failed and
        cancelled tasks too. Returns the tasks, latest scheduled first:
        each one's ``id``, ``description``, when it is scheduled and was
        created, its ``status``, its ``result`` or ``error``, and its
        ``recurrence`` (empty for a one-shot task).
        """
        return await self._ctx.backend.get_scheduled_tasks(args.include_done)

    @rpc(
        RpcMethod.START_SCHEDULER,
        args=NoArgs,
        scope=RpcScope.SESSION,
        streams=(
            PushChannel.SCHEDULER_STARTED,
            PushChannel.SCHEDULER_COMPLETED,
            *TOOL_PUSHES,
        ),
    )
    def start_scheduler(self, args: NoArgs) -> None:
        """Start running this project's scheduled tasks as they fall due.

        Takes no arguments and returns nothing; a scheduler already running
        is left as it is. From then on each due task runs through the
        agent: ``scheduler_started`` when it starts, ``scheduler_completed``
        with its result when it ends, and whatever its tools push between.
        """
        self._ctx.push.start_scheduler(self._ctx.backend)
