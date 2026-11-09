"""Which :class:`~igni_core.protocol.schemas.rpc.RpcErrorCode` a failed call answers with.

One table for every route that answers an RPC: a session's handler
through the dispatcher, and the pool's own calls.
"""

from __future__ import annotations

from igni_core.backend.rpc_binding import RpcArgsError
from igni_core.backend.run_controller import RunCancelledError
from igni_core.protocol.schemas.rpc import RpcErrorCode


def code_of(exc: BaseException) -> RpcErrorCode:
    """The code for a call that raised ``exc``.

    A handler refuses with ``ValueError`` (the house rule); its arguments
    not fitting is :class:`RpcArgsError`, a ``ValueError`` too, so it is
    tested first.
    """
    if isinstance(exc, RpcArgsError | KeyError):
        return RpcErrorCode.INVALID
    if isinstance(exc, RunCancelledError):
        return RpcErrorCode.CANCELLED
    if isinstance(exc, FileNotFoundError | LookupError):
        return RpcErrorCode.NOT_FOUND
    if isinstance(exc, ValueError | PermissionError):
        return RpcErrorCode.REFUSED
    return RpcErrorCode.FAILED


__all__ = ["code_of"]
