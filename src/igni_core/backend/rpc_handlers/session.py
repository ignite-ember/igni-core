"""Session lifecycle + status RPC handlers.

Handles everything about "which session am I on, what is it doing,
stop it, switch it" — the history / search / truncate cluster lives
in the sibling :mod:`.session_history` module to keep each class
under the 15-method ceiling.
"""

from __future__ import annotations

from igni_core.backend.rpc_binding import NoArgs, rpc
from igni_core.backend.rpc_handlers.base import RpcHandler
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
from igni_core.protocol.rpc import RpcMethod, RpcScope


class SessionRpcHandler(RpcHandler):
    """Session lifecycle + status + agent-run control + hook fires."""

    @rpc(RpcMethod.SHUTDOWN, args=NoArgs, scope=RpcScope.SESSION)
    async def shutdown(self, args: NoArgs) -> None:
        """Wind this session down: its hooks, its agents, its MCP servers, its scheduler.

        Takes no arguments and returns nothing once done. Runs the session's
        ``SessionEnd`` hooks, cleans up its ephemeral agents, disconnects its
        MCP servers and stops its scheduler — once: a session already wound
        down is left as it is. Background processes are igni's, not the
        session's, and are left running; they end when igni does. It does
        not stop igni or close the connection. Each step's failure is
        logged, not raised. Only igni's window may call it.
        """
        await self._ctx.backend.shutdown()

    @rpc(RpcMethod.UPLOAD_ATTACHMENT, args=UploadAttachmentArgs, scope=RpcScope.SESSION)
    def upload_attachment(self, args: UploadAttachmentArgs) -> UploadAttachmentResult:
        """Store a file for this session, so a message can refer to it.

        ``filename`` is the file's name (made safe for the disk; a clash gets
        a ``-N`` suffix); ``content_base64`` its bytes, base64-encoded.
        Returns ``path`` — the stored file's path, or an ``attachment:<id>``
        reference when attachments are kept in shared storage — and its
        ``size`` in bytes; or ``error`` when the content is not valid base64.
        Fails when the file cannot be stored.
        """
        return self._controllers.files.upload_attachment(args.filename, args.content_base64)

    @rpc(RpcMethod.GET_PENDING_MESSAGES, args=SessionIdArgs, scope=RpcScope.SESSION)
    async def get_pending_messages(self, args: SessionIdArgs) -> list[PendingMessage]:
        """List a session's messages that were received but never answered.

        ``session_id`` names the session. Returns the user messages still
        pending at least a minute after they arrived — typically left by a
        crash — oldest first, each with its text, when it arrived and its
        ``message_id``. A storage failure answers an empty list.
        """
        return await self._ctx.backend.get_pending_messages(args.session_id)

    @rpc(RpcMethod.GET_INTERRUPTED_RUNS, args=SessionIdArgs, scope=RpcScope.SESSION)
    async def get_interrupted_runs(self, args: SessionIdArgs) -> list[InterruptedRun]:
        """List a session's runs that stopped before they finished.

        ``session_id`` names the session. Returns one row per run that was
        cancelled or failed: the ``message_id`` and text of the message that
        started it, when it arrived and when it stopped, why (``cancelled``
        or ``errored``) and the last error. A client offers to retry,
        discard or edit each. A storage failure answers an empty list.
        """
        # Distinct from ``get_pending_messages`` — see the controller docs.
        return await self._ctx.backend.get_interrupted_runs(args.session_id)

    @rpc(RpcMethod.DISCARD_INTERRUPTED_RUN, args=InterruptedRunArgs, scope=RpcScope.SESSION)
    async def discard_interrupted_run(
        self, args: InterruptedRunArgs
    ) -> DiscardInterruptedRunResult:
        """Forget one interrupted run, so it is not offered again.

        ``message_id`` names the run (from ``get_interrupted_runs``);
        ``session_id`` the session it belongs to. Returns ``ok: true`` —
        also when no such run exists, so a retry is harmless — or ``ok:
        false`` with ``error`` when ``message_id`` is empty.
        """
        return await self._controllers.context.discard_interrupted_run(
            args.session_id, args.message_id
        )

    @rpc(RpcMethod.LIST_SESSIONS, args=NoArgs, scope=RpcScope.SESSION)
    async def list_sessions(self, args: NoArgs) -> msg.SessionListResult:
        """List the sessions kept in this session's project.

        Takes no arguments. Returns a ``session_list_result`` message, sent
        in place of an ``rpc_response``: each session's id, name, when it
        was created and last updated, how many runs it has, a summary and
        the agent it runs. A storage failure answers an empty list.
        """
        return await self._ctx.backend.list_sessions()

    @rpc(RpcMethod.SWITCH_SESSION, args=SessionIdArgs, scope=RpcScope.SESSION)
    async def switch_session(self, args: SessionIdArgs) -> msg.Info:
        """Make the session this request is routed to continue another stored session.

        ``session_id`` is the stored session to continue; the conversation,
        its history and its runs are read from it from now on. Returns an
        ``info`` message, sent in place of an ``rpc_response``, naming the
        session switched to. The id is not checked: an unknown one starts
        an empty session under that id.
        """
        return await self._ctx.backend.switch_session(args.session_id)

    @rpc(RpcMethod.GET_PROCESSING, args=NoArgs, scope=RpcScope.SESSION)
    def get_processing(self, args: NoArgs) -> bool:
        """Say whether this session is running a turn right now.

        Takes no arguments. Returns ``true`` while a turn the person (or a
        client) started is starting, streaming or finishing.
        """
        return self._ctx.backend.processing

    @rpc(RpcMethod.GET_SESSION_ID, args=NoArgs, scope=RpcScope.SESSION)
    def get_session_id(self, args: NoArgs) -> str:
        """Read the id of the session this request reached.

        Takes no arguments. Returns the session's current id, which can
        differ from the one the request was routed by after ``/clear`` or
        ``switch_session``.
        """
        return self._ctx.backend.session_id

    @rpc(RpcMethod.GET_RUN_TIMEOUT, args=NoArgs, scope=RpcScope.SESSION)
    def get_run_timeout(self, args: NoArgs) -> int:
        """Read how long, in seconds, a client should wait for one run.

        Takes no arguments. Returns the configured maximum run time; igni
        does not stop a run at it, the client decides what to do.
        """
        return self._ctx.backend.run_timeout

    @rpc(RpcMethod.GET_STATUS, args=NoArgs, scope=RpcScope.SESSION)
    def get_status(self, args: NoArgs) -> msg.StatusUpdate:
        """Read this session's status: model, account, context size and permission mode.

        Takes no arguments. Returns a ``status_update`` message, sent in
        place of an ``rpc_response``: the model and whether it is
        configured, the igni server connection and account, the context
        used and its window, the permission mode and the theme.
        """
        return self._ctx.backend.get_status()

    @rpc(
        RpcMethod.CANCEL_RUN,
        args=NoArgs,
        scope=RpcScope.SESSION,
        streams=(msg.Info, msg.StreamEnd),
    )
    def cancel_run(self, args: NoArgs) -> None:
        """Stop the turn this session is running.

        Takes no arguments and returns nothing; with no turn running it does
        nothing. The turn's stream ends with an ``info`` saying it was
        cancelled and its ``stream_end``, and the run is recorded as
        interrupted. A foreground shell command the turn started is killed.
        """
        self._ctx.backend.cancel_run()

    @rpc(RpcMethod.CANCEL_AGENT_RUN, args=AgentRunArgs, scope=RpcScope.IGNI)
    def cancel_agent_run(self, args: AgentRunArgs) -> CancelAgentRunResult:
        """Stop one sub-agent's run, leaving the rest of the turn going.

        ``run_id`` is the sub-agent run's id, as its events carry it.
        Returns ``ok: true`` once the cancellation is asked for — an unknown
        id is not detected — or ``ok: false`` with ``error`` when
        ``run_id`` is empty or the cancellation failed.
        """
        return self._controllers.runs.cancel_agent_run(args.run_id)

    @rpc(RpcMethod.FIRE_SESSION_START_HOOK, args=NoArgs, scope=RpcScope.SESSION)
    async def fire_session_start_hook(self, args: NoArgs) -> None:
        """Run this session's ``SessionStart`` hooks.

        Takes no arguments and returns nothing. The organisation's group
        policy is checked again first. Every call runs the hooks again;
        their failures are logged, not raised.
        """
        await self._ctx.backend.fire_session_start_hook()

    @rpc(RpcMethod.GET_PROJECT_DIR, args=NoArgs, scope=RpcScope.SESSION)
    def get_project_dir(self, args: NoArgs) -> str:
        """Read the folder this session is locked to.

        Takes no arguments. Returns the absolute path the agent's tools and
        shell commands work in.
        """
        return str(self._ctx.backend.project_dir)
