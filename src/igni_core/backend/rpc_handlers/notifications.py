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

from igni_core.backend.rpc_binding import NoArgs, rpc
from igni_core.backend.rpc_handlers.base import RpcHandler
from igni_core.notifications.schemas import (
    MarkReadRequest,
    NotificationCounts,
    NotificationsListResult,
    NotificationsReadResult,
)
from igni_core.protocol.rpc import RpcMethod, RpcScope
from igni_core.protocol.schemas.enums import PushChannel


class MarkReadArgs(BaseModel):
    """``notifications_mark_read``: ``{ids}``, ``{source}``, or ``{}`` for all."""

    model_config = ConfigDict(extra="ignore")

    ids: list[str] | None = None
    source: str | None = None


class NotificationsRpcHandler(RpcHandler):
    """List, count and mark the floor's notifications."""

    @rpc(
        RpcMethod.NOTIFICATIONS_LIST,
        args=NoArgs,
        scope=RpcScope.IGNI,
        streams=(PushChannel.NOTIFICATIONS_CHANGED,),
    )
    async def notifications_list(self, args: NoArgs) -> NotificationsListResult:
        """List the signed-in person's notifications, newest first.

        Takes no arguments. Reads igni server again and merges in what
        extensions on this machine posted. Returns at most 100
        ``notifications`` (each with its source, title, body, target and
        whether it is read), the unread ``counts`` in all and per
        extension, and ``server_error`` when igni server could not be
        read. The fresh counts are also pushed as ``notifications_changed``.
        """
        center = self._ctx.notifications
        if center is None:
            return NotificationsListResult()
        return await center.listing()

    @rpc(RpcMethod.NOTIFICATIONS_COUNTS, args=NoArgs, scope=RpcScope.IGNI)
    def notifications_counts(self, args: NoArgs) -> NotificationCounts:
        """Count the person's unread notifications, in all and per extension.

        Takes no arguments. Answers from what was last read, asking nobody;
        ``notifications_changed`` pushes the counts whenever they move.
        """
        center = self._ctx.notifications
        if center is None:
            return NotificationCounts()
        return center.counts()

    @rpc(
        RpcMethod.NOTIFICATIONS_MARK_READ,
        args=MarkReadArgs,
        scope=RpcScope.IGNI,
        streams=(PushChannel.NOTIFICATIONS_CHANGED,),
    )
    async def notifications_mark_read(self, args: MarkReadArgs) -> NotificationsReadResult:
        """Mark notifications read.

        ``ids`` names the notifications; ``source`` (an extension id) marks
        all of that extension's; neither marks every notification. Returns
        the unread ``counts`` afterwards, also pushed as
        ``notifications_changed``, and ``server_error`` when igni server
        could not be told.
        """
        center = self._ctx.notifications
        if center is None:
            return NotificationsReadResult()
        return await center.mark_read(MarkReadRequest(ids=args.ids, source=args.source))
