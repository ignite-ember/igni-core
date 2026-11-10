"""The chat handlers' arguments."""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_serializer

from igni_core.extensions.host.rpc_wire.base import RpcArgs


class SessionArgs(RpcArgs):
    session_id: str


class SearchChatArgs(RpcArgs):
    session_id: str
    query: str
    limit: int


class RunIdArgs(RpcArgs):
    run_id: str


class TruncateArgs(RpcArgs):
    session_id: str
    run_id: str


class CompactArgs(RpcArgs):
    ctx_tokens: int
    max_ctx: int


class LearningsArgs(RpcArgs):
    user_msg: str
    assistant_msg: str


class DiscardInterruptedArgs(RpcArgs):
    session_id: str
    message_id: str


class UploadArgs(RpcArgs):
    filename: str
    content_base64: str


class DispatchViewArgs(RpcArgs):
    """An action on one view. The handler reads the view's id inside
    ``params``; the one named here wins over one the caller put there."""

    action: str
    params: dict[str, Any]
    view_id: str = Field(exclude=True)

    @field_serializer("params")
    def _with_view_id(self, params: dict[str, Any]) -> dict[str, Any]:
        return {**params, "view_id": self.view_id}


__all__ = [
    "CompactArgs",
    "DiscardInterruptedArgs",
    "DispatchViewArgs",
    "LearningsArgs",
    "RunIdArgs",
    "SearchChatArgs",
    "SessionArgs",
    "TruncateArgs",
    "UploadArgs",
]
