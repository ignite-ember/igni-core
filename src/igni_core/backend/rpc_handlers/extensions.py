"""The base side of the extension surface.

One method so far: what is installed. An extension's *own* methods do
not come through here — they are ``ext.<id>.<method>``, routed by
prefix in :mod:`igni_core.extensions.rpc` because they arrive with a
manifest rather than at import time.

This exists so the frontend can draw an extension at all. Until it
did, an installed extension answered calls and had nowhere to put
them: nothing told the client which extensions existed, what state
they were in, or which loopback port their UI was on.
"""

from __future__ import annotations

from dataclasses import dataclass

from igni_core.backend.rpc_binding import NoArgs, rpc
from igni_core.backend.rpc_handlers.base import RpcHandler
from igni_core.backend.schemas_extensions import (
    ExtensionListResult,
    ExtensionSummary,
    SetExtensionSettingsArgs,
)
from igni_core.extensions.store import StoreError
from igni_core.extensions.web_home import ExtensionWebHome
from igni_core.protocol.rpc import RpcMethod, RpcScope


@dataclass(frozen=True)
class SettingsSaved:
    """Whether a settings write was stored, and if not, why not."""

    ok: bool
    error: str = ""


class ExtensionsRpcHandler(RpcHandler):
    """What the client needs to render installed extensions."""

    @rpc(RpcMethod.LIST_EXTENSIONS, args=NoArgs, scope=RpcScope.IGNI)
    async def list_extensions(self, args: NoArgs) -> ExtensionListResult:
        """List the installed extensions, including the ones that failed to start.

        Takes no arguments. Returns each extension's id, name, version and
        ``state`` — a failed one with its ``reason``, rather than left out —
        with what it adds to igni's window (pages, menu items, indicators),
        its settings and their values, and the addresses of its UI, its web
        home and its service.
        """
        return ExtensionListResult(extensions=self._summaries())

    @rpc(RpcMethod.SET_EXTENSION_SETTINGS, args=SetExtensionSettingsArgs, scope=RpcScope.IGNI)
    async def set_extension_settings(self, args: SetExtensionSettingsArgs) -> ExtensionListResult:
        """Change an installed extension's settings.

        ``extension_id`` is the extension; ``values`` the settings to
        change, by key, merged over what is stored. The extension reads them
        the next time it starts. Returns the whole list, as
        ``list_extensions`` does. Fails when the extension is not
        installed, a key is not one of its settings, a value is not one of
        a setting's options, or igni has no extension support.
        """
        saved = self._save_settings(args)
        if not saved.ok:
            raise ValueError(saved.error)
        return ExtensionListResult(extensions=self._summaries())

    def _save_settings(self, args: SetExtensionSettingsArgs) -> SettingsSaved:
        extensions = self._ctx.extensions
        store = self._ctx.extension_store
        if extensions is None or store is None:
            return SettingsSaved(ok=False, error="this backend has no extension support")
        entry = extensions.registry.get(args.extension_id)
        if entry is None:
            return SettingsSaved(ok=False, error=f"no extension '{args.extension_id}' is installed")
        try:
            store.save_settings(entry.manifest, args.values)
        except StoreError as exc:
            return SettingsSaved(ok=False, error=str(exc))
        return SettingsSaved(ok=True)

    def _summaries(self) -> list[ExtensionSummary]:
        extensions = self._ctx.extensions
        store = self._ctx.extension_store
        if extensions is None or store is None:
            # A backend built without extension support. An empty list
            # is the truthful answer: nothing is installed, and
            # nothing can be.
            return []

        origins = self._ctx.origins
        registry = extensions.registry
        web_home = ExtensionWebHome(store)
        return [
            ExtensionSummary.of(
                entry,
                origins.url_for(extension_id) if origins is not None else None,
                store.settings(entry.manifest),
                web_home.url_for(entry.manifest),
                origins.service_url_for(extension_id) if origins is not None else None,
            )
            for extension_id in registry.ids()
            if (entry := registry.get(extension_id)) is not None
        ]
