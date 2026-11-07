"""``host.chat``: what a session's chat holds, what an extension writes
into it, and the json-render views it shows there."""

from __future__ import annotations

import json
import uuid
from typing import Any

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.members.answers import (
    CompactResult,
    ContextUsage,
    ExtensionViewEvent,
)
from igni_core.extensions.host.members.base import Call, Members, call, get
from igni_core.extensions.host.members.rpc_args import (
    CompactArgs,
    DiscardInterruptedArgs,
    DispatchViewArgs,
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


class ChatMembersBase(Members):
    """Base: what more than one chat member reads."""

    async def _context_limit(self, c: Call) -> int:
        """The model's context window, in tokens; ``0`` when unknown."""
        status = await self.rpc(RpcMethod.GET_STATUS, c=c)
        return int(status.get("max_context") or 0)


class ChatReadMembers(ChatMembersBase):
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
        return SessionStatus.from_status(await self.rpc(RpcMethod.GET_STATUS, c=c), c.session_id)

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
        limit = await self._context_limit(c)
        return ContextUsage(tokens=int(tokens or 0), max=limit, breakdown=breakdown)

    @call("chat.search")
    async def search(self, c: Call) -> Any:
        args = SearchChatArgs(
            session_id=c.session_id, query=c.params["query"], limit=c.params.get("limit") or 50
        )
        return await self.rpc(RpcMethod.SEARCH_CHAT, args, c=c)


class ChatWriteMembers(ChatMembersBase):
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
        limit = await self._context_limit(c)
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


class ChatViewMembers(Members):
    """The json-render views in a chat: an extension's own, and acting on any."""

    @call("chat.append")
    async def append(self, c: Call) -> str:
        # A new card each time: two appends of the same view are two views.
        view_id = f"ext-{uuid.uuid4().hex[:12]}"
        await self.show(c, view_id, c.params["view"], str(c.params.get("title") or ""))
        self.host.state.view_owners.own(view_id, c.extension_id)
        return view_id

    @call("chat.replace")
    async def replace(self, c: Call) -> None:
        view_id = str(c.params["view_id"])
        owner = self.host.state.view_owners.owner_of(view_id)
        if owner is None:
            raise HostModelError(ErrorCode.NOT_FOUND, f"no view {view_id!r} was appended")
        if owner != c.extension_id:
            raise HostModelError(
                ErrorCode.REFUSED, f"view {view_id!r} belongs to another extension"
            )
        await self.show(c, view_id, c.params["view"], str(c.params.get("title") or ""))

    async def show(self, c: Call, view_id: str, spec: Any, title: str) -> None:
        if not isinstance(spec, dict) or not isinstance(spec.get("root"), str):
            raise HostModelError(
                ErrorCode.INVALID,
                "a view is a json-render spec: {root: <id>, elements: {<id>: {type, props, children}}}",
            )
        event = ExtensionViewEvent(
            agent_path=f"ext:{c.extension_id}",
            spec_id=view_id,
            spec_json=json.dumps(spec),
            final=True,
            title=title,
            source=c.extension_id,
        )
        await self.host.backend.show_view(
            session_id=c.session_id,
            payload=event.model_dump(by_alias=True),
            run_id=self.host.state.runs.last_run_of(c.session_id),
        )

    @get("chat.views.list")
    async def view_list(self, c: Call) -> Any:
        return self.host.backend.views(c.session_id)

    @call("chat.views.dispatch")
    async def dispatch(self, c: Call) -> Any:
        params = dict(c.params.get("params") or {})
        params["view_id"] = c.params["view_id"]
        args = DispatchViewArgs(action=c.params["action"], params=params)
        return await self.rpc(RpcMethod.DISPATCH_VISUALIZATION_ACTION, args, c=c)


__all__ = ["ChatMembersBase", "ChatReadMembers", "ChatViewMembers", "ChatWriteMembers"]
