"""Messages a client sends igni on the person's behalf.

Every previously-free-string field that names a closed set of
values is retyped to its :mod:`.enums` StrEnum member. Wire
strings stay unchanged; producers on the FE side that send raw
JSON like ``{"action": "confirm"}`` still deserialize correctly
because Pydantic accepts either the enum member or its underlying
string.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from igni_core.protocol.schemas.envelope import Direction, Message

# HITLAction / HITLChoice are referenced by docstrings only —
# the wire uses raw ``str`` for forward-compat. Producers import
# the enums directly from :mod:`.enums` for typed dispatch.


class UserMessage(Message):
    """The person sends a chat message, starting a run."""

    type: Literal["user_message"] = "user_message"
    direction: ClassVar[Direction] = Direction.TO_IGNI
    text: str = Field(default="", description="The message.")
    file_contents: dict[str, str] = Field(
        default_factory=dict,
        description="Files attached to the message: their contents, keyed by path.",
    )
    client_id: str = Field(
        default="",
        description=(
            "This client's id, from `welcome`. igni echoes the message to every other client "
            "attached to the session (`user_message_received`); the sender recognises its own "
            "by this id. Empty is allowed."
        ),
    )
    force: bool = Field(
        default=False,
        description=(
            "True to discard any interrupted or pending runs of the session first, then start "
            "afresh with this message; for retrying after an interruption."
        ),
    )


class QueueMessage(Message):
    """A message the person sends while a run is still going; it is queued for the agent."""

    type: Literal["queue_message"] = "queue_message"
    direction: ClassVar[Direction] = Direction.TO_IGNI
    text: str = Field(default="", description="The message.")
    client_id: str = Field(
        default="",
        description="This client's id, from `welcome`, so it can recognise its own echo. Empty is allowed.",
    )


#: The person's answer to an approval request: run the tool call, or not.
HitlActionValue = Literal["confirm", "reject"]

#: How long the answer stands. ``once`` (or empty): this call only.
#: ``always``: save a rule allowing this exact call. ``similar``: save a
#: rule allowing calls like it. ``deny``: save a rule refusing it.
HitlChoiceValue = Literal["", "once", "always", "similar", "deny"]

#: A permission mode to switch to before the run resumes; empty for none.
HitlModeValue = Literal["", "default", "plan", "acceptEdits", "bypassPermissions"]


class HITLResponse(Message):
    """The person's answer to one approval request (from ``run_paused``)."""

    type: Literal["hitl_response"] = "hitl_response"
    direction: ClassVar[Direction] = Direction.TO_IGNI
    requirement_id: str = Field(
        default="", description="The approval request's id, from its `hitl_request`."
    )
    action: HitlActionValue = Field(
        default="reject", description="`confirm` to run the tool call, `reject` to refuse it."
    )
    choice: HitlChoiceValue = Field(
        default="",
        description=(
            "How long the answer stands. `once` (or empty): this call only. `always`: save a "
            "rule allowing this exact call. `similar`: save a rule allowing calls like it. "
            "`deny`: save a rule refusing it."
        ),
    )


class HITLDecision(Message):
    """One answer inside a ``hitl_response_batch``: as ``hitl_response``,
    plus a permission mode to switch to before the run resumes
    (``set_permission_mode``), so the very next tool call is checked
    under it."""

    type: Literal["hitl_decision"] = "hitl_decision"
    direction: ClassVar[Direction] = Direction.TO_IGNI
    requirement_id: str = Field(
        default="", description="The approval request's id, from its `hitl_request`."
    )
    action: HitlActionValue = Field(
        default="reject", description="`confirm` to run the tool call, `reject` to refuse it."
    )
    choice: HitlChoiceValue = Field(
        default="",
        description=(
            "How long the answer stands. `once` (or empty): this call only. `always`: save a "
            "rule allowing this exact call. `similar`: save a rule allowing calls like it. "
            "`deny`: save a rule refusing it."
        ),
    )
    set_permission_mode: HitlModeValue = Field(
        default="",
        description="A permission mode to switch to before the run resumes; empty to keep the current one.",
    )


class HITLResponseBatch(Message):
    """The person's answers to every approval request of one pause, sent
    together: a request left out is taken as rejected."""

    type: Literal["hitl_response_batch"] = "hitl_response_batch"
    direction: ClassVar[Direction] = Direction.TO_IGNI
    decisions: list[HITLDecision] = Field(
        default=[], description="One answer per approval request."
    )


class Command(Message):
    """A slash command the person typed. igni answers with a ``command_result``."""

    type: Literal["command"] = "command"
    direction: ClassVar[Direction] = Direction.TO_IGNI
    text: str = Field(default="", description="The command as typed, slash included, e.g. `/help`.")


class Cancel(Message):
    """Stop the run in progress."""

    type: Literal["cancel"] = "cancel"
    direction: ClassVar[Direction] = Direction.TO_IGNI


class CancelLogin(Message):
    """Cancel an in-progress login flow."""

    type: Literal["cancel_login"] = "cancel_login"
    direction: ClassVar[Direction] = Direction.TO_IGNI


class SessionSwitch(Message):
    """Switch to a different saved session."""

    type: Literal["session_switch"] = "session_switch"
    direction: ClassVar[Direction] = Direction.TO_IGNI
    session_id: str = Field(default="", description="The id of the session to switch to.")


class SessionList(Message):
    """Ask for the saved sessions. igni answers with a ``session_list_result``."""

    type: Literal["session_list"] = "session_list"
    direction: ClassVar[Direction] = Direction.TO_IGNI


class ModelSwitch(Message):
    """Switch the session to a different model."""

    type: Literal["model_switch"] = "model_switch"
    direction: ClassVar[Direction] = Direction.TO_IGNI
    model_name: str = Field(default="", description="The id of the model to switch to.")


class MCPToggle(Message):
    """Connect or disconnect one of the configured MCP servers."""

    type: Literal["mcp_toggle"] = "mcp_toggle"
    direction: ClassVar[Direction] = Direction.TO_IGNI
    server_name: str = Field(default="", description="The MCP server's name, as configured.")
    connect: bool = Field(default=True, description="True to connect, false to disconnect.")


class Shutdown(Message):
    """Shut igni down cleanly."""

    type: Literal["shutdown"] = "shutdown"
    direction: ClassVar[Direction] = Direction.TO_IGNI


class StreamEnd(Message):
    """Marks the end of a streamed answer, such as a run's events."""

    type: Literal["stream_end"] = "stream_end"
    direction: ClassVar[Direction] = Direction.TO_CLIENT


__all__ = [
    "UserMessage",
    "QueueMessage",
    "HITLResponse",
    "HITLDecision",
    "HITLResponseBatch",
    "Command",
    "Cancel",
    "CancelLogin",
    "SessionSwitch",
    "SessionList",
    "ModelSwitch",
    "MCPToggle",
    "Shutdown",
    "StreamEnd",
]
