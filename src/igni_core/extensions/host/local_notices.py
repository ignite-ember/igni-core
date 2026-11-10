"""``host.notifications.notify``: a notice from an extension, checked as one over MCP is."""

from __future__ import annotations

from typing import Any

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.spec import ErrorCode
from igni_core.notifications.extension_notices import ExtensionNoticeReceiver, NoticeVerdict


class LocalNotices:
    """The floor's notice receiver, when this igni keeps notifications."""

    def __init__(self, receiver: ExtensionNoticeReceiver | None) -> None:
        self._receiver = receiver

    def notify(self, extension_id: str, notification: dict[str, Any]) -> None:
        """The same acceptance a notice arriving over MCP gets."""
        if self._receiver is None:
            raise HostModelError(ErrorCode.FAILED, "this igni keeps no notifications")
        outcome = self._receiver.receive(extension_id, notification)
        if outcome.verdict is NoticeVerdict.REFUSED:
            raise HostModelError(ErrorCode.REFUSED, outcome.reason)


__all__ = ["LocalNotices"]
