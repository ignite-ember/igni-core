"""The process igni starts for an extension.

Three concerns, all about the same process: the assets it ships and
runs, the package it is installed from, and the sandbox it runs
inside. What the process is *told* is a different question — that is
the environment (:mod:`igni_core.extensions.install_environment`), and
what igni *reaches* is the service
(:mod:`igni_core.extensions.install_service`).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from igni_core.core.mcp.config import MCPServerConfig
from igni_core.extensions.assets import (
    AssetLayout,
    AssetPlatform,
    AssetSource,
    ExtensionAssets,
)
from igni_core.extensions.install_context import InstallContext
from igni_core.extensions.install_errors import InstallError
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.provenance import DistributionRecord
from igni_core.extensions.python_env import PythonEnvironments
from igni_core.extensions.sandbox import Confinement, ExtensionSandbox
from igni_core.extensions.store import ExtensionStore

logger = logging.getLogger(__name__)


class ExtensionProcess:
    """What igni runs for an extension, and where it is allowed to run."""

    def __init__(
        self,
        *,
        context: InstallContext,
        asset_source: AssetSource | None = None,
        environments: PythonEnvironments | None = None,
        store: ExtensionStore | None = None,
        sandbox: ExtensionSandbox | None = None,
    ) -> None:
        """
        Args:
            context: The deployment this install happens in.
            asset_source: Where a development install's missing asset
                is fetched from — its manifest's ``source``. A
                distributed one never is: its assets came from igni.
            environments: Where ``python`` packaging is installed.
                Without one such an extension cannot start, and says so.
            store: Where the assets this machine already holds are kept.
            sandbox: What every process this starts runs inside
                (:mod:`igni_core.extensions.sandbox`). One reading the
                managed, env and user settings by default, with no air
                gap; the backend passes one built from its settings.
        """
        self._context = context
        self._asset_source = asset_source or AssetSource()
        self._environments = environments
        self._store = store
        self._sandbox = sandbox if sandbox is not None else ExtensionSandbox()

    async def lay_out_assets(
        self,
        manifest: ExtensionManifest,
        bundle: Path,
        distribution: DistributionRecord | None,
    ) -> Path | None:
        """Its assets for this platform, checked and laid out; where.

        A distributed extension's assets arrived with it from igni
        server; one missing here is a failure, never a download from
        anywhere else. A development install fetches a missing one
        from its ``source``, held to the same digest.
        """
        if not manifest.assets:
            return None
        platform = AssetPlatform.current()
        if platform is None:
            raise InstallError(
                f"{manifest.id} ships what it runs for "
                f"{', '.join(sorted({a.platform for a in manifest.assets}))}, and this "
                "machine is none of them"
            )
        mine = ExtensionAssets.for_platform(manifest.assets, platform)
        cache = (self._store or ExtensionStore()).asset_cache
        for asset in mine:
            if cache.holds(asset):
                continue
            if distribution is not None:
                raise InstallError(
                    f"asset {asset.name} ({asset.platform}) of {manifest.id} is not on this "
                    "machine; the next sync with igni server downloads it"
                )
            logger.info("fetching asset %s of %s from %s", asset.name, manifest.id, asset.source)
            await asyncio.to_thread(self._asset_source.fetch, asset, cache)
        return await asyncio.to_thread(AssetLayout(cache).lay_out, bundle, mine)

    async def python_entry(
        self,
        manifest: ExtensionManifest,
        bundle: Path,
        distribution: DistributionRecord | None,
    ) -> Path | None:
        """The installed console script, for ``python`` packaging.

        Built once per extension and version; a development install
        may build it from the bundle directory, a distributed one only
        from the wheels it carries.
        """
        if manifest.python is None:
            return None
        if self._environments is None:
            raise InstallError(
                f"{manifest.id} is a Python package and this backend has nowhere "
                "to install one, so it cannot start here."
            )
        return await self._environments.ensure(manifest, bundle, development=distribution is None)

    async def confine(
        self,
        manifest: ExtensionManifest,
        config: MCPServerConfig,
        *,
        bundle: Path,
        data_dir: Path | None,
        service_url: str | None,
    ) -> MCPServerConfig:
        """``config`` as the process runs it, confined to its grants.

        Every process igni starts for an extension starts confined to
        its grants, or — where there is no sandbox — says so.
        """
        return await self._sandbox.confine(
            manifest,
            config,
            bundle=bundle,
            data_dir=data_dir,
            project_dir=self._context.project_dir,
            service_url=service_url,
            development=self._context.allow_development,
        )

    async def release(self, extension_id: str) -> None:
        """Let go of whatever the sandbox is holding for this extension."""
        await self._sandbox.release(extension_id)

    def status(self, extension_id: str) -> Confinement | None:
        """How this extension's process runs: confined, or not and why."""
        return self._sandbox.status(extension_id)
