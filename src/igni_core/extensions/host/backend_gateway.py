"""The host model's way into igni's backend: RPCs, commands, attach, the account.

Every reach goes through here so a backend failure becomes a refusal in
one place, and every successful RPC is told to whoever listens for
writes (:class:`~.rpc_write.RpcWrite`) — the broadcasts,
which turn a write into its signal.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from igni_core.extensions.host.backend import HostBackend
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.rpc_write import RpcWrite
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.values import Account, CommandOutcome, SessionAttached
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod

WriteListener = Callable[[RpcWrite], None]


class HostBackendGateway:
    """igni's backend, as the members reach it: failures as refusals, writes told."""

    def __init__(self, backend: HostBackend) -> None:
        self._backend = backend
        self._listeners: list[WriteListener] = []

    def on_write(self, listener: WriteListener) -> None:
        """Be told every RPC that succeeded."""
        self._listeners.append(listener)

    def default_session(self) -> str:
        return self._backend.default_session()

    async def rpc(self, method: RpcMethod, args: dict[str, Any], *, session_id: str) -> Any:
        """An RPC handler, as the UI calls it. Its failures are refusals."""
        try:
            result = await self._backend.rpc(method, args, session_id=session_id)
        except HostModelError:
            raise
        except KeyError as exc:
            raise HostModelError(ErrorCode.INVALID, f"{method.value}: {exc} is required") from None
        except (ValueError, PermissionError, FileNotFoundError) as exc:
            raise HostModelError(ErrorCode.REFUSED, str(exc)) from None
        write = RpcWrite(method=method, args=args, session_id=session_id)
        for listener in list(self._listeners):
            listener(write)
        return result

    async def command(self, text: str, *, session_id: str) -> CommandOutcome:
        """A slash command, answered here rather than in the windows."""
        answer = await self._backend.ask(msg.Command(text=text), session_id=session_id)
        return CommandOutcome.from_answer(answer)

    async def attach(self, session_id: str, project_dir: str | None) -> SessionAttached:
        try:
            return await self._backend.attach(session_id, project_dir)
        except ValueError as exc:
            raise HostModelError(ErrorCode.REFUSED, str(exc)) from None

    async def account(self, session_id: str) -> Account:
        status = await self._backend.rpc(RpcMethod.GET_STATUS, {}, session_id=session_id)
        return Account.from_status(status)


__all__ = ["HostBackendGateway"]
