"""``terminal_open``, ``terminal_input``, ``terminal_resize`` and ``terminal_close``.

Only igni's window reaches these (``backend/standing_policy.py``), and a
window reaches only the terminals it opened. Pool-level: a terminal is
its connection's, not a session's.
"""

from __future__ import annotations

from collections.abc import Callable

from igni_core.backend.pool_rpcs.base import PoolRefusal, PoolRpc, UnavailableRpc
from igni_core.backend.rpc_binding import Correlation, rpc
from igni_core.backend.schemas_terminal import (
    TerminalAck,
    TerminalCloseArgs,
    TerminalInputArgs,
    TerminalOpenArgs,
    TerminalOpened,
    TerminalResizeArgs,
)
from igni_core.backend.terminal.exceptions import TerminalLimitReached, TerminalUnavailable
from igni_core.backend.terminal.terminal import Terminal
from igni_core.backend.terminal.terminal_registry import TerminalRegistry
from igni_core.core.config.permission_eval.mode_policy import ModePolicy
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcDuration, RpcMethod, RpcScope
from igni_core.protocol.schemas.enums import PushChannel
from igni_core.protocol.schemas.rpc import RpcErrorCode

#: The organisation's policy as it stands now (``None``: no policy read).
PolicySource = Callable[[], ModePolicy | None]


class TerminalRpc(PoolRpc):
    """Opens a shell for the window that asks, and types into, resizes and closes its own."""

    NOT_MOUNTED = "terminal: this igni has no terminal"
    NO_CONNECTION = (
        "the terminal needs a window on igni's WebSocket connection, so its output reaches "
        "that window alone"
    )

    def __init__(self, terminals: TerminalRegistry, policy: PolicySource) -> None:
        self._terminals = terminals
        self._policy = policy

    @classmethod
    def unmounted(cls) -> list[UnavailableRpc]:
        return [
            UnavailableRpc(RpcMethod.TERMINAL_OPEN, cls.NOT_MOUNTED),
            UnavailableRpc(RpcMethod.TERMINAL_INPUT, cls.NOT_MOUNTED),
            UnavailableRpc(RpcMethod.TERMINAL_RESIZE, cls.NOT_MOUNTED),
            UnavailableRpc(RpcMethod.TERMINAL_CLOSE, cls.NOT_MOUNTED),
        ]

    @rpc(
        RpcMethod.TERMINAL_OPEN,
        correlate=Correlation(by="payload.terminal_id", source="result.terminal_id"),
        duration=RpcDuration.IO,
        args=TerminalOpenArgs,
        scope=RpcScope.IGNI,
        streams=(PushChannel.TERMINAL_OUTPUT, PushChannel.TERMINAL_EXITED),
    )
    def open(self, args: TerminalOpenArgs, request: msg.RPCRequest) -> TerminalOpened:
        """Open an interactive terminal: the person's login shell, in the project folder.

        ``cols`` and ``rows`` are its size (1 to 1000 each). Returns the
        ``terminal_id`` every later call names, the ``shell`` it runs and the
        ``cwd`` it started in. What the shell prints comes as
        ``terminal_output`` pushes, and its end once as ``terminal_exited``,
        to this window alone. The terminal ends when this window's connection
        does. A window may have eight open at once.

        Refused when the organisation turned the terminal off
        (``permissions.allow_terminal: false``) or the window already has
        eight; unavailable when this machine has no terminal support.
        """
        policy = self._policy()
        refusal = policy.terminal_refusal() if policy is not None else None
        if refusal is not None:
            raise PoolRefusal(f"{request.method}: {refusal}", RpcErrorCode.REFUSED)
        if not request.from_client:
            raise PoolRefusal(f"{request.method}: {self.NO_CONNECTION}", RpcErrorCode.UNAVAILABLE)
        try:
            return self._terminals.open(request.from_client, args.cols, args.rows)
        except TerminalLimitReached as exc:
            raise PoolRefusal(f"{request.method}: {exc}", RpcErrorCode.REFUSED) from exc
        except TerminalUnavailable as exc:
            raise PoolRefusal(f"{request.method}: {exc}", RpcErrorCode.UNAVAILABLE) from exc
        except OSError as exc:
            raise PoolRefusal(
                f"{request.method}: the shell could not start: {exc}", RpcErrorCode.FAILED
            ) from exc

    @rpc(RpcMethod.TERMINAL_INPUT, args=TerminalInputArgs, scope=RpcScope.IGNI)
    def input(self, args: TerminalInputArgs, request: msg.RPCRequest) -> TerminalAck:
        """Type into a terminal this window opened.

        ``data`` is sent to the shell as the keyboard sends it. What it
        prints back comes on ``terminal_output``. Not found when this window
        has no open terminal by that id.
        """
        self._own(args.terminal_id, request).write(args.data)
        return TerminalAck(terminal_id=args.terminal_id)

    @rpc(RpcMethod.TERMINAL_RESIZE, args=TerminalResizeArgs, scope=RpcScope.IGNI)
    def resize(self, args: TerminalResizeArgs, request: msg.RPCRequest) -> TerminalAck:
        """Resize a terminal this window opened.

        ``cols`` and ``rows`` are its new size (1 to 1000 each); the shell and
        the program in the foreground are told. Not found when this window
        has no open terminal by that id.
        """
        self._own(args.terminal_id, request).resize(args.cols, args.rows)
        return TerminalAck(terminal_id=args.terminal_id)

    @rpc(
        RpcMethod.TERMINAL_CLOSE,
        correlate=Correlation(by="payload.terminal_id", source="args.terminal_id"),
        duration=RpcDuration.IO,
        args=TerminalCloseArgs,
        scope=RpcScope.IGNI,
        streams=(PushChannel.TERMINAL_EXITED,),
    )
    async def close(self, args: TerminalCloseArgs, request: msg.RPCRequest) -> TerminalAck:
        """Close a terminal this window opened, ending its shell and what it started.

        The shell is hung up, and killed if it is still there a moment later.
        Its ``terminal_exited`` push goes out before the answer. Not found when
        this window has no open terminal by that id, or it has already ended.
        """
        await self._own(args.terminal_id, request).close()
        return TerminalAck(terminal_id=args.terminal_id)

    def _own(self, terminal_id: str, request: msg.RPCRequest) -> Terminal:
        """The asking window's terminal; another window's is not found, never touched."""
        try:
            return self._terminals.get(request.from_client, terminal_id)
        except LookupError as exc:
            raise PoolRefusal(f"{request.method}: {exc}", RpcErrorCode.NOT_FOUND) from exc


__all__ = ["TerminalRpc"]
