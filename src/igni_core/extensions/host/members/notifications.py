"""``host.notifications``: the floor's notifications, as far as the grant reaches."""

from __future__ import annotations

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.members.answers import NotificationListing
from igni_core.extensions.host.members.base import Call, Members, call, get
from igni_core.extensions.host.members.rpc_args import MarkReadArgs
from igni_core.extensions.host.values import NotificationCounts, NotificationEntry
from igni_core.protocol.rpc import RpcMethod

#: The grant that reaches every extension's notifications, not only the caller's.
_ALL = "notifications"


class NotificationMembers(Members):
    @get("notifications.list")
    async def notification_list(self, c: Call) -> list[NotificationEntry]:
        found = await self.rpc(RpcMethod.NOTIFICATIONS_LIST, c=c)
        return NotificationListing.from_rpc(found).visible_to(c.grants, c.extension_id)

    @get("notifications.unread")
    async def unread(self, c: Call) -> NotificationCounts:
        counts = await self.rpc(RpcMethod.NOTIFICATIONS_COUNTS, c=c)
        return NotificationCounts.from_counts(counts).visible_to(c.grants)

    @call("notifications.notify")
    async def notify(self, c: Call) -> None:
        self.host.notices.notify(c.extension_id, c.params["notification"])

    @call("notifications.mark_read")
    async def mark_read(self, c: Call) -> NotificationCounts:
        ids = c.params.get("ids")
        source = c.params.get("source")
        if not c.grants.allows(_ALL):
            # Its own only: a source other than itself is refused, and
            # "everything" means everything of its own.
            if source not in (None, c.extension_id):
                raise self._not_granted(c)
            if ids:
                listing = NotificationListing.from_rpc(
                    await self.rpc(RpcMethod.NOTIFICATIONS_LIST, c=c)
                )
                if listing.foreign_ids(ids, c.extension_id):
                    raise self._not_granted(c)
            else:
                source = c.extension_id
        found = await self.rpc(
            RpcMethod.NOTIFICATIONS_MARK_READ, MarkReadArgs(ids=ids, source=source), c=c
        )
        counts = found.get("counts", {}) if isinstance(found, dict) else {}
        return NotificationCounts.from_counts(counts).visible_to(c.grants)

    @staticmethod
    def _not_granted(c: Call) -> HostModelError:
        return HostModelError.not_granted(c.extension_id, "marking another's notifications", _ALL)


__all__ = ["NotificationMembers"]
