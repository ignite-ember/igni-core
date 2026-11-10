"""Chat history + search + truncation + context-token RPCs.

Split out of :class:`SessionRpcHandler` because the combined cluster
went past the 15-method-per-class ceiling. History concerns are a
natural sub-cluster: query the past + measure it + trim it.
"""

from __future__ import annotations

from igni_core.backend.rpc_binding import NoArgs, rpc
from igni_core.backend.rpc_handlers.base import RpcHandler
from igni_core.backend.rpc_handlers.session_args import (
    SearchChatArgs,
    SessionIdArgs,
    TruncateHistoryArgs,
)
from igni_core.backend.schemas_context import TruncateHistoryResult
from igni_core.backend.schemas_history import ChatSearchHit, ChatTurn
from igni_core.core.session.schemas import ContextBreakdown
from igni_core.protocol.rpc import RpcDuration, RpcMethod, RpcScope


class SessionHistoryRpcHandler(RpcHandler):
    """Chat log query + search + truncate + context-token count."""

    @rpc(
        RpcMethod.GET_CHAT_HISTORY,
        duration=RpcDuration.IO,
        args=SessionIdArgs,
        scope=RpcScope.SESSION,
    )
    async def get_chat_history(self, args: SessionIdArgs) -> list[ChatTurn]:
        """Read a session's conversation, turn by turn, as a client draws it.

        ``session_id`` names the session. Returns the conversation in
        order, one entry per item, told apart by ``role``: the person's
        message (``user``), the agent's answer (``assistant``), its
        reasoning (``thinking``), a tool call and its result (``tool``), a
        submitted plan (``plan``), a run's token counts (``stats``), and a
        view the agent drew (``visualization``). Sub-agents' runs are left
        out.
        """
        return await self._ctx.backend.get_chat_history(args.session_id)

    @rpc(
        RpcMethod.SEARCH_CHAT, duration=RpcDuration.IO, args=SearchChatArgs, scope=RpcScope.SESSION
    )
    async def search_chat(self, args: SearchChatArgs) -> list[ChatSearchHit]:
        """Find text in a session's conversation.

        ``session_id`` names the session; ``query`` is the text to find
        (case-insensitive; blank finds nothing); ``limit`` caps the hits
        (default 50). Returns one hit per match, in conversation order:
        its index in ``get_chat_history``, its role and run, a snippet
        around it, and where the match sits in the snippet.
        """
        return await self._ctx.backend.search_chat(args.session_id, args.query, args.limit)

    @rpc(
        RpcMethod.TRUNCATE_HISTORY,
        duration=RpcDuration.IO,
        args=TruncateHistoryArgs,
        scope=RpcScope.SESSION,
    )
    async def truncate_history(self, args: TruncateHistoryArgs) -> TruncateHistoryResult:
        """Drop a run from a session's history, with every run after it.

        ``session_id`` names the session; ``run_id`` the first run to drop
        (a top-level run, as ``get_chat_history`` carries it). Use it to
        edit and resend a message. Returns how many runs were ``removed``,
        or ``error`` when the session or the run is not found or the
        history could not be saved.
        """
        return await self._ctx.backend.truncate_history(args.session_id, args.run_id)

    @rpc(
        RpcMethod.COUNT_CONTEXT_TOKENS, duration=RpcDuration.IO, args=NoArgs, scope=RpcScope.SESSION
    )
    async def count_context_tokens(self, args: NoArgs) -> int:
        """Estimate how many tokens this session's conversation takes up.

        Takes no arguments. Returns the estimate (``0`` when it cannot be
        read); a positive estimate also becomes the context size
        ``get_status`` reports.
        """
        return await self._ctx.backend.count_context_tokens()

    @rpc(
        RpcMethod.GET_CONTEXT_BREAKDOWN,
        duration=RpcDuration.IO,
        args=NoArgs,
        scope=RpcScope.SESSION,
    )
    async def get_context_breakdown(self, args: NoArgs) -> ContextBreakdown:
        """Split this session's context into what compaction can shrink and what it cannot.

        Takes no arguments. Returns the ``total`` tokens, the part taken by
        the conversation's runs (``runs``, which ``/compact`` can shrink),
        and the ``floor`` — instructions, tools and the rest that stays.
        Zeros when it cannot be measured.
        """
        return await self._ctx.backend.context_breakdown()
