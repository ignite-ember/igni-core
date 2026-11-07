"""One RPC that succeeded — what the gateway tells and the broadcasts translate.

It sits beside the gateway rather than in :mod:`.broadcasts` because both
ends use it: :class:`~.backend_gateway.HostBackendGateway` and the backend's
``RpcWatch`` make one, and
:class:`~.broadcasts.rpc_writes.RpcWriteSignals` turns it into a signal.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from igni_core.protocol.rpc import RpcMethod


class RpcWrite(BaseModel):
    """One RPC that succeeded: which, with what, in which session."""

    method: RpcMethod
    #: The RPC's arguments: each method takes its own.
    args: dict[str, Any]
    session_id: str

    @property
    def subject(self) -> Any:
        """What the write was to — a server, a plugin, an agent — as the caller named it."""
        return self.args.get("name") or self.args.get("server_name")

    def text(self, key: str) -> str:
        return str(self.args.get(key, ""))


__all__ = ["RpcWrite"]
