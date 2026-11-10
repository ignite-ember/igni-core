"""Keeps this machine's extensions in step with igni's listing.

One pass: list, update what differs, remove what is gone. The order
matters — the priority order first, so an extension installed below
starts with its collisions already settled; and the old version keeps
running until the new one is stored, so a download that fails costs an
update and never the extension that works.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import httpx

from igni_core.core.activity import background
from igni_core.core.config.endpoint import is_configured
from igni_core.extensions.assets import AssetPlatform, ExtensionAssets
from igni_core.extensions.distribution.bundle import BundleVerifier
from igni_core.extensions.distribution.client import DistributionClient
from igni_core.extensions.distribution.errors import DistributionError
from igni_core.extensions.distribution.schemas import DistributedExtension, SyncReport
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.provenance import DistributionRecord
from igni_core.extensions.service_token import IgniSession
from igni_core.extensions.store import ExtensionStore
from igni_core.extensions.store_errors import StoreError

logger = logging.getLogger(__name__)


class ExtensionLifecycle(Protocol):
    """Starting and stopping an extension in the running backend."""

    async def activate(self, extension_id: str, bundle: Path) -> None: ...

    async def deactivate(self, extension_id: str) -> None: ...


class ExtensionDistribution:
    """Keeps this machine's extensions in step with igni's listing."""

    def __init__(
        self,
        store: ExtensionStore,
        *,
        api_url: Callable[[], str | None],
        session: Callable[[], IgniSession | None],
        lifecycle: ExtensionLifecycle | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        on_priority: Callable[[], None] | None = None,
    ) -> None:
        """
        Args:
            on_priority: Told when the priority order changed, after
                it is stored.
        """
        self._store = store
        self._api_url = api_url
        self._session = session
        self._lifecycle = lifecycle
        self._on_priority = on_priority
        self._transport = transport
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[SyncReport | None]] = set()

    def schedule(self) -> asyncio.Task[SyncReport | None]:
        """Sync in the background. Nothing waits for it.

        Two calls close together run one after the other rather than
        at once: a sign-in during the start-up sync must still see the
        listing for the person who just signed in.
        """
        task = background.start(
            self._sync_logged(), holder="extension-distribution", reason="syncing extensions"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def stop(self) -> None:
        """Cancel the syncs in flight and wait for them to let go."""
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _sync_logged(self) -> SyncReport | None:
        """One sync, logged; a failure is never the caller's problem."""
        try:
            report = await self.sync()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a background sync must not take anything down
            logger.exception("extension distribution sync failed")
            return None
        if report.skipped:
            logger.info("extension distribution skipped: %s", report.skipped)
        elif report.installed or report.removed or report.failed:
            logger.info(
                "extension distribution: installed %s, removed %s, failed %s",
                report.installed,
                report.removed,
                report.failed,
            )
        return report

    async def sync(self) -> SyncReport:
        """One pass: list, update what differs, remove what is gone."""
        async with self._lock:
            session = self._session()
            if session is None:
                return SyncReport(skipped="nobody is signed in to igni")
            api_url = self._api_url()
            if not is_configured(api_url):
                return SyncReport(skipped="no igni server is configured")
            assert api_url is not None

            client = DistributionClient(api_url, session, transport=self._transport)
            try:
                listing = await client.listing()
            except DistributionError as exc:
                # Nothing is removed on a failed listing: "igni did not
                # answer" is not "the service is gone".
                logger.warning("extension listing failed, keeping what is installed: %s", exc)
                return SyncReport(skipped=str(exc))

            report = SyncReport()
            entitled = listing.entitled()
            self._store.listing.record_group(listing.group, name=listing.group_name)
            # The priority first: an extension installed below starts
            # with its collisions already settled.
            if self._store.listing.record_priority(listing.priority):
                report.priority_changed = True
                if self._on_priority is not None:
                    self._on_priority()
            for entry in entitled:
                await self._bring_up_to_date(client, entry, report)
            # Switched off for the person's group is gone, as a removed
            # service is.
            await self._remove_unlisted({entry.extension_id for entry in entitled}, report)
            self._store.prune_assets()
            return report

    async def _bring_up_to_date(
        self, client: DistributionClient, entry: DistributedExtension, report: SyncReport
    ) -> None:
        """One listed extension: install it, or leave it as it is."""
        extension_id = entry.extension_id
        try:
            installed_at = self._store.path_for(extension_id)
        except StoreError as exc:
            report.failed[extension_id] = str(exc)
            return
        record = self._store.provenance.of(extension_id)

        if record is None and installed_at.is_dir():
            logger.info(
                "extension %s is installed for development; the version igni "
                "distributes (%s) is not replacing it",
                extension_id,
                entry.version,
            )
            report.development.append(extension_id)
            return

        if (
            record is not None
            and installed_at.is_dir()
            and (record.version, record.sha256.lower()) == (entry.version, entry.sha256.lower())
        ):
            if record.service_url != entry.service_url:
                # Same bundle, service moved: the token audience
                # changes, so the process is restarted with the new one.
                self._store.provenance.record(self._record(entry))
                await self._restart(extension_id, installed_at)
            report.unchanged.append(extension_id)
            return

        try:
            await self._install(client, entry)
        except DistributionError as exc:
            logger.warning(
                "extension %s %s was not installed: %s", extension_id, entry.version, exc
            )
            report.failed[extension_id] = str(exc)
            return
        report.installed.append(extension_id)

    async def _install(self, client: DistributionClient, entry: DistributedExtension) -> None:
        """Download, verify, store and start one extension."""
        downloaded = await client.bundle(entry.extension_id)
        archive = BundleVerifier.verify(entry, downloaded)

        with tempfile.TemporaryDirectory(prefix="igni-ext-") as scratch:
            staging = Path(scratch) / "bundle"
            archive.extract(staging)
            try:
                manifest = self._store.read(staging)
            except StoreError as exc:
                raise DistributionError(str(exc)) from exc
            if (manifest.id, manifest.version) != (entry.extension_id, entry.version):
                raise DistributionError(
                    f"the bundle's manifest is {manifest.id} {manifest.version}, and "
                    f"its signature is for {entry.extension_id} {entry.version}"
                )
            if manifest.is_development_command:
                raise DistributionError(
                    f"{manifest.id} uses 'dev' packaging, a command out of its "
                    "manifest, which igni does not install from a service"
                )
            # Before the old version stops: a download that fails leaves
            # the one that works running.
            await self._fetch_assets(client, manifest)

            # Stopped before its files are replaced: a running process
            # whose environment is deleted under it fails in ways that
            # are no fun to read about.
            if self._lifecycle is not None:
                await self._lifecycle.deactivate(entry.extension_id)
            try:
                target = self._store.add(staging, replace=True, distribution=self._record(entry))
            except (StoreError, OSError) as exc:
                # The store keeps the old bundle when a replace fails,
                # so the version that worked is started again.
                previous = self._store.path_for(entry.extension_id)
                if self._lifecycle is not None and previous.is_dir():
                    await self._lifecycle.activate(entry.extension_id, previous)
                raise DistributionError(f"storing it failed: {exc}") from exc

        logger.info(
            "extension %s %s installed from %s",
            entry.extension_id,
            entry.version,
            entry.service_url,
        )
        if self._lifecycle is not None:
            await self._lifecycle.activate(entry.extension_id, target)

    async def _fetch_assets(self, client: DistributionClient, manifest: ExtensionManifest) -> None:
        """This machine's assets of ``manifest``, each one not here already."""
        if not manifest.assets:
            return
        platform = AssetPlatform.current()
        if platform is None:
            raise DistributionError(
                f"{manifest.id} ships assets for "
                f"{', '.join(sorted({a.platform for a in manifest.assets}))}, and this "
                "machine is none of the platforms igni runs on"
            )
        cache = self._store.asset_cache
        for asset in ExtensionAssets.for_platform(manifest.assets, platform):
            if cache.holds(asset):
                continue
            logger.info(
                "downloading asset %s (%s) of %s, %d bytes",
                asset.name,
                asset.platform,
                manifest.id,
                asset.size,
            )
            await client.asset(manifest.id, asset, cache)

    async def _restart(self, extension_id: str, bundle: Path) -> None:
        """Stop and start an extension whose service moved."""
        if self._lifecycle is None:
            return
        await self._lifecycle.deactivate(extension_id)
        await self._lifecycle.activate(extension_id, bundle)

    async def _remove_unlisted(self, listed: set[str], report: SyncReport) -> None:
        """Every installed extension igni no longer lists."""
        for record in self._store.provenance.records():
            if record.extension_id in listed:
                continue
            if self._lifecycle is not None:
                await self._lifecycle.deactivate(record.extension_id)
            self._store.remove(record.extension_id)
            logger.info(
                "extension %s removed: its service (%s) is no longer listed",
                record.extension_id,
                record.service_url,
            )
            report.removed.append(record.extension_id)

    @staticmethod
    def _record(entry: DistributedExtension) -> DistributionRecord:
        """The provenance record an installed extension is kept under."""
        return DistributionRecord(
            extension_id=entry.extension_id,
            version=entry.version,
            sha256=entry.sha256.lower(),
            service_url=entry.service_url,
            published_at=entry.published_at,
        )


__all__ = ["ExtensionDistribution", "ExtensionLifecycle"]
