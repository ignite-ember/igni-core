"""Turning a bundle on disk into something that answers.

This is the keystone the rest of the extension system was waiting
on. The manifest was modelled, the namespace was routed and
authorised, the frame was built and the floor was published — and
nothing anywhere called ``ExtensionRegistry.install``, so every one
of those was reachable only from a test.

What an install does, in order, and why that order:

1. **Read and validate the manifest.** Anything wrong here is wrong
   before we have touched the machine.
2. **Check trust**, which is a question about where the bundle came
   from. One igni server distributed — its signature verified against
   the approved service's key before it was stored — is trusted. Any
   other is a development install, refused unless the person at this
   machine set ``IGNI_DEV_EXTENSIONS``.
3. **Register as PREPARING.** From here on a failure is *recorded*
   rather than raised away: an extension that failed to start is a
   state an administrator needs to see, with the reason and the fix,
   not an exception in a log nobody reads.
4. **Start or reach the service**, then bind it.
5. **READY**, which is what makes it callable — the registry refuses
   anything else.

Four packagings, one per manifest. ``python`` installs a package into
a private environment and runs it here, ``dev`` runs a command here,
``endpoint`` reaches a service somebody else runs, and ``sidecar``
needs a container runtime this does not have yet and fails saying so.

A process igni starts that belongs to a service is told how to get a
token for it, and is given a directory of its own to keep what it
needs — that is the environment, and it is
:mod:`igni_core.extensions.install_environment`.

This module is the coordinator and nothing else. What an install
decides is in :mod:`igni_core.extensions.install_preflight`; what it
runs is in :mod:`igni_core.extensions.install_runtime`, which composes
the process, its environment, its service, its pages and its plugin.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from igni_core.core.agents.tool_audience import ToolAudience
from igni_core.core.mcp.hints import ToolHintsRegistry
from igni_core.extensions.assets import AssetError, AssetSource
from igni_core.extensions.install_context import InstallContext
from igni_core.extensions.install_contributions import ExtensionPages, ExtensionPlugin
from igni_core.extensions.install_environment import ExtensionEnvironment
from igni_core.extensions.install_errors import InstallError
from igni_core.extensions.install_preflight import InstallPreflight
from igni_core.extensions.install_process import ExtensionProcess
from igni_core.extensions.install_runtime import ExtensionRuntime
from igni_core.extensions.install_service import ExtensionConnection
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.mcp_manager import McpManager
from igni_core.extensions.permissions import PermissionDenied
from igni_core.extensions.profile import DeploymentProfile
from igni_core.extensions.python_env import PythonEnvironmentError, PythonEnvironments
from igni_core.extensions.registry import (
    ExtensionRegistry,
    ExtensionState,
    InstalledExtension,
)
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.extensions.sandbox import ExtensionSandbox
from igni_core.extensions.service_token import ServiceTokenEndpoint
from igni_core.extensions.store import DistributionRecord, ExtensionStore

if TYPE_CHECKING:
    from igni_core.extensions.host.channel import HostChannel
    from igni_core.extensions.origin import ExtensionOrigins

logger = logging.getLogger(__name__)


class ExtensionInstaller:
    """Installs, starts and removes extensions."""

    def __init__(
        self,
        *,
        registry: ExtensionRegistry,
        rpc: ExtensionRpc,
        mcp: McpManager,
        origins: ExtensionOrigins | None = None,
        project_dir: Path | None = None,
        secret_resolver: Callable[[str], str | None] | None = None,
        profile: DeploymentProfile | None = None,
        plugins_dir: Path | None = None,
        audience: ToolAudience | None = None,
        allow_development: bool = False,
        store: ExtensionStore | None = None,
        environments: PythonEnvironments | None = None,
        service_tokens: ServiceTokenEndpoint | None = None,
        host_channel: HostChannel | None = None,
        tool_hints: ToolHintsRegistry | None = None,
        asset_source: AssetSource | None = None,
        sandbox: ExtensionSandbox | None = None,
    ) -> None:
        """
        Args:
            mcp: The MCP client manager this extension's service is
                reached through
                (:class:`~igni_core.extensions.install_service.McpManager`).
            origins: Where an extension's UI is served from. Each
                gets its own loopback port, because a port is part of
                an origin and a path prefix is not — two extensions
                sharing one origin could read each other's storage
                and script each other's frames. Optional: an
                extension that contributes no pages needs none, and
                neither does a headless deployment.
            project_dir: The user's project. Told to a service only
                when ``permissions.workspace`` grants it.
            secret_resolver: The deployment's secret source. A name
                the manifest did not declare is never passed to it.
            profile: How many principals this backend serves. A
                sidecar is one process, so a shared backend refuses
                one — see :mod:`igni_core.extensions.profile`.
            allow_development: Whether a bundle igni server did not
                distribute may be installed — ``IGNI_DEV_EXTENSIONS``.
                False everywhere but development, because the easiest
                way to ship must not be the one with no provenance.
            store: Where a development install's settings are read,
                for the ``serviceUrl`` its token is fetched for.
            environments: Where ``python`` packaging is installed.
                Without one such an extension cannot start, and says so.
            service_tokens: The loopback endpoint an extension fetches
                its service's token from. Without one, no extension is
                told how to reach its service as the signed-in person.
            host_channel: The loopback socket an extension's process
                reaches the host object model on (``ext.host``). Without
                one, a process is not told where igni is.
            tool_hints: Where its tools' hints go for the permission
                modes to read. The process's shared registry by default.
            asset_source: Where a development install's missing asset
                is fetched from — its manifest's ``source``. A
                distributed one never is: its assets came from igni.
            sandbox: What every process this starts runs inside
                (:mod:`igni_core.extensions.sandbox`). One reading the
                managed, env and user settings by default, with no air
                gap; the backend passes one built from its settings.
        """
        context = InstallContext(
            profile=profile if profile is not None else DeploymentProfile.current(),
            allow_development=allow_development,
            project_dir=project_dir,
            secret_resolver=secret_resolver,
        )
        self._registry = registry
        self._preflight = InstallPreflight(context=context)
        self._runtime = ExtensionRuntime(
            environment=ExtensionEnvironment(
                context=context,
                service_tokens=service_tokens,
                host_channel=host_channel,
                store=store,
            ),
            process=ExtensionProcess(
                context=context,
                asset_source=asset_source,
                environments=environments,
                store=store,
                sandbox=sandbox,
            ),
            connection=ExtensionConnection(mcp=mcp, rpc=rpc, tool_hints=tool_hints),
            pages=ExtensionPages(origins=origins, context=context),
            plugin=ExtensionPlugin(plugins_dir=plugins_dir, audience=audience),
        )

    async def install(
        self, bundle: Path, *, distribution: DistributionRecord | None = None
    ) -> InstalledExtension:
        """Install the bundle at ``bundle`` and try to start it.

        Returns the registry entry either way. A started extension is
        ``READY``; one that could not start is ``FAILED`` with a
        reason, which is a state, not an error.

        Args:
            distribution: How igni server distributed this bundle, when
                it did. ``None`` makes it a development install.

        Raises:
            InstallError: The bundle is not installable at all — bad
                manifest, refused trust, or an id already installed.
        """
        manifest = self._read(bundle)
        self._preflight.check(manifest, distribution)

        if self._registry.get(manifest.id) is not None:
            raise InstallError(
                f"{manifest.id} is already installed. Remove it first — "
                "an upgrade keeps the old grants until an administrator "
                "accepts the new ones, and that is a separate decision."
            )

        entry = self._registry.install(
            manifest,
            state=ExtensionState.PREPARING,
            reason="starting its service",
        )
        permissions = self._runtime.permissions(manifest)

        try:
            prepared = await self._runtime.prepare(manifest, bundle, permissions, distribution)
        except (
            AssetError,
            InstallError,
            PermissionDenied,
            PythonEnvironmentError,
        ) as exc:
            # Packaging we cannot honour. Recorded rather than raised:
            # the extension is installed and not runnable here, which
            # is exactly what an administrator needs to be shown.
            await self._runtime.abandon(manifest.id)
            self._registry.set_state(manifest.id, ExtensionState.FAILED, str(exc))
            return entry

        confinement = self._runtime.confinement(manifest.id)
        if confinement is not None:
            self._registry.set_sandbox(manifest.id, confinement.describe())

        started = await self._runtime.start(manifest, prepared)
        if started.reason is not None:
            await self._runtime.abandon(manifest.id)
            self._registry.set_state(manifest.id, ExtensionState.FAILED, started.reason)
            return entry

        self._registry.set_problems(manifest.id, started.problems)
        self._note_unsandboxed(manifest)

        reason = await self._runtime.publish(manifest, bundle, prepared)
        if reason is not None:
            # The service works and the UI cannot be shown. FAILED
            # rather than READY: a page in the sidebar that opens on
            # nothing is worse than one that is not offered.
            self._registry.set_state(manifest.id, ExtensionState.FAILED, reason)
            return entry

        self._registry.set_state(manifest.id, ExtensionState.READY)
        logger.info("extension %s is READY", manifest.id)
        return entry

    async def uninstall(self, extension_id: str) -> bool:
        """Stop and forget an extension. ``True`` if there was one.

        Order matters here too, in reverse: unbind first so nothing
        new is routed, then stop the service, then forget it. Doing
        it the other way leaves a window where a call is authorised
        against a registry entry whose service has already gone.
        """
        if self._registry.get(extension_id) is None:
            return False
        await self._runtime.stop(extension_id)
        self._registry.remove(extension_id)
        logger.info("extension %s removed", extension_id)
        return True

    def _note_unsandboxed(self, manifest: ExtensionManifest) -> None:
        """A process unconfined for want of a sandbox is a problem the panel shows.

        Turned off by ``extensions.sandbox: false`` it is a choice, and
        :attr:`InstalledExtension.sandbox` says so without calling it a
        problem. Not a failure either way — no sandbox here is a
        machine's state, and
        ``extensions.require_sandbox`` is how an administrator makes it
        one — but the person should see that this extension's process
        has their full file and network access.
        """
        confinement = self._runtime.confinement(manifest.id)
        if confinement is None or confinement.confined or confinement.deliberate:
            return
        entry = self._registry.get(manifest.id)
        problems = list(entry.problems) if entry is not None else []
        problems.append(
            f"its process is not sandboxed ({confinement.reason}), so it has this "
            "person's full file and network access rather than its grants"
        )
        self._registry.set_problems(manifest.id, problems)

    @staticmethod
    def _read(bundle: Path) -> ExtensionManifest:
        path = bundle / "manifest.json"
        if not path.is_file():
            raise InstallError(f"{bundle} has no manifest.json, so there is nothing to install.")
        try:
            return ExtensionManifest.load(path)
        except ValueError as exc:
            raise InstallError(f"{path} is not a valid manifest: {exc}") from exc
