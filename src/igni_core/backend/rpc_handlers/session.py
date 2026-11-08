"""Session lifecycle + status RPC handlers.

Handles everything about "which session am I on, what is it doing,
stop it, switch it" — the history / search / truncate cluster lives
in the sibling :mod:`.session_history` module to keep each class
under the 15-method ceiling.
"""

from __future__ import annotations

from igni_core.backend.rpc_handlers.base import RpcHandler, WireArgs, rpc
from igni_core.backend.rpc_handlers.session_args import (
    AgentRunArgs,
    InterruptedRunArgs,
    SessionIdArgs,
    UploadAttachmentArgs,
)
from igni_core.backend.schemas_context import (
    DiscardInterruptedRunResult,
    InterruptedRun,
    PendingMessage,
)
from igni_core.backend.schemas_run import CancelAgentRunResult
from igni_core.backend.server_files import UploadAttachmentResult
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod


class SessionRpcHandler(RpcHandler):
    """Session lifecycle + status + agent-run control + hook fires."""

    @rpc(RpcMethod.SHUTDOWN)
    async def shutdown(self, args: WireArgs) -> None:
        await self._ctx.backend.shutdown()

    @rpc(RpcMethod.UPLOAD_ATTACHMENT, args=UploadAttachmentArgs)
    def upload_attachment(self, args: UploadAttachmentArgs) -> UploadAttachmentResult:
        return self._controllers.files.upload_attachment(args.filename, args.content_base64)

    @rpc(RpcMethod.GET_PENDING_MESSAGES, args=SessionIdArgs)
    async def get_pending_messages(self, args: SessionIdArgs) -> list[PendingMessage]:
        return await self._ctx.backend.get_pending_messages(args.session_id)

    @rpc(RpcMethod.GET_INTERRUPTED_RUNS, args=SessionIdArgs)
    async def get_interrupted_runs(self, args: SessionIdArgs) -> list[InterruptedRun]:
        # Surface the explicit interrupted-run records the FE uses
        # to render Retry / Discard / Edit-prompt banners. Distinct
        # from ``get_pending_messages`` — see the controller docs.
        return await self._ctx.backend.get_interrupted_runs(args.session_id)

    @rpc(RpcMethod.DISCARD_INTERRUPTED_RUN, args=InterruptedRunArgs)
    async def discard_interrupted_run(
        self, args: InterruptedRunArgs
    ) -> DiscardInterruptedRunResult:
        # Hard-delete a single interrupted record. Idempotent on
        # missing rows (the controller returns ``ok=True`` either
        # way) so the FE can retry on transient errors without
        # worrying about double-delete.
        return await self._controllers.context.discard_interrupted_run(
            args.session_id, args.message_id
        )

    @rpc(RpcMethod.LIST_SESSIONS)
    async def list_sessions(self, args: WireArgs) -> msg.SessionListResult:
        return await self._ctx.backend.list_sessions()

    @rpc(RpcMethod.SWITCH_SESSION, args=SessionIdArgs)
    async def switch_session(self, args: SessionIdArgs) -> msg.Info:
        return await self._ctx.backend.switch_session(args.session_id)

    @rpc(RpcMethod.GET_PROCESSING)
    def get_processing(self, args: WireArgs) -> bool:
        return self._ctx.backend.processing

    @rpc(RpcMethod.GET_SESSION_ID)
    def get_session_id(self, args: WireArgs) -> str:
        return self._ctx.backend.session_id

    @rpc(RpcMethod.GET_RUN_TIMEOUT)
    def get_run_timeout(self, args: WireArgs) -> int:
        return self._ctx.backend.run_timeout

    @rpc(RpcMethod.GET_STATUS)
    def get_status(self, args: WireArgs) -> msg.StatusUpdate:
        return self._ctx.backend.get_status()

    @rpc(RpcMethod.CANCEL_RUN)
    def cancel_run(self, args: WireArgs) -> None:
        self._ctx.backend.cancel_run()

    @rpc(RpcMethod.CANCEL_AGENT_RUN, args=AgentRunArgs)
    def cancel_agent_run(self, args: AgentRunArgs) -> CancelAgentRunResult:
        return self._controllers.runs.cancel_agent_run(args.run_id)

    @rpc(RpcMethod.FIRE_SESSION_START_HOOK)
    async def fire_session_start_hook(self, args: WireArgs) -> None:
        await self._ctx.backend.fire_session_start_hook()

    @rpc(RpcMethod.GET_PROJECT_DIR)
    def get_project_dir(self, args: WireArgs) -> str:
        return str(self._ctx.backend.project_dir)
