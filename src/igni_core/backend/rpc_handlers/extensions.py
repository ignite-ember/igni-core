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

from igni_core.backend.rpc_handlers.base import RpcHandler, WireArgs, rpc
from igni_core.backend.schemas_extensions import (
    ExtensionListResult,
    ExtensionSummary,
    SetExtensionSettingsArgs,
)
from igni_core.extensions.store_errors import StoreError
from igni_core.extensions.web_home import ExtensionWebHome
from igni_core.protocol.rpc import RpcMethod


@dataclass(frozen=True)
class SettingsSaved:
    """Whether a settings write was stored, and if not, why not."""

    ok: bool
    error: str = ""


class ExtensionsRpcHandler(RpcHandler):
    """What the client needs to render installed extensions."""

    @rpc(RpcMethod.LIST_EXTENSIONS)
    async def list_extensions(self, args: WireArgs) -> ExtensionListResult:
        """Every installed extension, including the broken ones.

        A ``FAILED`` extension is returned with its reason rather than
        filtered out. An administrator looking for "why is Compliance
        missing" is better served by a row that says why than by an
        absence they have to interpret.
        """
        return ExtensionListResult(extensions=self._summaries())

    @rpc(RpcMethod.SET_EXTENSION_SETTINGS, args=SetExtensionSettingsArgs)
    async def set_extension_settings(self, args: SetExtensionSettingsArgs) -> ExtensionListResult:
        """Store settings an administrator changed.

        Returns the whole list rather than the one row: the caller is
        a settings surface that has just invalidated its own view of
        everything, and one round trip is simpler than a merge it
        would have to get right.

        A refusal leaves as an RPC error, not as a reply. Two callers
        depend on that: the web client's Extensions panel shows the
        error when the call rejects and otherwise redraws as saved, and
        the after-RPC write signals tell the extension its settings
        changed for any answer that is not an error.

        The values reach the extension as launch params on its next
        mount. Not pushed to a running frame — `params` can carry
        them, but an extension that reconfigured itself mid-render is
        a harder promise than this is worth until somebody asks.
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
