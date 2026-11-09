"""The parts of an installed extension that run here.

The installer decides *whether* a bundle may be installed and records
what happened; this decides *what runs*: the process and its
environment, the service igni connects to, the pages it serves and the
plugin it carries. It is the coordinator over those four
collaborators, and it owns the order they happen in — which is the
part that is easy to get wrong and impossible to read off a list of
methods.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from igni_core.core.mcp.config import MCPServerConfig
from igni_core.extensions.install_contributions import ExtensionPages, ExtensionPlugin
from igni_core.extensions.install_environment import ExtensionEnvironment, ProcessEnvironment
from igni_core.extensions.install_process import ExtensionProcess
from igni_core.extensions.install_service import ExtensionConnection
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.permissions import Permissions
from igni_core.extensions.sandbox import Confinement
from igni_core.extensions.store import DistributionRecord

logger = logging.getLogger(__name__)


class PreparedExtension(BaseModel):
    """An extension that is packaged and ready to start.

    What the packaging steps produced, in one value: how to reach its
    service, the environment its process starts with, what it is
    allowed to do, and which service it belongs to.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config: MCPServerConfig
    environment: ProcessEnvironment
    permissions: Permissions
    service_url: str | None = None


class ServiceStart(BaseModel):
    """What starting an extension's service produced.

    ``reason`` is why it did not start, and is ``None`` when it did.
    ``problems`` are the things the extensions panel shows about a
    service that did start — a tool that reports hints the manifest
    does not agree with.
    """

    reason: str | None = None
    problems: list[str] = Field(default_factory=list)


class ExtensionRuntime:
    """What runs for an installed extension, and in which order."""

    def __init__(
        self,
        *,
        environment: ExtensionEnvironment,
        process: ExtensionProcess,
        connection: ExtensionConnection,
        pages: ExtensionPages,
        plugin: ExtensionPlugin,
    ) -> None:
        """
        Args:
            environment: The scope the extension is given and the
                environment its process starts with.
            process: What igni runs for it, and where.
            connection: Its MCP service.
            pages: Its pages, on an origin of their own.
            plugin: The plugin it carries.
        """
        self._environment = environment
        self._process = process
        self._connection = connection
        self._pages = pages
        self._plugin = plugin

    def permissions(self, manifest: ExtensionManifest) -> Permissions:
        """What this extension is allowed to do."""
        return self._environment.permissions(manifest)

    async def prepare(
        self,
        manifest: ExtensionManifest,
        bundle: Path,
        permissions: Permissions,
        distribution: DistributionRecord | None,
    ) -> PreparedExtension:
        """Package it: its assets, its package, its environment, its sandbox.

        Raises:
            AssetError: An asset it ships is not on this machine.
            InstallError: Its packaging cannot be honoured here.
            PermissionDenied: A grant it holds cannot be delivered.
            PythonEnvironmentError: Its package could not be installed.
        """
        assets_dir = await self._process.lay_out_assets(manifest, bundle, distribution)
        entry_point = await self._process.python_entry(manifest, bundle, distribution)
        service_url = self._environment.service_url(manifest, distribution)
        environment = await self._environment.build(manifest, permissions, service_url, assets_dir)
        config = self._connection.config_for(
            manifest, bundle, permissions, entry_point, environment
        )
        config = await self._process.confine(
            manifest,
            config,
            bundle=bundle,
            data_dir=environment.data_dir,
            service_url=service_url,
        )
        return PreparedExtension(
            config=config,
            environment=environment,
            permissions=permissions,
            service_url=service_url,
        )

    async def start(self, manifest: ExtensionManifest, prepared: PreparedExtension) -> ServiceStart:
        """Start the service, and say what it may offer."""
        reason = await self._connection.start(prepared.config)
        if reason is not None:
            return ServiceStart(reason=reason)
        server = prepared.config.name
        self._connection.restrict_tools(manifest, server, prepared.permissions)
        problems = await self._connection.apply_hints(manifest, server, prepared.permissions)
        return ServiceStart(problems=problems)

    async def publish(
        self,
        manifest: ExtensionManifest,
        bundle: Path,
        prepared: PreparedExtension,
    ) -> str | None:
        """Offer it: its calls, its pages, its agents.

        The calls are bound first, so an extension whose pages cannot
        be served is unbound again rather than left half-published.

        Returns a reason on failure, or ``None`` when it was published.
        """
        self._connection.bind(manifest.id, prepared.config.name)
        reason = await self._pages.serve(
            manifest, bundle, prepared.permissions, prepared.service_url
        )
        if reason is not None:
            self._connection.unbind(manifest.id)
            return reason
        self._plugin.install(manifest, bundle)
        self._plugin.restrict(manifest)
        return None

    async def abandon(self, extension_id: str) -> None:
        """Give up on an extension that did not start.

        The tokens it was given and the sandbox holding it are let go;
        what the registry says about it is the installer's business.
        """
        self._environment.revoke(extension_id)
        await self._process.release(extension_id)

    async def stop(self, extension_id: str) -> None:
        """Stop everything this extension runs.

        In reverse: unbind first so nothing new is routed, then stop
        the service, then forget it. Doing it the other way leaves a
        window where a call is authorised against a registry entry
        whose service has already gone.
        """
        self._connection.unbind(extension_id)
        await self._pages.drop(extension_id)
        await self._connection.disconnect(extension_id)
        self._plugin.remove(extension_id)
        self._plugin.release(extension_id)
        self._connection.forget_hints(extension_id)
        await self.abandon(extension_id)

    def confinement(self, extension_id: str) -> Confinement | None:
        """How this extension's process runs: confined, or not and why."""
        return self._process.status(extension_id)
