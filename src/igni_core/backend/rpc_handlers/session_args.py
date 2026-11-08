"""The wire arguments of the session RPCs, validated before
:class:`~igni_core.backend.rpc_handlers.session.SessionRpcHandler` sees them."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SessionRpcArgs(BaseModel):
    """Base for every session RPC's arguments: keys a newer frontend adds
    are ignored rather than refused."""

    model_config = ConfigDict(extra="ignore")


class SessionIdArgs(SessionRpcArgs):
    """``get_pending_messages`` / ``get_interrupted_runs`` /
    ``switch_session``: the session to act on."""

    session_id: str


class InterruptedRunArgs(SessionIdArgs):
    """``discard_interrupted_run``: one interrupted run of one session.
    An empty ``message_id`` reaches the controller, which refuses it."""

    message_id: str


class UploadAttachmentArgs(SessionRpcArgs):
    """``upload_attachment``: the file's name and its base64 contents."""

    filename: str
    content_base64: str


class AgentRunArgs(SessionRpcArgs):
    """``cancel_agent_run``: the sub-agent run to stop (empty is refused
    by the controller)."""

    run_id: str = ""
