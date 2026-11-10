"""The shapes notifications take, from igni server, from an extension, and to the client.

igni server's ``docs/service-identity.md`` §10. Two paths arrive here
and leave as one: a row igni server delivered to this person
(:class:`ServerNotification`), and a notice an extension's own process
told this host (:class:`ExtensionNotice`). Both become a
:class:`FloorNotification`, which is all the client ever sees.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class NotificationTarget(BaseModel):
    """Where opening a notification goes: one of its extension's pages."""

    model_config = ConfigDict(extra="ignore")

    page: str = Field(min_length=1, description="The id of the extension's page to open.")
    # Strings, because they end up in a route the client persists and in
    # the extension's ``params``.
    params: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "The parameters to open the page with. Always strings: a number or a flag is "
            "given as its JSON text."
        ),
    )

    @field_validator("params", mode="before")
    @classmethod
    def _as_strings(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        return {
            str(key): item if isinstance(item, str) else json.dumps(item)
            for key, item in value.items()
            if item is not None
        }


class ServerNotification(BaseModel):
    """One row of ``GET /v1/notifications`` (§10.1)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    #: The organisation it happened in. igni server lists every
    #: organisation the person belongs to in one feed.
    org_id: str = ""
    #: The extension the notifying service ships, recorded by igni server.
    source: str
    kind: str = ""
    title: str = ""
    body: str = ""
    actor: str | None = None
    target: NotificationTarget | None = None
    created_at: datetime | None = None
    delivered_at: datetime | None = None
    read_at: datetime | None = None


class OrganisationMembership(BaseModel):
    """One organisation the person belongs to, from ``GET /v1/portal/me``."""

    model_config = ConfigDict(extra="ignore")

    id: str
    display_name: str = ""


class PortalMe(BaseModel):
    """The part of ``GET /v1/portal/me`` the notifications read: where the
    person is a member."""

    model_config = ConfigDict(extra="ignore")

    organizations: list[OrganisationMembership] = Field(default_factory=list)

    def names(self) -> dict[str, str]:
        """Organisation id to the name people know it by."""
        return {org.id: org.display_name or org.id for org in self.organizations}


class ServerNotificationPage(BaseModel):
    """What ``GET /v1/notifications`` answers."""

    model_config = ConfigDict(extra="ignore")

    notifications: list[ServerNotification] = Field(default_factory=list)


class NotificationCounts(BaseModel):
    """How many notifications are unread, in all and per extension — the numbers for badges."""

    model_config = ConfigDict(extra="ignore")

    unread: int = Field(default=0, description="Unread notifications in all.")
    by_source: dict[str, int] = Field(
        default_factory=dict,
        description="Unread notifications per extension id; an extension with none is left out.",
    )

    def plus(self, other: NotificationCounts) -> NotificationCounts:
        """Both counts, added source by source."""
        by_source = dict(self.by_source)
        for source, count in other.by_source.items():
            by_source[source] = by_source.get(source, 0) + count
        return NotificationCounts(
            unread=self.unread + other.unread,
            by_source={source: n for source, n in by_source.items() if n > 0},
        )


class MarkReadRequest(BaseModel):
    """``POST /v1/notifications/read``: these ids, this source, or (neither) all."""

    ids: list[str] | None = None
    source: str | None = None

    def body(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class ExtensionNotice(BaseModel):
    """The params of ``notifications/igni/notify`` (§10.2).

    §10.1's shape without ``recipients`` or ``actor``: the person is
    whoever is using this igni. Bounded, because it is drawn in igni's
    own chrome and a process can send anything.
    """

    model_config = ConfigDict(extra="ignore")

    kind: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=1000)
    target: NotificationTarget
    event_key: str = Field(min_length=1, max_length=200)


class NotificationOrigin(StrEnum):
    """Where a notification came from.

    * ``server``: delivered by igni server, which keeps whether it was read.
    * ``local``: posted by an extension on this machine; this igni keeps
      whether it was read.
    """

    SERVER = "server"
    LOCAL = "local"


class FloorNotification(BaseModel):
    """One notification for the person, from igni server or from an extension on this machine."""

    id: str = Field(description="The notification's id; use it to mark it read.")
    origin: NotificationOrigin = Field(description="Where it came from.")
    source: str = Field(description="The id of the extension it is from.")
    kind: str = Field(
        default="",
        description="The extension's own name for what kind of notification it is; empty when none.",
    )
    title: str = Field(default="", description="A one-line headline.")
    body: str = Field(
        default="", description="The text under the headline; empty when there is none."
    )
    actor: str = Field(
        default="", description="Who caused it, for display; empty when nobody in particular."
    )
    target: NotificationTarget | None = Field(
        default=None,
        description="The page to open when the person opens it; null when there is none.",
    )
    created_at: datetime | None = Field(
        default=None, description="When it was created; null when unknown."
    )
    read: bool = Field(default=False, description="True once it has been marked read.")
    org_id: str = Field(
        default="",
        description="The organisation it happened in, for one from igni server; empty for a local one.",
    )
    # With one organisation every row is from it, and saying so is noise.
    org_name: str = Field(
        default="",
        description=(
            "That organisation's name, set only when the person belongs to more than one. "
            "Show it when it is set."
        ),
    )

    @classmethod
    def from_server(
        cls, row: ServerNotification, organisations: dict[str, str] | None = None
    ) -> FloorNotification:
        """``organisations`` is the person's, id to name. A name is put on
        the row only when there is more than one of them."""
        several = organisations is not None and len(organisations) > 1
        return cls(
            id=row.id,
            origin=NotificationOrigin.SERVER,
            org_id=row.org_id,
            org_name=(organisations or {}).get(row.org_id, "") if several else "",
            source=row.source,
            kind=row.kind,
            title=row.title,
            body=row.body,
            actor=row.actor or "",
            target=row.target,
            created_at=row.created_at,
            read=row.read_at is not None,
        )


class NotificationsListResult(BaseModel):
    """The person's notifications, newest first, with the unread counts."""

    notifications: list[FloorNotification] = Field(
        default_factory=list, description="The notifications, newest first."
    )
    counts: NotificationCounts = Field(
        default_factory=NotificationCounts, description="The unread counts."
    )
    server_error: str = Field(
        default="",
        description=(
            "Why igni server could not be asked, when it could not; empty otherwise. The "
            "local notifications and the last ones fetched from the server are still returned."
        ),
    )


class NotificationsReadResult(BaseModel):
    """The unread counts after marking notifications read."""

    counts: NotificationCounts = Field(
        default_factory=NotificationCounts, description="The unread counts now."
    )
    server_error: str = Field(
        default="",
        description=(
            "Why igni server could not be told, when it could not; empty otherwise. "
            "Notifications from the server may then still count as unread."
        ),
    )


#: What moved the counts. ``server``: igni server's list was read again on
#: its own (a sign-in, the stream saying something changed). ``listed``: a
#: ``notifications_list`` call read it. ``marked_read``: a
#: ``notifications_mark_read`` call. ``extension``: an extension on this
#: machine posted a notice. ``signed_out``: the person signed out.
NotificationsCause = Literal["server", "listed", "marked_read", "extension", "signed_out"]


class NotificationsChanged(BaseModel):
    """The unread notification counts changed. Refetch the list if you are showing it."""

    counts: NotificationCounts = Field(
        default_factory=NotificationCounts, description="The unread counts now."
    )
    cause: NotificationsCause = Field(
        default="server",
        description=(
            "What changed them. `server`: igni server's list was fetched again on its own "
            "(after signing in, or when the server said something changed). `listed`: a "
            "`notifications_list` call fetched it. `marked_read`: a "
            "`notifications_mark_read` call. `extension`: an extension on this machine "
            "posted a notice. `signed_out`: the person signed out."
        ),
    )
