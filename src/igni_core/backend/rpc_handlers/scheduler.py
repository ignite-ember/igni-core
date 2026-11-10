"""Scheduler (recurring / one-shot) RPC handlers."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from igni_core.backend.rpc_binding import Correlation, NoArgs, RpcArgs, rpc
from igni_core.backend.rpc_handlers.base import RpcHandler
from igni_core.backend.rpc_streams import TOOL_PUSHES
from igni_core.core.scheduler.models import ScheduledTask
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcDuration, RpcMethod, RpcScope
from igni_core.protocol.schemas.enums import PushChannel


class ExecuteScheduledTaskArgs(RpcArgs):
    """Arguments for running a scheduled task's work now."""

    description: str = Field(
        description="What the task asks the agent to do (a scheduled task's `description`)."
    )
    run_id: str = Field(
        default="",
        description=(
            "Id to give the run, so the caller can follow the run's pushes (their "
            "`run_id`) as they come; one is made when absent."
        ),
    )


class ScheduledTaskRunResult(BaseModel):
    """The answer of a scheduled task run, and the run's id."""

    run_id: str = Field(description="The run's id: the one asked for, or the one made.")
    result: str = Field(description="The agent's answer, as text.")


class ScheduledTaskArgs(RpcArgs):
    """Arguments naming a scheduled task."""

    task_id: str = Field(description="The task's `id`, as `get_scheduled_tasks` lists it.")


class ScheduledTasksArgs(RpcArgs):
    """Arguments for listing scheduled tasks."""

    include_done: bool = Field(
        default=True, description="Also list completed, failed and cancelled tasks."
    )


class SchedulerRpcHandler(RpcHandler):
    """Task execution, cancellation, listing, and scheduler
    (re)start. Starting the scheduler is delegated to the
    :class:`PushNotificationBridge` on the shared context, matching
    the boot-time auto-start path."""

    @rpc(
        RpcMethod.EXECUTE_SCHEDULED_TASK,
        correlate=Correlation(by="run_id", source="result.run_id"),
        duration=RpcDuration.LONG,
        args=ExecuteScheduledTaskArgs,
        scope=RpcScope.SESSION,
        may_stream=TOOL_PUSHES,
    )
    async def execute_scheduled_task(
        self, args: ExecuteScheduledTaskArgs
    ) -> ScheduledTaskRunResult:
        """Run a scheduled task's work now, in this session, and wait for the answer.

        ``description`` is what the task asks the agent to do (a scheduled
        task's ``description``). The agent runs it as a stored task firing,
        not as a live message: its run is not streamed to the windows, and
        the task's own record is not touched. What its tools push while
        they work still goes out. It is the session's run while it goes:
        it waits for a turn already running, ``get_processing`` reports
        it, and ``cancel_run`` stops it. Returns the agent's answer as
        text with the run's ``run_id`` — the one given, or one made — which
        every push the run's tools make carries too. Fails when the
        organisation's group policy refuses the run, the run is cancelled,
        or the run fails.
        """
        run_id = args.run_id or f"sched-{uuid.uuid4().hex[:12]}"
        result = await self._controllers.loop.scheduler.execute(args.description, run_id=run_id)
        return ScheduledTaskRunResult(run_id=run_id, result=result)

    @rpc(
        RpcMethod.CANCEL_SCHEDULED_TASK,
        duration=RpcDuration.IO,
        args=ScheduledTaskArgs,
        scope=RpcScope.SESSION,
    )
    async def cancel_scheduled_task(self, args: ScheduledTaskArgs) -> msg.Info:
        """Cancel a scheduled task of this session's project.

        ``task_id`` is the task's ``id`` (from ``get_scheduled_tasks``). The
        task is marked cancelled, so it does not run again; a run already
        going is not stopped, but its outcome is not recorded. Returns an
        ``info`` message, sent in place of an ``rpc_response``. Fails for
        an id the project has no task for, and for a task that already
        completed, failed or was cancelled, whose record is left as it is.
        """
        return await self._controllers.loop.scheduler.cancel(args.task_id)

    @rpc(
        RpcMethod.GET_SCHEDULED_TASKS,
        duration=RpcDuration.IO,
        args=ScheduledTasksArgs,
        scope=RpcScope.SESSION,
    )
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
        correlate=Correlation(by="channel"),
        duration=RpcDuration.IO,
        args=NoArgs,
        scope=RpcScope.SESSION,
        streams=(
            PushChannel.SCHEDULER_STARTED,
            PushChannel.SCHEDULER_COMPLETED,
        ),
        may_stream=TOOL_PUSHES,
    )
    def start_scheduler(self, args: NoArgs) -> None:
        """Start running this project's scheduled tasks as they fall due.

        Takes no arguments and returns nothing; a scheduler already running
        is left as it is. From then on each due task runs through the
        agent: ``scheduler_started`` when it starts, ``scheduler_completed``
        with its result when it ends, and whatever its tools push between.
        """
        self._ctx.push.start_scheduler(self._ctx.backend)
