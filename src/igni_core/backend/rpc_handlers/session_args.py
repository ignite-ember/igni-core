"""The wire arguments of the session RPCs, validated before
:class:`~igni_core.backend.rpc_handlers.session.SessionRpcHandler` sees them."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SessionRpcArgs(BaseModel):
    """Base for every session RPC's arguments: keys a newer client adds
    are ignored rather than refused."""

    model_config = ConfigDict(extra="ignore")


class SessionIdArgs(SessionRpcArgs):
    """Arguments naming a session."""

    session_id: str = Field(description="Id of the session to act on.")


class InterruptedRunArgs(SessionIdArgs):
    """Arguments naming one interrupted run of a session."""

    message_id: str = Field(
        description=(
            "Id of the message that started the run, as listed by "
            "`get_interrupted_runs`; must not be empty."
        )
    )


class UploadAttachmentArgs(SessionRpcArgs):
    """Arguments for uploading a file to attach to a message."""

    filename: str = Field(
        description=(
            "The file's name. It is made safe for the disk; a name clash gets a `-N` suffix."
        )
    )
    content_base64: str = Field(description="The file's bytes, base64-encoded.")


class AgentRunArgs(SessionRpcArgs):
    """Arguments naming a sub-agent run to stop."""

    run_id: str = Field(
        default="",
        description="Id of the sub-agent run, as its events carry it; must not be empty.",
    )


class SearchChatArgs(SessionIdArgs):
    """Arguments for searching a session's conversation."""

    query: str = Field(description="Text to find, case-insensitive; blank finds nothing.")
    limit: int = Field(default=50, description="Maximum number of hits to return.")


class TruncateHistoryArgs(SessionIdArgs):
    """Arguments for dropping a run, and every later run, from a
    session's history."""

    run_id: str = Field(
        description=(
            "Id of the first run to drop: a top-level run, as `get_chat_history` carries it."
        )
    )
