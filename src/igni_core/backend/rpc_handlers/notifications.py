"""The floor's notifications, for the client.

igni server's ``docs/service-identity.md`` §10. One surface for both
paths — what igni server delivered to whoever is signed in, and what
an extension on this machine told the host — answered by
:class:`~igni_core.notifications.center.NotificationCenter`. No
extension is asked anything: the count on an extension's badge is the
floor's, not the extension's.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from igni_core.backend.rpc_handlers.base import RpcHandler, rpc
from igni_core.notifications.schemas import (
    MarkReadRequest,
    NotificationCounts,
    NotificationsListResult,
    NotificationsReadResult,
)
from igni_core.protocol.rpc import RpcMethod


class MarkReadArgs(BaseModel):
    """``notifications_mark_read``: ``{ids}``, ``{source}``, or ``{}`` for all."""

    model_config = ConfigDict(extra="ignore")

    ids: list[str] | None = None
    source: str | None = None


class NotificationsRpcHandler(RpcHandler):
    """List, count and mark the floor's notifications."""

    @rpc(RpcMethod.NOTIFICATIONS_LIST)
    async def notifications_list(self, args: dict) -> NotificationsListResult:
        """Both paths, newest first, with igni server read now."""
        center = self._ctx.notifications
        if center is None:
            return NotificationsListResult()
        return await center.listing()

    @rpc(RpcMethod.NOTIFICATIONS_COUNTS)
    def notifications_counts(self, args: dict) -> NotificationCounts:
        """Unread, in all and per extension, as last read. Asks nobody:
        the counts move by the ``notifications_changed`` push."""
        center = self._ctx.notifications
        if center is None:
            return NotificationCounts()
        return center.counts()

    @rpc(RpcMethod.NOTIFICATIONS_MARK_READ)
    async def notifications_mark_read(self, args: dict) -> NotificationsReadResult:
        center = self._ctx.notifications
        if center is None:
            return NotificationsReadResult()
        parsed = MarkReadArgs.model_validate(args or {})
        return await center.mark_read(MarkReadRequest(ids=parsed.ids, source=parsed.source))
