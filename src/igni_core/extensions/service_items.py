"""What a service sends a producer's agent.

``docs/CLOUD.md`` §6.1 and igni server's service-identity §15. A service
sends **items** — an ``event``, a ``message``, a new ``chat`` or an
``action`` on a chat — through igni server, which hands them to the
producer's igni with the same lease and acknowledgement an event has.

An event is its extension's: igni hands it to the handler and posts
nothing. A message, a chat or an action is the agent's: igni posts it as
the service sent it, and no extension code is involved.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from igni_core.protocol.json_value import WireJson


class ItemKind(StrEnum):
    """What kind of item a service sent.

    * ``event``: for the extension's own code to handle; nothing is posted.
    * ``message``: a message posted to a chat, for the agent.
    * ``chat``: opens a chat with a title; may carry its first message.
    * ``action``: acts on a chat: show a view, rename it, or cancel its run.
    """

    EVENT = "event"
    MESSAGE = "message"
    CHAT = "chat"
    ACTION = "action"


class ItemAttachment(BaseModel):
    """A file the service uploaded for the item, as igni server describes it."""

    model_config = ConfigDict(extra="ignore")

    attachment_id: str = Field(description="The attachment's id.")
    filename: str = Field(description="The file's name.")
    content_type: str = Field(
        default="application/octet-stream", description="The file's media type."
    )
    size: int = Field(default=0, description="The file's size, in bytes.")
    sha256: str = Field(
        default="", description="The file's SHA-256 hash, in hex; empty when not given."
    )


class ChatAction(BaseModel):
    """What an ``action`` item does to its chat."""

    model_config = ConfigDict(extra="ignore")

    op: Literal["show", "rename", "cancel"] = Field(
        description=(
            "`show`: show a view in the chat. `rename`: rename the chat. `cancel`: cancel "
            "the chat's run."
        )
    )
    view: dict[str, WireJson] | None = Field(
        default=None,
        description=(
            "For `show`: the view, as a json-render spec. Shown in the chat, never sent to "
            "the model."
        ),
    )
    title: str | None = Field(
        default=None,
        description="For `rename`: the chat's new title. For `show`: the view's title.",
    )


__all__ = ["ChatAction", "ItemAttachment", "ItemKind"]
