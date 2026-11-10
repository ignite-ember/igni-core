"""What the host model pushes to igni's windows: model changes, links, page signals.

One place builds each :class:`~igni_core.protocol.messages.PushNotification`
the host sends, from a typed payload.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from igni_core.extensions.host.window_changes import WindowChange
from igni_core.extensions.host.wire import SignalMessage
from igni_core.protocol import messages as msg
from igni_core.protocol.json_value import WireJson
from igni_core.protocol.schemas.enums import PushChannel

Broadcast = Callable[[msg.Message], None]


class ExternalOpen(BaseModel):
    """An extension asked for a link to be opened in the person's browser."""

    window: str = Field(
        description="The id of the window that should open it; other windows ignore it."
    )
    url: str = Field(description="The link to open.")
    source: str = Field(description="The id of the extension that asked.")


class HostSignalPush(BaseModel):
    """A signal for one extension's pages: pass it to every page of that extension you show."""

    extension_id: str = Field(description="The id of the extension whose pages it is for.")
    signal: str = Field(
        description="The signal's name, e.g. `chat.run_completed` or `session.switched`."
    )
    value: WireJson = Field(
        default=None, description="The signal's value; its shape depends on `signal`."
    )


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
        (:class:`~igni_core.extensions.host.external_link.ExternalLink`); the window
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
