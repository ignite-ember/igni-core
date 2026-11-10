"""Multi-client session mirroring events.

One BE session, N attached views (web tabs, IDE webviews, the TUI).
Every state change is an event so all views render identically.
These messages are BE→FE *broadcast* semantics — one session,
every attached view receives the same event — distinct from the
per-run BE→FE events in :mod:`.be_events`.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from igni_core.protocol.schemas.envelope import Direction, Message


class Welcome(Message):
    """The first message igni sends a newly attached client."""

    type: Literal["welcome"] = "welcome"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    client_id: str = Field(
        default="",
        description=(
            "The id igni gave this client. Send it with your messages; messages igni echoes "
            "to every client carry it, so you can skip your own."
        ),
    )


class Typing(Message):
    """What the person is typing in one client, shared with every other client of the session.

    Each message carries the whole draft, not a change to it. Send it at
    a modest rate while the person types.
    """

    type: Literal["typing"] = "typing"
    direction: ClassVar[Direction] = Direction.BOTH
    text: str = Field(
        default="", description="The whole draft so far; empty when it was sent or cleared."
    )
    client_id: str = Field(default="", description="The id of the client the person is typing in.")


class UserMessageReceived(Message):
    """A chat message igni accepted, sent to every client of the session so all show it.

    The client that sent it can recognise it by ``client_id`` and skip it.
    """

    type: Literal["user_message_received"] = "user_message_received"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    text: str = Field(default="", description="The message.")
    client_id: str = Field(
        default="", description="The id of the client that sent it; empty when no client did."
    )
    queued: bool = Field(
        default=False, description="True when it was sent during a run and is queued for the agent."
    )
    extension_id: str = Field(
        default="",
        description=(
            "Set when an extension posted the message (`host.chat.post`): show it as that "
            "extension's, not the person's. Empty otherwise."
        ),
    )
    # docs/CLOUD.md §6.1: a message item from the extension's service.
    service_client_id: str = Field(
        default="",
        description=(
            "Set when the extension's service sent the message: that service's client id. "
            "`extension_id` is set too. Empty otherwise."
        ),
    )


class RequirementResolved(Message):
    """An approval request was answered, in any client; close its dialog if you show one."""

    type: Literal["requirement_resolved"] = "requirement_resolved"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    requirement_id: str = Field(
        default="", description="The id of the approval request that was answered."
    )


class RequirementDetails(Message):
    """The reason for an open approval request, sent after the request itself.

    A request sent with ``details_pending`` set gets its ``details``
    later in this message; show it in place of the placeholder.
    """

    type: Literal["requirement_details"] = "requirement_details"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    requirement_id: str = Field(default="", description="The id of the approval request.")
    details: str = Field(default="", description="Why approval is being asked, for the person.")


__all__ = [
    "Welcome",
    "Typing",
    "UserMessageReceived",
    "RequirementResolved",
    "RequirementDetails",
]
