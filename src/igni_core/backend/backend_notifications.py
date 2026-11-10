"""The floor's notifications, both paths (igni server's ``docs/service-identity.md`` §10)."""

from __future__ import annotations

from typing import Any

from igni_core.backend.extensions_runtime import ExtensionsRuntime
from igni_core.backend.igni_server_access import IgniServerAccess
from igni_core.backend.login_coordinator import LoginCoordinator
from igni_core.backend.push_bridge import PushNotificationBridge
from igni_core.core.config.settings import Settings
from igni_core.core.db.locator import StorageLocator
from igni_core.extensions.contributions import ExtensionConditionReceiver
from igni_core.notifications.center import NotificationCenter
from igni_core.notifications.extension_notices import ExtensionNoticeReceiver
from igni_core.notifications.local_store import LocalNotificationStore


class BackendNotifications:
    """igni server's notifications and the ones extensions send, in one center.

    igni server's half follows the signed-in person, so it starts again on
    every sign-in. The local half is what an extension's process says with
    ``notifications/igni/notify`` on its own MCP connection, accepted only
    as coming from the extension that connection belongs to.
    """

    def __init__(self, *, center: NotificationCenter, notices: ExtensionNoticeReceiver) -> None:
        self._center = center
        self._notices = notices

    @classmethod
    def build(
        cls,
        *,
        settings: Settings,
        access: IgniServerAccess,
        push_bridge: PushNotificationBridge,
        runtime: ExtensionsRuntime,
        mcp: Any,
        login: LoginCoordinator,
    ) -> BackendNotifications:
        center = NotificationCenter(
            server=access.signed_in_server(),
            local=LocalNotificationStore.from_locator(StorageLocator.from_settings(settings)),
            announce=push_bridge.notifications_changed,
        )
        notices = ExtensionNoticeReceiver(registry=runtime.registry, sink=center)
        if mcp is not None:
            mcp.notifications.handle(ExtensionNoticeReceiver.METHOD, notices)
            # And the ``when`` of an extension's prompt contributions, on the
            # same connections for the same reason: the connection names the sender.
            mcp.notifications.handle(
                ExtensionConditionReceiver.METHOD, runtime.condition_receiver()
            )
        login.on_signed_in(center.schedule_signed_in)
        return cls(center=center, notices=notices)

    @property
    def center(self) -> NotificationCenter:
        return self._center

    @property
    def notices(self) -> ExtensionNoticeReceiver:
        return self._notices

    def start(self) -> None:
        self._center.start()

    async def stop(self) -> None:
        await self._center.stop()
