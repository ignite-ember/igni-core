"""Plugin + marketplace RPC handlers."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from igni_core.backend.plugin_schemas import PluginContents
from igni_core.backend.rpc_binding import NoArgs, rpc
from igni_core.backend.rpc_handlers.base import RpcHandler
from igni_core.core.plugins.models import MarketplaceInfo, PluginInfo
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod, RpcScope


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

    @rpc(RpcMethod.GET_PLUGIN_DETAILS, args=NoArgs, scope=RpcScope.SESSION)
    def get_plugin_details(self, args: NoArgs) -> list[PluginInfo]:
        """List the installed plugins.

        Takes no arguments. Returns each plugin's name, version,
        description, where it came from, whether it is enabled (a plugin
        the organisation manages always is), the commit it is pinned to,
        and what it brings: skills, agents, hooks, MCP servers.
        """
        return self._ctx.backend.get_plugin_details()

    @rpc(RpcMethod.GET_PLUGIN_CONTENTS, args=PluginNameArgs, scope=RpcScope.SESSION)
    def get_plugin_contents(self, args: PluginNameArgs) -> PluginContents:
        """Show what an installed plugin contains.

        ``name`` is the installed plugin. Returns its folder and its
        skills, agents, hooks, MCP servers and tools, with its README; or
        ``error`` when no such plugin is installed.
        """
        return self._controllers.plugins.contents(args.name)

    @rpc(RpcMethod.PREVIEW_PLUGIN, args=PreviewPluginArgs, scope=RpcScope.SESSION)
    async def preview_plugin(self, args: PreviewPluginArgs) -> PluginContents:
        """Show what a plugin contains without installing it.

        ``source`` is the plugin's git URL (optionally followed by a
        subfolder); ``branch`` the branch, tag or commit to read (the
        default branch when absent); ``subdir`` the plugin's folder inside
        the repository, which wins over one in ``source``. The repository
        is cloned to a temporary place. Returns the same shape as
        ``get_plugin_contents``, with ``preview_source`` naming what was
        read; or ``error`` when the organisation's policy refuses the
        source, git is missing, or the clone failed.
        """
        return await self._controllers.plugins.preview(
            source=args.source,
            branch=args.branch,
            subdir=args.subdir,
        )

    @rpc(RpcMethod.SET_PLUGIN_ENABLED, args=PluginEnabledArgs, scope=RpcScope.SESSION)
    def set_plugin_enabled(self, args: PluginEnabledArgs) -> msg.Info:
        """Switch an installed plugin on or off.

        ``name`` is the installed plugin; ``enabled`` its new state. The
        choice is saved and this session reloads its plugins. Returns an
        ``info`` message, sent in place of an ``rpc_response``, whose text
        says what happened — including a refusal: no such plugin, or one
        the organisation manages.
        """
        return self._controllers.plugins.set_enabled(args.name, args.enabled)

    @rpc(RpcMethod.INSTALL_PLUGIN, args=InstallPluginArgs, scope=RpcScope.SESSION)
    async def install_plugin(self, args: InstallPluginArgs) -> msg.Info:
        """Install a plugin from a git repository or a marketplace.

        ``ref`` is a git URL or ``@<marketplace>/<plugin>``; ``install_ref``
        the branch, tag or commit to check out (the marketplace's, or the
        default branch, when absent). The plugin is active in this session
        at once. Returns an ``info`` message, sent in place of an
        ``rpc_response``, whose text says what was installed — or why not:
        git missing, an unknown marketplace entry, the organisation's
        policy, a git error.
        """
        return await self._controllers.plugins.install(args.ref, args.install_ref)

    @rpc(RpcMethod.UPDATE_PLUGIN, args=UpdatePluginArgs, scope=RpcScope.SESSION)
    async def update_plugin(self, args: UpdatePluginArgs) -> msg.Info:
        """Move an installed plugin to another commit.

        ``name`` is the installed plugin; ``install_ref`` the branch, tag
        or commit to move to (the remote's default branch when absent).
        Returns an ``info`` message, sent in place of an ``rpc_response``,
        whose text names the commit now installed, or why not.
        """
        return await self._controllers.plugins.update(args.name, args.install_ref)

    @rpc(RpcMethod.REMOVE_PLUGIN, args=PluginNameArgs, scope=RpcScope.SESSION)
    async def remove_plugin(self, args: PluginNameArgs) -> msg.Info:
        """Uninstall a plugin.

        ``name`` is the installed plugin. Returns an ``info`` message, sent
        in place of an ``rpc_response``, whose text says it was removed, or
        why not.
        """
        return await self._controllers.plugins.remove(args.name)

    @rpc(RpcMethod.GET_MARKETPLACES, args=NoArgs, scope=RpcScope.IGNI)
    def get_marketplaces(self, args: NoArgs) -> list[MarketplaceInfo]:
        """List the plugin marketplaces igni knows, with their catalogues.

        Takes no arguments. Returns each marketplace's name, URL, when it
        was last fetched, and the plugins it offers.
        """
        return self._ctx.backend.get_marketplaces()

    @rpc(RpcMethod.ADD_MARKETPLACE, args=MarketplaceUrlArgs, scope=RpcScope.IGNI)
    async def add_marketplace(self, args: MarketplaceUrlArgs) -> msg.Info:
        """Add a plugin marketplace and read its catalogue.

        ``url`` is the marketplace's git URL. Returns an ``info`` message,
        sent in place of an ``rpc_response``, whose text says how many
        plugins it catalogues — or why it was not added: the
        organisation's policy, a git error, an unreadable catalogue.
        """
        return await self._controllers.marketplaces.add(args.url)

    @rpc(RpcMethod.REMOVE_MARKETPLACE, args=MarketplaceNameArgs, scope=RpcScope.IGNI)
    async def remove_marketplace(self, args: MarketplaceNameArgs) -> msg.Info:
        """Forget a plugin marketplace.

        ``name`` is the marketplace. Returns an ``info`` message, sent in
        place of an ``rpc_response``, whose text says it was removed, or
        that there is no such marketplace.
        """
        return await self._controllers.marketplaces.remove(args.name)

    @rpc(RpcMethod.REFRESH_MARKETPLACES, args=RefreshMarketplacesArgs, scope=RpcScope.IGNI)
    async def refresh_marketplaces(self, args: RefreshMarketplacesArgs) -> msg.Info:
        """Fetch marketplaces' catalogues again.

        ``name`` is one marketplace; absent or empty refreshes every one.
        Returns an ``info`` message, sent in place of an ``rpc_response``,
        whose text says what was refreshed, or why not.
        """
        return await self._controllers.marketplaces.refresh(args.name)
