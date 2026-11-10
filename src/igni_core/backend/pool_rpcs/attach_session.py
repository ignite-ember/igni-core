"""``attach_session``: a window binding to a session, and seeing what waits in it."""

from __future__ import annotations

import logging

from igni_core.backend.pool_replier import PoolReplier
from igni_core.backend.pool_rpcs.base import PoolRefusal, PoolRpc
from igni_core.backend.rpc_binding import Correlation, rpc
from igni_core.backend.rpc_streams import RUN_EVENTS, TOOL_PUSHES
from igni_core.backend.schemas_pool import AttachRefused, AttachSessionParams
from igni_core.backend.schemas_rpc import AttachSessionResult
from igni_core.backend.session_router import SessionRouter
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcDuration, RpcMethod, RpcScope

logger = logging.getLogger(__name__)


class AttachSessionRpc(PoolRpc):
    """Answers ``{session_id, project_dir}`` so the view can adopt the binding."""

    def __init__(self, sessions: SessionRouter) -> None:
        self._sessions = sessions

    @rpc(
        RpcMethod.ATTACH_SESSION,
        correlate=Correlation(by="id", source="request.id"),
        duration=RpcDuration.IO,
        args=AttachSessionParams,
        scope=RpcScope.SESSION,
        streams=RUN_EVENTS,
        may_stream=TOOL_PUSHES,
    )
    async def attach(
        self, args: AttachSessionParams, request: msg.RPCRequest
    ) -> AttachSessionResult:
        """Bind to a session — an existing one, or a new one in a project folder.

        ``session_id`` names the session to resume; ``project_dir`` the folder
        it works in. Neither: the default session. Only ``project_dir``: a new
        session locked to that folder. Both: that session, which must belong
        to that folder. The session is made live if it is not.

        Returns the ``session_id`` and ``project_dir`` actually bound; send
        later requests for that session with this ``session_id`` on the
        envelope. Right after the answer, every approval the session is
        waiting on is shown again (a ``run_paused`` carrying its
        ``hitl_request`` items), since a client that reloaded lost its
        dialog; a run igni was continuing when it stopped is reported with
        an ``info``, and a sub-agent's interrupted run is continued, which
        streams as any run does. Each of those messages carries this
        request's ``id``, and a ``stream_end`` of that ``id`` ends them.

        Fails when ``project_dir`` is not a directory, or the session belongs
        to another project folder — an attach never moves a session.
        """
        attached = await self._sessions.attach(args.session_id, args.project_dir)
        if isinstance(attached, AttachRefused):
            raise PoolRefusal(attached.reason)
        return attached

    async def handle(self, request: msg.RPCRequest, replier: PoolReplier) -> None:
        """Answered first; then the session's waiting approvals are shown again,
        since after a reload or a restart the window has lost its dialog."""
        answer = await self.answer(request)
        await replier.reply(request, answer)
        if isinstance(answer.result, AttachSessionResult):
            try:
                runtime = await self._sessions.pool.get_or_create(answer.result.session_id)
                await runtime.replay_waiting_approvals(request.id)
            except Exception as exc:
                # The attach was answered; what it shows again afterwards is
                # extra, and its failure is no reason to tell every window
                # routing broke.
                logger.error("replaying approvals after attach failed: %s", exc, exc_info=True)
