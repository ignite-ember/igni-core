"""``host.plugins``: the installed plugins, the marketplaces, and what a
plugin carries."""

from __future__ import annotations

from igni_core.backend.plugin_schemas import PluginContents
from igni_core.core.plugins.models import MarketplaceInfo, PluginInfo
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.reads import read_answer, read_answers
from igni_core.extensions.host.members.tag import call, get
from igni_core.extensions.host.rpc_wire.settings import (
    InstallPluginArgs,
    PluginEnabledArgs,
    PluginNameArgs,
    PreviewPluginArgs,
    RefreshMarketplacesArgs,
    UpdatePluginArgs,
    UrlArgs,
)
from igni_core.protocol.rpc import RpcMethod


class PluginMembers(Members):
    @get("plugins.installed")
    async def installed(self, c: Call) -> list[PluginInfo]:
        return read_answers(
            PluginInfo,
            await self.rpc(RpcMethod.GET_PLUGIN_DETAILS, c=c),
            method="plugins.installed",
        )

    @get("plugins.marketplaces")
    async def marketplaces(self, c: Call) -> list[MarketplaceInfo]:
        return read_answers(
            MarketplaceInfo,
            await self.rpc(RpcMethod.GET_MARKETPLACES, c=c),
            method="plugins.marketplaces",
        )

    @call("plugins.contents")
    async def contents(self, c: Call) -> PluginContents:
        args = PluginNameArgs(name=c.params["name"])
        return read_answer(
            PluginContents,
            await self.rpc(RpcMethod.GET_PLUGIN_CONTENTS, args, c=c),
            method="plugins.contents",
        )

    @call("plugins.preview")
    async def preview(self, c: Call) -> PluginContents:
        args = PreviewPluginArgs(
            source=c.params["source"],
            branch=c.params.get("branch"),
            subdir=c.params.get("subdir"),
        )
        return read_answer(
            PluginContents,
            await self.rpc(RpcMethod.PREVIEW_PLUGIN, args, c=c),
            method="plugins.preview",
        )

    @call("plugins.install")
    async def install(self, c: Call) -> None:
        args = InstallPluginArgs(ref=c.params["ref"], install_ref=c.params.get("install_ref"))
        await self.rpc_checked(RpcMethod.INSTALL_PLUGIN, args, c=c)

    @call("plugins.update")
    async def update(self, c: Call) -> None:
        args = UpdatePluginArgs(name=c.params["name"], install_ref=c.params.get("install_ref"))
        await self.rpc_checked(RpcMethod.UPDATE_PLUGIN, args, c=c)

    @call("plugins.remove")
    async def remove(self, c: Call) -> None:
        await self.rpc_checked(RpcMethod.REMOVE_PLUGIN, PluginNameArgs(name=c.params["name"]), c=c)

    @call("plugins.set_enabled")
    async def set_enabled(self, c: Call) -> None:
        args = PluginEnabledArgs(name=c.params["name"], enabled=bool(c.params["on"]))
        await self.rpc_checked(RpcMethod.SET_PLUGIN_ENABLED, args, c=c)

    @call("plugins.add_marketplace")
    async def add_marketplace(self, c: Call) -> None:
        await self.rpc_checked(RpcMethod.ADD_MARKETPLACE, UrlArgs(url=c.params["url"]), c=c)

    @call("plugins.remove_marketplace")
    async def remove_marketplace(self, c: Call) -> None:
        args = PluginNameArgs(name=c.params["name"])
        await self.rpc_checked(RpcMethod.REMOVE_MARKETPLACE, args, c=c)

    @call("plugins.refresh_marketplaces")
    async def refresh_marketplaces(self, c: Call) -> None:
        args = RefreshMarketplacesArgs(name=c.params.get("name"))
        await self.rpc_checked(RpcMethod.REFRESH_MARKETPLACES, args, c=c)


__all__ = ["PluginMembers"]
