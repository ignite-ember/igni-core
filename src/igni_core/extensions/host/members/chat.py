"""``host.chat``: what a session's chat holds, and what an extension
writes into it. The views it shows there are :mod:`.chat_views`'."""

from __future__ import annotations

from typing import Any

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.members.answers import CompactResult, ContextUsage
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.tag import call, get
from igni_core.extensions.host.rpc_wire.chat import (
    CompactArgs,
    DiscardInterruptedArgs,
    LearningsArgs,
    RunIdArgs,
    SearchChatArgs,
    SessionArgs,
    TruncateArgs,
    UploadArgs,
)
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.values import SessionStatus
from igni_core.protocol.rpc import RpcMethod


class ChatReadMembers(Members):
    @get("chat.session_id")
    async def session_id(self, c: Call) -> str:
        return c.session_id

    @get("chat.messages")
    async def messages(self, c: Call) -> Any:
        return await self.rpc(RpcMethod.GET_CHAT_HISTORY, SessionArgs(session_id=c.session_id), c=c)

    @get("chat.pending")
    async def pending(self, c: Call) -> Any:
        return await self.rpc(
            RpcMethod.GET_PENDING_MESSAGES, SessionArgs(session_id=c.session_id), c=c
        )

    @get("chat.processing")
    async def processing(self, c: Call) -> bool:
        return bool(await self.rpc(RpcMethod.GET_PROCESSING, c=c))

    @get("chat.status")
    async def status(self, c: Call) -> SessionStatus:
        return (await self.read_status(c)).session_status(c.session_id)

    @get("chat.timeout")
    async def timeout(self, c: Call) -> int:
        return int(await self.rpc(RpcMethod.GET_RUN_TIMEOUT, c=c))

    @get("chat.interrupted_runs")
    async def interrupted_runs(self, c: Call) -> Any:
        return await self.rpc(
            RpcMethod.GET_INTERRUPTED_RUNS, SessionArgs(session_id=c.session_id), c=c
        )

    @get("chat.context")
    async def context(self, c: Call) -> ContextUsage:
        tokens = await self.rpc(RpcMethod.COUNT_CONTEXT_TOKENS, c=c)
        breakdown = await self.rpc(RpcMethod.GET_CONTEXT_BREAKDOWN, c=c)
        limit = (await self.read_status(c)).max_context
        return ContextUsage(tokens=int(tokens or 0), max=limit, breakdown=breakdown)

    @call("chat.search")
    async def search(self, c: Call) -> Any:
        args = SearchChatArgs(
            session_id=c.session_id, query=c.params["query"], limit=c.params.get("limit") or 50
        )
        return await self.rpc(RpcMethod.SEARCH_CHAT, args, c=c)


class ChatWriteMembers(Members):
    @call("chat.post")
    async def post(self, c: Call) -> None:
        text = str(c.params["text"])
        if not text.strip():
            raise HostModelError(ErrorCode.INVALID, "an empty message is not posted")
        if not c.on_screen:
            # Nobody may be watching: a service event's rules — start a
            # run, or wait for the one going; never answer an approval.
            posted = await self.host.backend.deliver(
                session_id=c.session_id,
                extension_id=c.extension_id,
                text=text,
                media=c.params.get("attachments") or None,
            )
            if not posted.ok:
                raise HostModelError(ErrorCode.REFUSED, posted.reason)
            return
        await self.host.backend.post(
            session_id=c.session_id,
            extension_id=c.extension_id,
            text=text,
            media=c.params.get("attachments") or None,
            queued=False,
        )

    @call("chat.queue")
    async def queue(self, c: Call) -> None:
        text = str(c.params["text"])
        if not text.strip():
            raise HostModelError(ErrorCode.INVALID, "an empty message is not queued")
        await self.host.backend.post(
            session_id=c.session_id,
            extension_id=c.extension_id,
            text=text,
            media=None,
            queued=True,
        )

    @call("chat.cancel")
    async def cancel(self, c: Call) -> None:
        await self.rpc(RpcMethod.CANCEL_RUN, c=c)

    @call("chat.cancel_agent")
    async def cancel_agent(self, c: Call) -> None:
        await self.rpc(RpcMethod.CANCEL_AGENT_RUN, RunIdArgs(run_id=c.params["run_id"]), c=c)

    @call("chat.truncate")
    async def truncate(self, c: Call) -> Any:
        args = TruncateArgs(session_id=c.session_id, run_id=c.params["run_id"])
        return await self.rpc(RpcMethod.TRUNCATE_HISTORY, args, c=c)

    @call("chat.compact")
    async def compact(self, c: Call) -> CompactResult | None:
        tokens = int(await self.rpc(RpcMethod.COUNT_CONTEXT_TOKENS, c=c) or 0)
        limit = (await self.read_status(c)).max_context
        cleared = await self.rpc(
            RpcMethod.COMPACT_IF_NEEDED, CompactArgs(ctx_tokens=tokens, max_ctx=limit), c=c
        )
        return CompactResult.from_rpc(c.session_id, cleared)

    @call("chat.extract_learnings")
    async def extract_learnings(self, c: Call) -> None:
        args = LearningsArgs(user_msg=c.params["user_msg"], assistant_msg=c.params["assistant_msg"])
        await self.rpc(RpcMethod.EXTRACT_LEARNINGS, args, c=c)

    @call("chat.discard_interrupted")
    async def discard_interrupted(self, c: Call) -> Any:
        args = DiscardInterruptedArgs(session_id=c.session_id, message_id=c.params["message_id"])
        return await self.rpc(RpcMethod.DISCARD_INTERRUPTED_RUN, args, c=c)

    @call("chat.upload")
    async def upload(self, c: Call) -> Any:
        args = UploadArgs(filename=c.params["filename"], content_base64=c.params["content_base64"])
        return await self.rpc(RpcMethod.UPLOAD_ATTACHMENT, args, c=c)


__all__ = ["ChatReadMembers", "ChatWriteMembers"]
