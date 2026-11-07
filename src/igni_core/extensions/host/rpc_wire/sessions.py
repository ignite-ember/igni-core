"""The session handlers' arguments."""

from __future__ import annotations

from igni_core.extensions.host.rpc_wire.base import RpcArgs


class SwitchSessionArgs(RpcArgs):
    session_id: str


__all__ = ["SwitchSessionArgs"]
