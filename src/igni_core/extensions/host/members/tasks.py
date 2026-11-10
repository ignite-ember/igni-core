"""``host.tasks``: the agent's todo list."""

from __future__ import annotations

from igni_core.core.tools.todo import TodoItemWire
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.reads import read_answers
from igni_core.extensions.host.members.tag import get
from igni_core.protocol.rpc import RpcMethod


class TaskMembers(Members):
    @get("tasks.todos")
    async def todos(self, c: Call) -> list[TodoItemWire]:
        return read_answers(
            TodoItemWire,
            await self.rpc(RpcMethod.GET_TODOS, c=c),
            method="tasks.todos",
        )


__all__ = ["TaskMembers"]
