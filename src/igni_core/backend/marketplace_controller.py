"""Marketplace registry controller — the marketplace panel's RPCs.

* :meth:`list_registered` — snapshot of registered marketplaces and
  their cached catalogs.
* :meth:`add` — register a new marketplace by URL.
* :meth:`remove` — unregister a marketplace (installed plugins from
  it remain).
* :meth:`refresh` — re-fetch one or all marketplaces.

Every operation goes through :class:`PluginBackendGateway` — the same
seam ``/plugin marketplace`` uses — so the source policy, the expected
failures it turns into typed results, and their wording live in one
place. This class only shapes those results into the panel's
:class:`msg.Info` reply. A gateway is built per call from the session,
so a mid-session change to the data dir or source policy is picked up
on the next call.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from igni_core.core.plugins.models import MarketplaceInfo
from igni_core.protocol import messages as msg

if TYPE_CHECKING:
    from igni_core.backend.cmd_plugin.gateway import PluginBackendGateway
    from igni_core.core.session import Session


class MarketplaceController:
    """Marketplace registry CRUD + bulk refresh. Composed onto
    :class:`BackendServer` as ``self.marketplaces``.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def _gateway(self) -> PluginBackendGateway:
        """A gateway over the session's current data dir and policy."""
        # Imported on first use: ``cmd_plugin`` is a slash command's
        # package and an idle backend must not load it
        # (tests/test_commands_load_on_first_use.py).
        from igni_core.backend.cmd_plugin.gateway import PluginBackendGateway

        return PluginBackendGateway.from_settings(self._session.settings)

    def list_registered(self) -> list[MarketplaceInfo]:
        """Snapshot of every registered marketplace for the panel."""
        registry = self._gateway().list_marketplaces()
        return [MarketplaceInfo.from_entry(m) for m in registry.marketplaces]

    async def add(self, url: str) -> msg.Info:
        """Register a new marketplace by URL and cache its catalog.

        The clone and the catalog read run in a worker thread, as do the
        other writes here: on the event loop they held up every session.
        """
        result = await asyncio.to_thread(self._gateway().add_marketplace, url)
        if not result.ok:
            return msg.Info(text=result.error)
        return msg.Info(text=f"Added '{result.name}' ({result.plugin_count} plugin(s) catalogued).")

    async def remove(self, name: str) -> msg.Info:
        """Unregister a marketplace. Installed plugins from it remain."""
        if not await asyncio.to_thread(self._gateway().remove_marketplace, name):
            return msg.Info(text=f"No marketplace named '{name}'.")
        return msg.Info(text=f"Unregistered '{name}'. Installed plugins from it remain.")

    async def refresh(self, name: str | None = None) -> msg.Info:
        """Re-fetch one marketplace by name, or every one when ``name``
        is empty."""
        gateway = self._gateway()
        if not name:
            from igni_core.backend.cmd_plugin.bulk_refresh import BulkRefreshRunner

            runner = BulkRefreshRunner(gateway)
            summary = await asyncio.to_thread(runner.run, gateway.list_marketplaces())
            return msg.Info(text=summary.summary_line())
        result = await asyncio.to_thread(gateway.refresh_marketplace, name)
        if result.not_found:
            return msg.Info(text=f"No marketplace named '{name}'.")
        if not result.ok:
            return msg.Info(text=f"Could not refresh '{name}': {result.error}")
        return msg.Info(text=f"Refreshed '{result.name}' ({result.plugin_count} plugins).")
