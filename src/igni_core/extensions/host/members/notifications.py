"""``host.notifications``: the floor's notifications, as far as the grant reaches."""

from __future__ import annotations

from igni_core.extensions.host.grants import NOTIFICATIONS
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.tag import call, get
from igni_core.extensions.host.rpc_wire.notifications import MarkReadArgs, NotificationListing
from igni_core.extensions.host.values import NotificationCounts, NotificationEntry
from igni_core.protocol.rpc import RpcMethod


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
        # An optional field the extension left out arrives as ``None``; the
        # notice's own model has a default for it, and no use for ``None``.
        notice = {k: v for k, v in c.params["notification"].items() if v is not None}
        target = notice.get("target")
        if isinstance(target, dict):
            notice["target"] = {k: v for k, v in target.items() if v is not None}
        self.host.notices.notify(c.extension_id, notice)

    @call("notifications.mark_read")
    async def mark_read(self, c: Call) -> NotificationCounts:
        ids = c.params.get("ids")
        source = c.params.get("source")
        if not c.grants.sees_all_notifications:
            # Its own only: a source other than itself is refused, and
            # "everything" means everything of its own.
            if source not in (None, c.extension_id):
                raise c.not_granted("marking another's notifications", NOTIFICATIONS)
            if ids:
                listing = NotificationListing.from_rpc(
                    await self.rpc(RpcMethod.NOTIFICATIONS_LIST, c=c)
                )
                if listing.foreign_ids(ids, c.extension_id):
                    raise c.not_granted("marking another's notifications", NOTIFICATIONS)
            else:
                source = c.extension_id
        found = await self.rpc(
            RpcMethod.NOTIFICATIONS_MARK_READ, MarkReadArgs(ids=ids, source=source), c=c
        )
        counts = found.get("counts", {}) if isinstance(found, dict) else {}
        return NotificationCounts.from_counts(counts).visible_to(c.grants)


__all__ = ["NotificationMembers"]
