"""The workspace handlers' arguments."""

from __future__ import annotations

from igni_core.extensions.host.rpc_wire.base import RpcArgs


class PathArgs(RpcArgs):
    path: str


class CompleteArgs(RpcArgs):
    query: str
    limit: int


class QueryArgs(RpcArgs):
    query: str


__all__ = ["CompleteArgs", "PathArgs", "QueryArgs"]
