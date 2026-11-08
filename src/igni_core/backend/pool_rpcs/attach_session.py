"""``attach_session``: a window binding to a session, and seeing what waits in it."""

from __future__ import annotations

from igni_core.backend.pool_replier import PoolReplier
from igni_core.backend.pool_rpcs.base import PoolRpc
from igni_core.backend.schemas_pool import AttachRefused, AttachSessionParams, PoolAnswer
from igni_core.backend.schemas_rpc import AttachSessionResult
from igni_core.backend.session_router import SessionRouter
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod


class AttachSessionRpc(PoolRpc):
    """Answers ``{session_id, project_dir}`` so the view can adopt the binding."""

    methods = frozenset({RpcMethod.ATTACH_SESSION})

    def __init__(self, sessions: SessionRouter) -> None:
        self._sessions = sessions

    async def answer(self, request: msg.RPCRequest) -> PoolAnswer:
        params = AttachSessionParams.model_validate(request.args)
        attached = await self._sessions.attach(params.session_id, params.project_dir)
        if isinstance(attached, AttachRefused):
            return PoolAnswer.failed(attached.reason)
        return PoolAnswer.ok(attached)

    async def handle(self, request: msg.RPCRequest, replier: PoolReplier) -> None:
        """Answered first; then the session's waiting approvals are shown again,
        since after a reload or a restart the window has lost its dialog."""
        answer = await self.answer(request)
        await replier.reply(request, answer)
        if isinstance(answer.result, AttachSessionResult):
            runtime = await self._sessions.pool.get_or_create(answer.result.session_id)
            await runtime.replay_waiting_approvals()
