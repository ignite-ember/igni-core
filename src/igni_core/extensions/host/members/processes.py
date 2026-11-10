"""``host.processes``: the background shells."""

from __future__ import annotations

from igni_core.backend.schemas_processes import ProcessRow, ProcessTailResult, StopProcessResult
from igni_core.backend.schemas_rpc import RunShellResult
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.reads import read_answer, read_answers
from igni_core.extensions.host.members.tag import call, get
from igni_core.extensions.host.rpc_wire.work import PidArgs, ShellArgs, TailArgs
from igni_core.protocol.rpc import RpcMethod


class ProcessMembers(Members):
    @get("processes.list")
    async def process_list(self, c: Call) -> list[ProcessRow]:
        return read_answers(
            ProcessRow,
            await self.rpc(RpcMethod.LIST_BACKGROUND_PROCESSES, c=c),
            method="processes.list",
        )

    @call("processes.tail")
    async def tail(self, c: Call) -> ProcessTailResult:
        args = TailArgs(pid=c.params["pid"], tail=c.params.get("lines") or 200)
        return read_answer(
            ProcessTailResult,
            await self.rpc(RpcMethod.READ_PROCESS_TAIL, args, c=c),
            method="processes.tail",
        )

    @call("processes.stop")
    async def stop(self, c: Call) -> StopProcessResult:
        args = PidArgs(pid=c.params["pid"])
        return read_answer(
            StopProcessResult,
            await self.rpc(RpcMethod.STOP_BACKGROUND_PROCESS, args, c=c),
            method="processes.stop",
        )

    @call("processes.run")
    async def run(self, c: Call) -> RunShellResult:
        args = ShellArgs(command=c.params["command"])
        return read_answer(
            RunShellResult,
            await self.rpc(RpcMethod.RUN_SHELL, args, c=c),
            method="processes.run",
        )


__all__ = ["ProcessMembers"]
