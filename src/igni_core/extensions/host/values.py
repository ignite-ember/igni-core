"""The typed values the members answer with and the broadcasts send.

Each model is named after the host schema's own typedef
(:mod:`.definition`), so a strict-mode check and the model agree field
for field. Each carries its projection from what igni's backend hands
over — an RPC answer, a registry entry, a notification row — as a
classmethod. The coercions are explicit (``str(x.get(k, ""))``,
``int(x or 0)``) rather than left to lenient validators, so a value the
backend sends reaches an extension exactly as it always has.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel

from igni_core.backend.workspace_watch import Head
from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.signal_values import Signal
from igni_core.extensions.impact import SwitchedOff
from igni_core.extensions.registry import InstalledExtension
from igni_core.protocol import messages as msg


def _text(value: Any) -> str:
    """A value as text, with nothing as empty: ``None`` is not the word "None"."""
    return "" if value is None else str(value)


class SessionInfo(BaseModel):
    """One session of the person's list."""

    session_id: str
    name: str
    created_at: int
    updated_at: int
    run_count: int
    summary: str
    agent_name: str

    @classmethod
    def from_entry(cls, entry: dict[str, Any]) -> SessionInfo:
        """One entry of ``list_sessions``."""
        return cls(
            session_id=_text(entry.get("session_id")),
            name=_text(entry.get("name")),
            created_at=int(entry.get("created_at") or 0),
            updated_at=int(entry.get("updated_at") or 0),
            run_count=int(entry.get("run_count") or 0),
            summary=_text(entry.get("summary")),
            agent_name=_text(entry.get("agent_name")),
        )


class SessionStatus(Signal):
    """``Status``: a session's model, mode and token counts."""

    WIRE: ClassVar[str] = "chat.status_changed"

    session_id: str
    model: str
    model_configured: bool
    permission_mode: str
    input_tokens: int
    output_tokens: int
    context_tokens: int
    max_context: int

    @classmethod
    def from_status(cls, status: dict[str, Any], session_id: str) -> SessionStatus:
        """``get_status`` (or a ``status_update``), for ``session_id``."""
        return cls(
            session_id=session_id,
            model=_text(status.get("model")),
            model_configured=bool(status.get("model_configured", True)),
            permission_mode=_text(status.get("permission_mode", "default")),
            input_tokens=int(status.get("input_tokens") or 0),
            output_tokens=int(status.get("output_tokens") or 0),
            context_tokens=int(status.get("context_tokens") or 0),
            max_context=int(status.get("max_context") or 0),
        )

    @classmethod
    def from_update(cls, m: msg.StatusUpdate) -> SessionStatus:
        """A ``status_update``, already typed."""
        return cls(
            session_id=m.session_id,
            model=m.model,
            model_configured=m.model_configured,
            permission_mode=m.permission_mode,
            input_tokens=m.input_tokens,
            output_tokens=m.output_tokens,
            context_tokens=m.context_tokens,
            max_context=m.max_context,
        )


class Connection(BaseModel):
    """``app.connection``: whether igni reaches the cloud and has a model."""

    cloud_connected: bool
    model_configured: bool


class Account(Signal):
    """Who is signed in to igni server, if anyone."""

    WIRE: ClassVar[str] = "account.changed"

    signed_in: bool
    email: str
    name: str
    org: str

    @classmethod
    def from_status(cls, status: dict[str, Any]) -> Account:
        """The account part of ``get_status`` (or a ``status_update``)."""
        email = str(status.get("cloud_email") or "")
        return cls(
            signed_in=bool(email) or bool(status.get("cloud_connected")),
            email=email,
            name=str(status.get("cloud_name") or ""),
            org=str(status.get("cloud_org") or ""),
        )

    @classmethod
    def from_update(cls, m: msg.StatusUpdate) -> Account:
        """The account part of a ``status_update``, already typed."""
        return cls(
            signed_in=bool(m.cloud_email) or m.cloud_connected,
            email=m.cloud_email,
            name=m.cloud_name,
            org=m.cloud_org,
        )


class HeadInfo(BaseModel):
    """``Head``: where the project's git HEAD points. Named apart from
    :class:`~igni_core.backend.workspace_watch.Head`, which it is made from."""

    commit_sha: str
    branch: str

    @classmethod
    def of(cls, head: Head) -> HeadInfo:
        return cls(commit_sha=head.commit_sha, branch=head.branch)


class CommandOutcome(BaseModel):
    """What a slash command answered."""

    kind: str
    content: str
    display_content: str

    @classmethod
    def from_answer(cls, answer: Any) -> CommandOutcome:
        """The backend's answer to a ``Command``.

        Untyped on purpose: the backend answers a command with whichever
        message the command produces (``Info``, ``Error``, a command
        result), so the fields are read where present.
        """
        return cls(
            kind=_text(getattr(answer, "kind", "info")),
            content=_text(getattr(answer, "content", "")),
            display_content=_text(getattr(answer, "display_content", "")),
        )


class UpdateInfo(BaseModel):
    """A newer igni, when the update check found one."""

    #: The signal an update check answers with: this, or ``None``.
    CHANGED: ClassVar[str] = "app.update_changed"

    current_version: str
    latest_version: str
    download_url: str

    @classmethod
    def from_check(cls, found: Any) -> UpdateInfo | None:
        """``check_for_update``'s answer; ``None`` when nothing is newer."""
        if not isinstance(found, dict) or not found.get("available"):
            return None
        return cls(
            current_version=found["current_version"],
            latest_version=found["latest_version"],
            download_url=found["download_url"],
        )


class SessionAttached(BaseModel):
    """A session made live: its id, and the folder it works in."""

    session_id: str
    project_dir: str


class Withheld(BaseModel):
    """A claim an extension does not get, and why."""

    claim: str
    reason: str

    @classmethod
    def of(cls, o: SwitchedOff) -> Withheld:
        return cls(claim=o.claim, reason=o.reason)


class ExtensionInfo(BaseModel):
    """One installed extension, as ``self.state`` and ``extensions.installed`` tell it."""

    id: str
    name: str
    version: str
    state: str
    reason: str
    web_url: str | None
    switched_off: list[Withheld]
    dropped: list[Withheld]
    faulty: str

    @classmethod
    def from_entry(cls, entry: InstalledExtension, web_url: str | None) -> ExtensionInfo:
        manifest = entry.manifest
        return cls(
            id=manifest.id,
            name=manifest.display_name,
            version=manifest.version,
            state=entry.state.value,
            reason=entry.reason,
            web_url=web_url,
            switched_off=[Withheld.of(o) for o in entry.switched_off],
            dropped=[Withheld.of(o) for o in entry.dropped],
            faulty=entry.faulty,
        )


class SettingsChange(BaseModel):
    """An extension's settings, as they are after a save."""

    values: dict[str, Any]


class NotificationEntry(BaseModel):
    """One notification of the floor's list."""

    id: str
    origin: str
    source: str
    kind: str
    title: str
    body: str
    actor: str
    #: The row's target as it was stored, when it is an object.
    target: dict[str, Any] | None
    #: ISO time when the row carries a datetime; otherwise as the row has it.
    created_at: Any
    read: bool

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> NotificationEntry:
        """One row of ``notifications_list``."""
        target = row.get("target")
        created = row.get("created_at")
        return cls(
            id=_text(row.get("id")),
            origin=_text(row.get("origin")),
            source=_text(row.get("source")),
            kind=_text(row.get("kind")),
            title=_text(row.get("title")),
            body=_text(row.get("body")),
            actor=str(row.get("actor") or ""),
            target=target if isinstance(target, dict) else None,
            created_at=created.isoformat() if hasattr(created, "isoformat") else created,
            read=bool(row.get("read")),
        )


class NotificationCounts(BaseModel):
    """Unread notifications: all of them, and by source."""

    unread: int
    by_source: dict[str, int]

    @classmethod
    def from_counts(cls, counts: dict[str, Any]) -> NotificationCounts:
        """``notifications_counts`` (or the counts a push carries)."""
        return cls(
            unread=int(counts.get("unread") or 0),
            by_source={str(k): int(v) for k, v in (counts.get("by_source") or {}).items()},
        )

    def visible_to(self, grants: HostGrants) -> NotificationCounts:
        """What one extension may see: everything with ``notifications``,
        otherwise its own count alone."""
        if grants.sees_all_notifications:
            return self
        own = self.by_source.get(grants.extension_id, 0)
        return NotificationCounts(unread=own, by_source={grants.extension_id: own} if own else {})


__all__ = [
    "Account",
    "CommandOutcome",
    "Connection",
    "ExtensionInfo",
    "HeadInfo",
    "NotificationCounts",
    "NotificationEntry",
    "SessionAttached",
    "SessionInfo",
    "SessionStatus",
    "SettingsChange",
    "UpdateInfo",
    "Withheld",
]
