"""``host.scheduler``: the scheduled tasks.

A task is run by its description — what the scheduler hands the agent —
so ``run_now`` finds the task first and passes on what it carries.
"""

from __future__ import annotations

from igni_core.core.scheduler.models import ScheduledTask
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.reads import read_answers
from igni_core.extensions.host.members.tag import call, get
from igni_core.extensions.host.rpc_wire.work import (
    ExecuteTaskArgs,
    ScheduledTaskArgs,
    ScheduledTasksArgs,
)
from igni_core.extensions.host.spec import ErrorCode
from igni_core.protocol.rpc import RpcMethod


class SchedulerMembers(Members):
    @get("scheduler.tasks")
    async def tasks(self, c: Call) -> list[ScheduledTask]:
        return read_answers(ScheduledTask, await self._listing(c), method="scheduler.tasks")

    @call("scheduler.start")
    async def start(self, c: Call) -> None:
        await self.rpc(RpcMethod.START_SCHEDULER, c=c)

    @call("scheduler.run_now")
    async def run_now(self, c: Call) -> str:
        task_id = str(c.params["task_id"])
        task = next((t for t in await self._tasks(c) if t.id == task_id), None)
        if task is None:
            raise HostModelError(ErrorCode.NOT_FOUND, f"no scheduled task {task_id!r}")
        args = ExecuteTaskArgs(description=task.description)
        ran = await self.rpc(RpcMethod.EXECUTE_SCHEDULED_TASK, args, c=c)
        return str(ran["result"])

    @call("scheduler.cancel")
    async def cancel(self, c: Call) -> None:
        args = ScheduledTaskArgs(task_id=c.params["task_id"])
        await self.rpc(RpcMethod.CANCEL_SCHEDULED_TASK, args, c=c)

    async def _tasks(self, c: Call) -> list[ScheduledTask]:
        """The scheduled tasks, read through the model that carries them."""
        return read_answers(ScheduledTask, await self._listing(c), method="scheduler.tasks")

    async def _listing(self, c: Call) -> list[dict]:
        """``get_scheduled_tasks``, done ones included, as the backend gives it."""
        found = await self.rpc(
            RpcMethod.GET_SCHEDULED_TASKS, ScheduledTasksArgs(include_done=True), c=c
        )
        if not isinstance(found, list):
            raise HostModelError(
                ErrorCode.FAILED,
                f"scheduler.tasks answered with {type(found).__name__}, not a list",
            )
        return found


__all__ = ["SchedulerMembers"]
