"""Backend gateway — the single seam for plugin / marketplace ops.

Every verb in :mod:`igni_core.backend.cmd_plugin.verbs` delegates
through :class:`PluginBackendGateway`, which owns one
:class:`PluginInstaller` and one :class:`MarketplaceRegistryStore`
and turns their expected failures (``GitError`` / ``PluginError`` /
``ValueError`` / ``OSError``) into Result models. Verbs never see
raised exceptions — they match on ``result.ok`` and render.

Constructed per command by
:class:`~igni_core.backend.cmd_plugin.commands.PluginCommand`, so a
mid-session settings reload flows through the ``data_dir`` and
source policy handed to each new gateway.
"""

from __future__ import annotations

from igni_core.backend.plugin_schemas import (
    AddMarketplaceResult,
    InstallResult,
    RefreshOneResult,
    RemoveResult,
    ResolvedInstallRef,
    UpdateResult,
)
from igni_core.core.config.settings import Settings
from igni_core.core.plugins.git import GitError
from igni_core.core.plugins.installer import PluginError, PluginInstaller
from igni_core.core.plugins.marketplace_store import MarketplaceRegistryStore
from igni_core.core.plugins.models import MarketplaceRegistry
from igni_core.core.plugins.source_policy import PluginSourcePolicy


class PluginBackendGateway:
    """Facade over :class:`PluginInstaller` +
    :class:`MarketplaceRegistryStore`.

    All git / PluginError / OSError / ValueError catches live in this
    one class. Unexpected exception types are not swallowed — they
    surface as bugs rather than as a generic failure message.
    """

    def __init__(
        self,
        data_dir: str,
        source_policy: PluginSourcePolicy | None = None,
        *,
        installer: PluginInstaller | None = None,
        marketplaces: MarketplaceRegistryStore | None = None,
    ) -> None:
        #: Which URLs git may be pointed at. Production passes the
        #: session's (``PluginSourcePolicy.from_settings``); a gateway
        #: built without one is not air-gapped.
        self._source_policy = source_policy or PluginSourcePolicy()
        self._installer = installer or PluginInstaller(
            data_dir=data_dir, source_policy=self._source_policy
        )
        self._marketplaces = marketplaces or MarketplaceRegistryStore(data_dir)

    @classmethod
    def from_settings(cls, settings: Settings) -> PluginBackendGateway:
        """A gateway over the settings' plugin data dir and source policy.

        Where the plugin data lives is the settings' business, so the
        gateway reads it from there instead of every caller digging
        the path out of the settings for itself.
        """
        return cls(
            data_dir=settings.storage.data_dir,
            source_policy=PluginSourcePolicy.from_settings(settings),
        )

    def is_git_available(self) -> bool:
        """Precondition every install/update path checks. Verbs call
        this first and short-circuit with a "install git" hint on
        False, so we don't chase a git error into the installer."""
        return self._installer.is_git_available()

    # ── /plugin install / update / remove ──────────────────────

    def resolve_install_ref(self, target: str) -> ResolvedInstallRef | None:
        """Resolve an ``@<marketplace>/<plugin>`` ref to a concrete
        git URL (+ optional subdir + marketplace-declared ref).
        Returns ``None`` when the marketplace or plugin name is not
        registered."""
        resolved = self._marketplaces.resolve_install_ref(target)
        if resolved is None:
            return None
        resolved_source, _entry = resolved
        return ResolvedInstallRef(
            url=resolved_source.url,
            subdir=resolved_source.subdir,
            ref=resolved_source.ref,
        )

    def install(self, url: str, *, ref: str | None, subdir: str | None) -> InstallResult:
        """Install a plugin from a git URL."""
        if not self._source_policy.allows(url):
            return InstallResult(ok=False, error=self._source_policy.refusal(url))
        try:
            manifest = self._installer.install(url, ref=ref, subdir=subdir)
        except GitError as e:
            return InstallResult(ok=False, error=f"git error: {e}")
        except PluginError as e:
            return InstallResult(ok=False, error=str(e))
        return InstallResult(ok=True, name=manifest.name, version=manifest.version or "")

    def update(self, name: str, *, ref: str | None) -> UpdateResult:
        """Update an installed plugin to a new HEAD (or a specified
        branch/tag/SHA via ``ref``)."""
        try:
            new_sha = self._installer.update(name, ref=ref)
        except GitError as e:
            return UpdateResult(ok=False, error=f"git error: {e}")
        except PluginError as e:
            return UpdateResult(ok=False, error=str(e))
        return UpdateResult(ok=True, sha=new_sha)

    def remove(self, name: str) -> RemoveResult:
        """Uninstall a plugin. Only PluginError is expected (no git
        network I/O here)."""
        try:
            self._installer.remove(name)
        except PluginError as e:
            return RemoveResult(ok=False, error=str(e))
        return RemoveResult(ok=True)

    # ── /plugin marketplace add / list / remove / refresh ──────

    def add_marketplace(self, url: str) -> AddMarketplaceResult:
        """Register a marketplace by URL. Failure surface: ``GitError``
        (clone), ``PluginError`` (manifest parse), ``ValueError``
        (catalog fetch / bad URL), ``OSError`` (registry write)."""
        if not self._source_policy.allows(url):
            return AddMarketplaceResult(ok=False, error=self._source_policy.refusal(url))
        try:
            entry = self._marketplaces.add(url)
        except GitError as e:
            return AddMarketplaceResult(ok=False, error=f"git error: {e}")
        except (PluginError, ValueError, OSError) as e:
            return AddMarketplaceResult(ok=False, error=f"Failed to add marketplace: {e}")
        return AddMarketplaceResult(ok=True, name=entry.name, plugin_count=entry.plugin_count)

    def list_marketplaces(self) -> MarketplaceRegistry:
        """Load the registry. Never raises for a missing or corrupt
        file (returns an empty registry), so no Result wrapper."""
        return self._marketplaces.load()

    def remove_marketplace(self, name: str) -> bool:
        """Unregister a marketplace. ``False`` when no marketplace by
        that name is registered."""
        return self._marketplaces.remove(name)

    def refresh_marketplace(self, name: str) -> RefreshOneResult:
        """Refresh one marketplace by name. Distinguishes
        "not registered" (``not_found=True``) from a fetch failure
        (``ok=False`` + ``error``) so the verb can surface distinct
        messages."""
        registered = self._marketplaces.load().find(name)
        if registered is not None and not self._source_policy.allows(registered.url):
            return RefreshOneResult(ok=False, error=self._source_policy.refusal(registered.url))
        try:
            refreshed = self._marketplaces.refresh(name)
        except GitError as e:
            return RefreshOneResult(ok=False, error=f"git error: {e}")
        except (PluginError, ValueError, OSError) as e:
            return RefreshOneResult(ok=False, error=f"Refresh failed: {e}")
        if refreshed is None:
            return RefreshOneResult(ok=False, not_found=True, name=name)
        return RefreshOneResult(ok=True, name=refreshed.name, plugin_count=refreshed.plugin_count)


__all__ = ["PluginBackendGateway"]
