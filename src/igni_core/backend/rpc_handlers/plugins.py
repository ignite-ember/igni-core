"""Plugin + marketplace RPC handlers."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from igni_core.backend.plugin_schemas import PluginContents
from igni_core.backend.rpc_binding import NoArgs, rpc
from igni_core.backend.rpc_handlers.base import RpcHandler
from igni_core.core.plugins.models import MarketplaceInfo, PluginInfo
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcDuration, RpcMethod, RpcScope


class PluginArgs(BaseModel):
    """Base for every plugin RPC's arguments: keys a newer client adds
    are ignored rather than refused."""

    model_config = ConfigDict(extra="ignore")


class PluginNameArgs(PluginArgs):
    """Arguments naming an installed plugin."""

    name: str = Field(description="Name of the installed plugin.")


class PreviewPluginArgs(PluginArgs):
    """Arguments for previewing a plugin from git without installing it."""

    source: str = Field(
        description="The plugin's git URL, optionally followed by a folder inside the repository."
    )
    branch: str | None = Field(
        default=None, description="Branch, tag or commit to read; the default branch when absent."
    )
    subdir: str | None = Field(
        default=None,
        description="The plugin's folder inside the repository; overrides one given in `source`.",
    )


class PluginEnabledArgs(PluginArgs):
    """Arguments for switching an installed plugin on or off."""

    name: str = Field(description="Name of the installed plugin.")
    enabled: bool = Field(description="The plugin's new state: `true` to switch it on.")


class InstallPluginArgs(PluginArgs):
    """Arguments for installing a plugin."""

    ref: str = Field(
        description="A git URL, or `@<marketplace>/<plugin>` for a marketplace plugin."
    )
    install_ref: str | None = Field(
        default=None,
        description=(
            "Branch, tag or commit to check out; when absent, the marketplace's choice "
            "or the default branch."
        ),
    )


class UpdatePluginArgs(PluginArgs):
    """Arguments for moving an installed plugin to another commit."""

    name: str = Field(description="Name of the installed plugin.")
    install_ref: str | None = Field(
        default=None,
        description="Branch, tag or commit to move to; the remote's default branch when absent.",
    )


class MarketplaceUrlArgs(PluginArgs):
    """Arguments for adding a plugin marketplace."""

    url: str = Field(description="The marketplace's git URL.")


class MarketplaceNameArgs(PluginArgs):
    """Arguments naming a plugin marketplace."""

    name: str = Field(description="Name of the marketplace.")


class RefreshMarketplacesArgs(PluginArgs):
    """Arguments for re-reading marketplace catalogues."""

    name: str | None = Field(
        default=None, description="The marketplace to refresh; every marketplace when absent."
    )


class PluginsRpcHandler(RpcHandler):
    """Plugin details / contents / preview / enable-toggle + install
    / update / remove, plus marketplace CRUD + refresh."""

    @rpc(RpcMethod.GET_PLUGIN_DETAILS, duration=RpcDuration.IO, args=NoArgs, scope=RpcScope.SESSION)
    def get_plugin_details(self, args: NoArgs) -> list[PluginInfo]:
        """List the installed plugins.

        Takes no arguments. Returns each plugin's name, version,
        description, where it came from, whether it is enabled (a plugin
        the organisation manages always is), the commit it is pinned to,
        and what it brings: skills, agents, hooks, MCP servers.
        """
        return self._ctx.backend.get_plugin_details()

    @rpc(
        RpcMethod.GET_PLUGIN_CONTENTS,
        duration=RpcDuration.IO,
        args=PluginNameArgs,
        scope=RpcScope.SESSION,
    )
    def get_plugin_contents(self, args: PluginNameArgs) -> PluginContents:
        """Show what an installed plugin contains.

        ``name`` is the installed plugin. Returns its folder and its
        skills, agents, hooks, MCP servers and tools, with its README; or
        ``error`` when no such plugin is installed.
        """
        return self._controllers.plugins.contents(args.name)

    @rpc(
        RpcMethod.PREVIEW_PLUGIN,
        duration=RpcDuration.LONG,
        args=PreviewPluginArgs,
        scope=RpcScope.SESSION,
    )
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

    @rpc(
        RpcMethod.SET_PLUGIN_ENABLED,
        duration=RpcDuration.IO,
        args=PluginEnabledArgs,
        scope=RpcScope.SESSION,
    )
    def set_plugin_enabled(self, args: PluginEnabledArgs) -> msg.Info:
        """Switch an installed plugin on or off.

        ``name`` is the installed plugin; ``enabled`` its new state. The
        choice is saved and this session reloads its plugins. Returns an
        ``info`` message, sent in place of an ``rpc_response``, whose text
        says what happened — including a refusal: no such plugin, or one
        the organisation manages.
        """
        return self._controllers.plugins.set_enabled(args.name, args.enabled)

    @rpc(
        RpcMethod.INSTALL_PLUGIN,
        duration=RpcDuration.LONG,
        args=InstallPluginArgs,
        scope=RpcScope.SESSION,
    )
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

    @rpc(
        RpcMethod.UPDATE_PLUGIN,
        duration=RpcDuration.LONG,
        args=UpdatePluginArgs,
        scope=RpcScope.SESSION,
    )
    async def update_plugin(self, args: UpdatePluginArgs) -> msg.Info:
        """Move an installed plugin to another commit.

        ``name`` is the installed plugin; ``install_ref`` the branch, tag
        or commit to move to (the remote's default branch when absent).
        Returns an ``info`` message, sent in place of an ``rpc_response``,
        whose text names the commit now installed, or why not.
        """
        return await self._controllers.plugins.update(args.name, args.install_ref)

    @rpc(
        RpcMethod.REMOVE_PLUGIN,
        duration=RpcDuration.IO,
        args=PluginNameArgs,
        scope=RpcScope.SESSION,
    )
    async def remove_plugin(self, args: PluginNameArgs) -> msg.Info:
        """Uninstall a plugin.

        ``name`` is the installed plugin. Returns an ``info`` message, sent
        in place of an ``rpc_response``, whose text says it was removed, or
        why not.
        """
        return await self._controllers.plugins.remove(args.name)

    @rpc(RpcMethod.GET_MARKETPLACES, duration=RpcDuration.IO, args=NoArgs, scope=RpcScope.IGNI)
    def get_marketplaces(self, args: NoArgs) -> list[MarketplaceInfo]:
        """List the plugin marketplaces igni knows, with their catalogues.

        Takes no arguments. Returns each marketplace's name, URL, when it
        was last fetched, and the plugins it offers.
        """
        return self._ctx.backend.get_marketplaces()

    @rpc(
        RpcMethod.ADD_MARKETPLACE,
        duration=RpcDuration.LONG,
        args=MarketplaceUrlArgs,
        scope=RpcScope.IGNI,
    )
    async def add_marketplace(self, args: MarketplaceUrlArgs) -> msg.Info:
        """Add a plugin marketplace and read its catalogue.

        ``url`` is the marketplace's git URL. Returns an ``info`` message,
        sent in place of an ``rpc_response``, whose text says how many
        plugins it catalogues — or why it was not added: the
        organisation's policy, a git error, an unreadable catalogue.
        """
        return await self._controllers.marketplaces.add(args.url)

    @rpc(
        RpcMethod.REMOVE_MARKETPLACE,
        duration=RpcDuration.IO,
        args=MarketplaceNameArgs,
        scope=RpcScope.IGNI,
    )
    async def remove_marketplace(self, args: MarketplaceNameArgs) -> msg.Info:
        """Forget a plugin marketplace.

        ``name`` is the marketplace. Returns an ``info`` message, sent in
        place of an ``rpc_response``, whose text says it was removed, or
        that there is no such marketplace.
        """
        return await self._controllers.marketplaces.remove(args.name)

    @rpc(
        RpcMethod.REFRESH_MARKETPLACES,
        duration=RpcDuration.LONG,
        args=RefreshMarketplacesArgs,
        scope=RpcScope.IGNI,
    )
    async def refresh_marketplaces(self, args: RefreshMarketplacesArgs) -> msg.Info:
        """Fetch marketplaces' catalogues again.

        ``name`` is one marketplace; absent or empty refreshes every one.
        Returns an ``info`` message, sent in place of an ``rpc_response``,
        whose text says what was refreshed, or why not.
        """
        return await self._controllers.marketplaces.refresh(args.name)
