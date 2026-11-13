"""Where installed extensions live between restarts.

``ExtensionInstaller`` takes a bundle directory and makes it run. It
does not remember: restart the backend and it has installed nothing,
because nothing wrote the decision down. That is fine for a test and
useless for a deployment, where "installed" has to mean "still
installed tomorrow".

So an install copies the bundle under ``<data>/extensions/<id>/`` and
the backend reads that directory at boot. Copied rather than linked
to where the author happened to leave it: a bundle that lives in
somebody's ``~/Downloads`` is one ``rm`` away from a deployment that
cannot start its extensions, and a signature over a directory
somebody else can edit is not a signature over anything.

**Validation happens here, before the copy.** A directory that is not
an extension should not become one half-written in the install path,
and an administrator should be told what is wrong with the thing they
pointed at rather than with the thing we made from it.

**Where a bundle came from is written down beside it.** A bundle igni
server distributed — its signature checked against the approved
service's key — is recorded in ``<id>.distribution.json``; a bundle
with no record was installed for development. The installer decides
trust from that, and the distribution sync decides what to update
and what to remove from it, so a development install is never
overwritten or removed by a sync.

A ``python``-packaged extension gets a private environment under
``.python/<id>/<version>/``. It belongs to the bundle: replacing or
removing the bundle removes it.

**The store is a directory, not everything about it.** Where a bundle
came from is :mod:`igni_core.extensions.provenance`, what igni
server's last listing carried is :mod:`igni_core.extensions.listing`,
and an extension's settings are
:mod:`igni_core.extensions.extension_settings`. This module owns the
bundles themselves.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from igni_core.core.paths import home_config_dir
from igni_core.extensions.assets import AssetCache, ReadOnlyTree
from igni_core.extensions.extension_settings import ExtensionSettings
from igni_core.extensions.listing import ListingState
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.provenance import DistributionRecord, Provenance
from igni_core.extensions.settings_values import SettingsValues
from igni_core.extensions.store_errors import StoreError, StoreName

logger = logging.getLogger(__name__)

#: Under the data directory. Not configurable yet, and named here so
#: that when it becomes configurable there is one place to change.
EXTENSIONS_DIR = "extensions"


#: Inside the store, and dot-named so :meth:`ExtensionStore.installed`
#: never mistakes it for a bundle.
ENVIRONMENTS_DIR = ".python"

#: Inside the store, dot-named for the same reason: one directory per
#: extension that is its alone, kept across reinstalls.
DATA_DIR = ".data"

#: What an extension's process is told that directory is.
DATA_DIR_ENV = "IGNI_EXTENSION_DATA"

#: Inside the store, dot-named: assets downloaded for installed
#: extensions, one file per digest (:class:`~igni_core.extensions.assets.AssetCache`).
ASSETS_DIR = ".assets"


class ExtensionStore:
    """The installed bundles on disk.

    The directory is this class's own; what is beside the bundles —
    where they came from (:attr:`provenance`), what igni server's
    last listing carried (:attr:`listing`) and the settings values
    (:mod:`igni_core.extensions.settings_values`) — belongs to
    collaborators of it.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = root if root is not None else home_config_dir() / EXTENSIONS_DIR
        self._provenance = Provenance(self._root)
        self._listing = ListingState(self._root)
        self._values: SettingsValues | None = None

    @property
    def root(self) -> Path:
        return self._root

    @property
    def provenance(self) -> Provenance:
        """Where the installed bundles came from."""
        return self._provenance

    @property
    def listing(self) -> ListingState:
        """What igni server's last listing carried."""
        return self._listing

    def path_for(self, extension_id: str) -> Path:
        """Where this extension's bundle lives, installed or not."""
        return self._root / StoreName(extension_id).id

    @property
    def asset_cache(self) -> AssetCache:
        """Assets on this machine, shared by every extension, by digest."""
        return AssetCache(self._root / ASSETS_DIR)

    def prune_assets(self) -> None:
        """Forget the assets no installed extension declares any more."""
        keep: set[str] = set()
        for bundle in self.installed():
            try:
                keep |= {asset.sha256 for asset in self.read(bundle).assets}
            except StoreError:
                continue
        self.asset_cache.prune(keep)

    @property
    def environments_root(self) -> Path:
        """Where ``python``-packaged extensions get their environments."""
        return self._root / ENVIRONMENTS_DIR

    def installed(self) -> list[Path]:
        """Every stored bundle, in a stable order.

        A directory with no readable manifest is skipped with a
        warning rather than raising: one corrupt extension must not
        stop a backend from starting the others. Dot-named ones are
        the store's own — staging, environments — and skipped quietly.
        """
        if not self._root.is_dir():
            return []
        bundles: list[Path] = []
        for path in sorted(self._root.iterdir()):
            if not path.is_dir() or path.name.startswith("."):
                continue
            if not (path / "manifest.json").is_file():
                logger.warning("ignoring %s: no manifest.json", path)
                continue
            bundles.append(path)
        return bundles

    def read(self, bundle: Path) -> ExtensionManifest:
        """Validate a bundle's manifest, naming the file on failure."""
        path = bundle / "manifest.json"
        if not path.is_file():
            raise StoreError(f"{bundle} has no manifest.json, so it is not an extension.")
        try:
            return ExtensionManifest.load(path)
        except ValueError as exc:
            raise StoreError(f"{path} is not a valid manifest: {exc}") from exc

    def add(
        self,
        bundle: Path,
        *,
        replace: bool = False,
        distribution: DistributionRecord | None = None,
    ) -> Path:
        """Copy a bundle into the store. Returns where it landed.

        ``replace`` is how an upgrade lands, and it is not the
        default: an install that silently overwrote one would make
        "which version is this" unanswerable, and the grants an
        administrator accepted belong to the version they accepted.

        ``distribution`` is the verified record when igni server
        distributed this bundle. Without one the bundle is a
        development install, and any record a previous distributed
        copy left is removed with it.
        """
        manifest = self.read(bundle)
        target = self.path_for(manifest.id)

        if target.exists() and not replace:
            raise StoreError(
                f"{manifest.id} is already installed at {target}. "
                "Pass --replace to upgrade it, which re-asks for its "
                "permissions."
            )

        self._root.mkdir(parents=True, exist_ok=True)
        # Copy to a sibling and rename, so an interrupted copy does
        # not leave a half-written bundle that ``installed`` would
        # then try to start at the next boot — and, on an upgrade,
        # does not cost the version that works: the old bundle is set
        # aside only once the new one is complete, and put back if the
        # swap fails.
        staging = target.with_name(f".{manifest.id}.incoming")
        retired = target.with_name(f".{manifest.id}.outgoing")
        for leftover in (staging, retired):
            if leftover.exists():
                ReadOnlyTree.remove(leftover)
        try:
            shutil.copytree(bundle, staging)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        if target.exists():
            target.rename(retired)
        try:
            staging.rename(target)
        except Exception:
            if retired.exists():
                retired.rename(target)
            shutil.rmtree(staging, ignore_errors=True)
            raise
        # Its assets were laid out read-only.
        ReadOnlyTree.remove(retired)

        # The environment was built from the bundle just replaced.
        self.drop_environments(manifest.id)
        if distribution is not None:
            self._provenance.record(distribution)
        else:
            self._provenance.path(manifest.id).unlink(missing_ok=True)

        logger.info("stored extension %s at %s", manifest.id, target)
        return target

    def drop_environments(self, extension_id: str) -> None:
        """Remove every private environment built for this extension."""
        target = self.environments_root / extension_id
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)

    def settings(self, manifest: ExtensionManifest) -> dict[str, object]:
        """This extension's settings, defaults filled in."""
        return self._settings_of(manifest).read()

    def save_settings(
        self, manifest: ExtensionManifest, values: dict[str, object]
    ) -> dict[str, object]:
        """Store values for the keys this extension declares."""
        return self._settings_of(manifest).write(values)

    def remove(self, extension_id: str) -> bool:
        """Delete a stored bundle. ``True`` if there was one.

        What belongs to it goes even when the bundle itself is already
        gone — a record or an environment left over from a bundle
        somebody deleted by hand would otherwise outlive it forever.
        """
        target = self.path_for(extension_id)
        self._provenance.path(extension_id).unlink(missing_ok=True)
        self.drop_environments(extension_id)
        # Its settings go with it. Leaving them would mean
        # reinstalling an extension silently inherited whatever the
        # last administrator had configured.
        self._settings_values().delete(extension_id)
        if not target.is_dir():
            return False
        ReadOnlyTree.remove(target)
        logger.info("removed extension %s", extension_id)
        return True

    def _settings_of(self, manifest: ExtensionManifest) -> ExtensionSettings:
        return ExtensionSettings(manifest, self._settings_values())

    def _settings_values(self) -> SettingsValues:
        if self._values is None:
            self._values = SettingsValues.for_store(self._root)
        return self._values


__all__ = [
    "ASSETS_DIR",
    "DATA_DIR",
    "DATA_DIR_ENV",
    "ENVIRONMENTS_DIR",
    "EXTENSIONS_DIR",
    "ExtensionStore",
]
