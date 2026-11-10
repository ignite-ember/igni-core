"""Writes a pack's file-shaped entries where their loaders look, and removes stale ones."""

from __future__ import annotations

import logging
import shutil

from pydantic import BaseModel

from igni_core.core.config.group_policy.kinds import ActiveNames, KindRegistry, MaterializeContext
from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.config.group_policy.meta import PackMeta, PackMetaStore
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.group_policy.plugins import PluginInstallCoordinator

logger = logging.getLogger(__name__)


class MaterializeResult(BaseModel):
    """What a materialise pass left out."""

    #: Entries whose name could not be written safely; the rest of the pack applied.
    skipped: list[str] = []


class EntryMaterializer:
    """One directory per kind, each of which a loader is told to scan."""

    def __init__(
        self,
        layout: GroupPolicyLayout,
        registry: KindRegistry,
        plugins: PluginInstallCoordinator,
        meta: PackMetaStore,
    ) -> None:
        self._layout = layout
        self._registry = registry
        self._plugins = plugins
        self._meta = meta

    def materialize(self, pack: GroupPolicyPack, *, rebuild: bool = False) -> MaterializeResult:
        """Write every enabled entry through its kind, then prune and record the meta.

        Settings are not written out: they are read back from the signed pack,
        verified, at the next start. A derived, unsigned copy would be a
        second thing to forge.
        """
        ctx = MaterializeContext(layout=self._layout, rebuild=rebuild)
        skipped: list[str] = []
        for entry in pack.entries:
            if not entry.enabled:
                continue
            # One unsafe name must not cost the group its whole policy: the
            # entry is skipped, named, and the other N still apply.
            check = entry.safe_name()
            if not check.ok:
                logger.warning(
                    "Group policy entry skipped: %s. A %s entry cannot be written "
                    "under that name, so it has been ignored; the rest of the pack "
                    "was applied.",
                    check.reason,
                    entry.kind,
                )
                skipped.append(entry.entry_name)
                continue
            kind = self._registry.get(entry.kind)
            if kind is not None:
                kind.write(entry, ctx)

        scripts_dir = self._layout.ensure_dir("scripts")
        self._layout.ensure_dir("hooks")
        ctx.hooks.expanded(scripts_dir).write_to(self._layout.hooks_file)
        self._prune(ctx.active)
        # The pack's plugin directory holds only what the pack installs.
        self._plugins.prune_planted(pack)
        self._meta.save(PackMeta.from_pack(pack))
        return MaterializeResult(skipped=skipped)

    def clear_derived(self) -> None:
        """Remove every kind's directory: there is no pack this install may apply.

        The signed pack, the cached keys and the version are kept: they are
        what the next start checks against.
        """
        for name in self._registry.file_directories():
            path = self._layout.dir_for(name)
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)

    def _prune(self, active: ActiveNames) -> None:
        """Remove what the group no longer ships."""
        for kind in self._registry.pruned():
            directory = self._layout.ensure_dir(kind.directory)
            names = active.of(kind.name)
            for path in sorted(directory.iterdir()):
                if not kind.is_stale(path, names):
                    continue
                try:
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                except OSError as exc:  # a leftover file is not fatal
                    logger.debug("Could not remove stale group policy path %s: %s", path, exc)
