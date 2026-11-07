"""What the host model pushes to igni's windows: model changes, links, page signals.

One place builds each :class:`~igni_core.protocol.messages.PushNotification`
the host sends, from a typed payload.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from igni_core.extensions.host.windows import WindowChange
from igni_core.extensions.host.wire import SignalMessage
from igni_core.protocol import messages as msg
from igni_core.protocol.schemas.enums import PushChannel

Broadcast = Callable[[msg.Message], None]


class ExternalOpen(BaseModel):
    """The ``open_external`` push: which window opens which link, for whom."""

    window: str
    url: str
    #: The extension that asked.
    source: str


class HostSignalPush(BaseModel):
    """The ``host_signal`` push: one signal for one extension's pages."""

    extension_id: str
    signal: str
    value: Any = None


class WindowPushes:
    """Pushes to every window, through the backend's broadcast."""

    def __init__(self, broadcast: Broadcast) -> None:
        self._broadcast = broadcast

    def window_changed(self, change: WindowChange) -> None:
        """A window's model changed: the window it is about applies it."""
        self._push(PushChannel.WINDOW_MODEL_CHANGED, change.model_dump(mode="json"))

    def open_external(self, window: str, url: str, *, source: str) -> None:
        """Ask ``window`` to open ``url`` the way it opens every link
        outside igni. Checked already
        (:class:`~igni_core.extensions.host.members.window_values.ExternalLink`); the window
        checks again before it opens anything."""
        self._push(
            PushChannel.OPEN_EXTERNAL,
            ExternalOpen(window=window, url=url, source=source).model_dump(),
        )

    def host_signal(self, extension_id: str, message: SignalMessage) -> None:
        """A signal for an extension's pages; each window posts it into that extension's frames."""
        self._push(
            PushChannel.HOST_SIGNAL,
            HostSignalPush(
                extension_id=extension_id, signal=message.signal, value=message.value
            ).model_dump(),
        )

    def _push(self, channel: PushChannel, payload: dict[str, Any]) -> None:
        self._broadcast(msg.PushNotification(channel=channel.value, payload=payload))


__all__ = ["ExternalOpen", "HostSignalPush", "WindowPushes"]
