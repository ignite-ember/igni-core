"""Plugin + marketplace RPC handlers."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from igni_core.backend.plugin_schemas import PluginContents
from igni_core.backend.rpc_handlers.base import RpcHandler, WireArgs, rpc
from igni_core.core.plugins.models import MarketplaceInfo, PluginInfo
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod


class PluginArgs(BaseModel):
    """Base for every plugin RPC's arguments: keys a newer frontend adds
    are ignored rather than refused."""

    model_config = ConfigDict(extra="ignore")


class PluginNameArgs(PluginArgs):
    """``get_plugin_contents`` / ``remove_plugin``: the installed plugin
    to act on."""

    name: str


class PreviewPluginArgs(PluginArgs):
    """``preview_plugin``: the git source of a not-yet-installed plugin,
    with an optional branch and subdirectory inside the repo."""

    source: str
    branch: str | None = None
    subdir: str | None = None


class PluginEnabledArgs(PluginArgs):
    """``set_plugin_enabled``: one installed plugin, on or off."""

    name: str
    enabled: bool


class InstallPluginArgs(PluginArgs):
    """``install_plugin``: a git URL or ``@<marketplace>/<plugin>`` ref,
    and the branch / tag / SHA to check out (the default when absent)."""

    ref: str
    install_ref: str | None = None


class UpdatePluginArgs(PluginArgs):
    """``update_plugin``: the installed plugin and the branch / tag / SHA
    to reset it to (origin's HEAD when absent)."""

    name: str
    install_ref: str | None = None


class MarketplaceUrlArgs(PluginArgs):
    """``add_marketplace``: the catalog's URL."""

    url: str


class MarketplaceNameArgs(PluginArgs):
    """``remove_marketplace``: the registered marketplace to drop."""

    name: str


class RefreshMarketplacesArgs(PluginArgs):
    """``refresh_marketplaces``: one marketplace, or every one when absent."""

    name: str | None = None


class PluginsRpcHandler(RpcHandler):
    """Plugin details / contents / preview / enable-toggle + install
    / update / remove, plus marketplace CRUD + refresh."""

    @rpc(RpcMethod.GET_PLUGIN_DETAILS)
    def get_plugin_details(self, args: WireArgs) -> list[PluginInfo]:
        return self._ctx.backend.get_plugin_details()

    @rpc(RpcMethod.GET_PLUGIN_CONTENTS, args=PluginNameArgs)
    def get_plugin_contents(self, args: PluginNameArgs) -> PluginContents:
        return self._controllers.plugins.contents(args.name)

    @rpc(RpcMethod.PREVIEW_PLUGIN, args=PreviewPluginArgs)
    async def preview_plugin(self, args: PreviewPluginArgs) -> PluginContents:
        return await self._controllers.plugins.preview(
            source=args.source,
            branch=args.branch,
            subdir=args.subdir,
        )

    @rpc(RpcMethod.SET_PLUGIN_ENABLED, args=PluginEnabledArgs)
    def set_plugin_enabled(self, args: PluginEnabledArgs) -> msg.Info:
        return self._controllers.plugins.set_enabled(args.name, args.enabled)

    @rpc(RpcMethod.INSTALL_PLUGIN, args=InstallPluginArgs)
    def install_plugin(self, args: InstallPluginArgs) -> msg.Info:
        return self._controllers.plugins.install(args.ref, args.install_ref)

    @rpc(RpcMethod.UPDATE_PLUGIN, args=UpdatePluginArgs)
    def update_plugin(self, args: UpdatePluginArgs) -> msg.Info:
        return self._controllers.plugins.update(args.name, args.install_ref)

    @rpc(RpcMethod.REMOVE_PLUGIN, args=PluginNameArgs)
    def remove_plugin(self, args: PluginNameArgs) -> msg.Info:
        return self._controllers.plugins.remove(args.name)

    @rpc(RpcMethod.GET_MARKETPLACES)
    def get_marketplaces(self, args: WireArgs) -> list[MarketplaceInfo]:
        return self._ctx.backend.get_marketplaces()

    @rpc(RpcMethod.ADD_MARKETPLACE, args=MarketplaceUrlArgs)
    def add_marketplace(self, args: MarketplaceUrlArgs) -> msg.Info:
        return self._controllers.marketplaces.add(args.url)

    @rpc(RpcMethod.REMOVE_MARKETPLACE, args=MarketplaceNameArgs)
    def remove_marketplace(self, args: MarketplaceNameArgs) -> msg.Info:
        return self._controllers.marketplaces.remove(args.name)

    @rpc(RpcMethod.REFRESH_MARKETPLACES, args=RefreshMarketplacesArgs)
    def refresh_marketplaces(self, args: RefreshMarketplacesArgs) -> msg.Info:
        return self._controllers.marketplaces.refresh(args.name)
