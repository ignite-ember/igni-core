"""The installed extensions, as the host model tells them: grants, info, settings."""

from __future__ import annotations

from typing import Any

from igni_core.extensions.host.backend_gateway import HostBackendGateway
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.values import ExtensionInfo, SettingsChange
from igni_core.extensions.registry import ExtensionRegistry, InstalledExtension
from igni_core.extensions.store import ExtensionStore
from igni_core.extensions.web_home import ExtensionWebHome
from igni_core.protocol.rpc import RpcMethod


class ExtensionDirectory:
    """Looks extensions up in the registry and the store; saves settings the UI's way."""

    def __init__(
        self,
        registry: ExtensionRegistry,
        store: ExtensionStore,
        web_home: ExtensionWebHome,
        gateway: HostBackendGateway,
    ) -> None:
        self.registry = registry
        self.store = store
        self._web_home = web_home
        self._gateway = gateway

    def grants_for(self, extension_id: str) -> HostGrants | None:
        entry = self.registry.get(extension_id)
        if entry is None:
            return None
        return HostGrants(entry.manifest)

    def info(self, extension_id: str) -> ExtensionInfo:
        return self._info(self._entry(extension_id))

    def installed(self) -> list[ExtensionInfo]:
        return [
            self._info(entry)
            for extension_id in self.registry.ids()
            if (entry := self.registry.get(extension_id)) is not None
        ]

    def settings_of(self, extension_id: str) -> dict[str, Any]:
        return dict(self.store.settings(self._entry(extension_id).manifest))

    def saved_settings(self, extension_id: str) -> dict[str, Any] | None:
        """The extension's saved settings; ``None`` when it is not installed (never raises)."""
        entry = self.registry.get(extension_id)
        if entry is None:
            return None
        return dict(self.store.settings(entry.manifest))

    async def save_settings(self, extension_id: str, values: dict[str, Any]) -> SettingsChange:
        """Through ``set_extension_settings``, as the Extensions page saves them."""
        await self._gateway.rpc(
            RpcMethod.SET_EXTENSION_SETTINGS,
            {"extension_id": extension_id, "values": values},
            session_id=self._gateway.default_session(),
        )
        return SettingsChange(values=self.settings_of(extension_id))

    def _entry(self, extension_id: str) -> InstalledExtension:
        entry = self.registry.get(extension_id)
        if entry is None:
            raise HostModelError(ErrorCode.NOT_FOUND, f"no extension {extension_id!r} is installed")
        return entry

    def _info(self, entry: InstalledExtension) -> ExtensionInfo:
        return ExtensionInfo.from_entry(entry, self._web_home.url_for(entry.manifest))


__all__ = ["ExtensionDirectory"]
