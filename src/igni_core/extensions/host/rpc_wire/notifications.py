"""The notification handlers: marking read, and the floor's list."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.rpc_wire.base import RpcArgs
from igni_core.extensions.host.values import NotificationEntry


class MarkReadArgs(RpcArgs):
    #: ``None``: every notification of ``source``, or all of them.
    ids: list[str] | None
    source: str | None


class NotificationRow(BaseModel):
    """One row of the floor's list: the entry, and its id as the row has it."""

    entry: NotificationEntry
    #: The raw id — an ``int`` or a ``str`` — not :attr:`NotificationEntry.id`,
    #: which is always a string: ids a caller names are compared as rows carry them.
    key: int | str | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> NotificationRow:
        return cls(entry=NotificationEntry.from_row(row), key=row.get("id"))


class NotificationListing(BaseModel):
    """``notifications_list``: the floor's rows, and who owns which."""

    rows: list[NotificationRow]

    @classmethod
    def from_rpc(cls, found: Any) -> NotificationListing:
        rows = found.get("notifications", []) if isinstance(found, dict) else []
        return cls(rows=[NotificationRow.from_row(r) for r in rows])

    def visible_to(self, grants: HostGrants, extension_id: str) -> list[NotificationEntry]:
        """Every row with ``notifications``; otherwise the extension's own."""
        entries = [r.entry for r in self.rows]
        if grants.sees_all_notifications:
            return entries
        return [n for n in entries if n.source == extension_id]

    def foreign_ids(self, ids: list[Any], extension_id: str) -> list[Any]:
        """The ``ids`` that are not the extension's own rows. Compared as
        the rows carry them, not as strings."""
        own = {r.key for r in self.rows if r.entry.source == extension_id}
        return [i for i in ids if i not in own]


__all__ = ["MarkReadArgs", "NotificationListing", "NotificationRow"]
