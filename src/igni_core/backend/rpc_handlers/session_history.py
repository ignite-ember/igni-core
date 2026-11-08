"""Chat history + search + truncation + context-token RPCs.

Split out of :class:`SessionRpcHandler` because the combined cluster
went past the 15-method-per-class ceiling. History concerns are a
natural sub-cluster: query the past + measure it + trim it.
"""

from __future__ import annotations

from igni_core.backend.rpc_handlers.base import RpcHandler, WireArgs, rpc
from igni_core.backend.rpc_handlers.session_args import (
    SearchChatArgs,
    SessionIdArgs,
    TruncateHistoryArgs,
)
from igni_core.backend.schemas_context import TruncateHistoryResult
from igni_core.backend.schemas_history import ChatSearchHit, ChatTurn
from igni_core.core.session.schemas import ContextBreakdown
from igni_core.protocol.rpc import RpcMethod


class SessionHistoryRpcHandler(RpcHandler):
    """Chat log query + search + truncate + context-token count."""

    @rpc(RpcMethod.GET_CHAT_HISTORY, args=SessionIdArgs)
    async def get_chat_history(self, args: SessionIdArgs) -> list[ChatTurn]:
        return await self._ctx.backend.get_chat_history(args.session_id)

    @rpc(RpcMethod.SEARCH_CHAT, args=SearchChatArgs)
    async def search_chat(self, args: SearchChatArgs) -> list[ChatSearchHit]:
        return await self._ctx.backend.search_chat(args.session_id, args.query, args.limit)

    @rpc(RpcMethod.TRUNCATE_HISTORY, args=TruncateHistoryArgs)
    async def truncate_history(self, args: TruncateHistoryArgs) -> TruncateHistoryResult:
        return await self._ctx.backend.truncate_history(args.session_id, args.run_id)

    @rpc(RpcMethod.COUNT_CONTEXT_TOKENS)
    async def count_context_tokens(self, args: WireArgs) -> int:
        return await self._ctx.backend.count_context_tokens()

    @rpc(RpcMethod.GET_CONTEXT_BREAKDOWN)
    async def get_context_breakdown(self, args: WireArgs) -> ContextBreakdown:
        return await self._ctx.backend.context_breakdown()
