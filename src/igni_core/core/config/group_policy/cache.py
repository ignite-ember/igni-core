"""The policy cache: a thin facade over the collaborators that own its parts."""

from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import BaseModel

from igni_core.core.config.group_policy.kinds import KindRegistry
from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.config.group_policy.materializer import EntryMaterializer, MaterializeResult
from igni_core.core.config.group_policy.meta import PackMetaStore
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.group_policy.plugins import PluginInstallCoordinator
from igni_core.core.config.group_policy.signed_store import SignedPackStore, StoredPackRead
from igni_core.core.config.group_policy.trust import PackTrust
from igni_core.core.plugins.installer import PluginInstaller


class RebuildResult(BaseModel):
    """Whether the files derived from a verified pack are whole on disk."""

    ok: bool = True
    missing_plugins: list[str] = []
    reason: str = ""


class GroupPolicyCache:
    """Materializes a group pack under ``<data_dir>/group-policy/``.

    One directory per kind (``agents/<name>.md``, ``mcps/<name>.json``, …);
    plugins with a ``source_url`` are installed by :class:`PluginInstaller`
    into ``plugins/<entry name>``, the ``group-policy`` root of the plugin
    loader. The signed pack and its keys sit beside them (:attr:`signed`),
    and ``pack_meta.json`` records what was applied (:attr:`meta`).
    """

    def __init__(self, layout: GroupPolicyLayout, installer: PluginInstaller | None = None) -> None:
        self._layout = layout
        self._meta = PackMetaStore(layout)
        self._signed = SignedPackStore(layout)
        self._plugins = PluginInstallCoordinator(layout, installer)
        self._materializer = EntryMaterializer(
            layout, KindRegistry.default(self._plugins), self._plugins, self._meta
        )

    @classmethod
    def for_data_dir(
        cls, data_dir: str | Path | None, installer: PluginInstaller | None = None
    ) -> GroupPolicyCache:
        return cls(GroupPolicyLayout.for_data_dir(data_dir), installer)

    @property
    def layout(self) -> GroupPolicyLayout:
        return self._layout

    @property
    def meta(self) -> PackMetaStore:
        return self._meta

    @property
    def signed(self) -> SignedPackStore:
        return self._signed

    @property
    def plugins(self) -> PluginInstallCoordinator:
        return self._plugins

    @property
    def agents_dir(self) -> Path:
        return self._layout.ensure_dir("agents")

    @property
    def scripts_dir(self) -> Path:
        """Where a hook's ``{group_scripts}`` resolves to."""
        return self._layout.ensure_dir("scripts")

    def materialize(self, pack: GroupPolicyPack, *, rebuild: bool = False) -> MaterializeResult:
        """Write every file-shaped entry to disk, removing stale ones."""
        result = self._materializer.materialize(pack, rebuild=rebuild)
        # Nothing on disk may look like a policy that is not one.
        self._signed.drop_legacy_settings()
        return result

    def rebuild(self, pack: GroupPolicyPack) -> RebuildResult:
        """Re-derive every file-shaped entry from a *verified* pack.

        Called at start-up, so what the loaders then read is what igni server
        signed: an org file that was deleted is written back, one that was
        edited is overwritten, and a file added beside them is pruned. No
        network: a missing ``source_url`` plugin is reported, and the next
        fetch (which asks for the full pack while one is missing) installs it.
        """
        try:
            self.materialize(pack, rebuild=True)
        except OSError as exc:
            return RebuildResult(ok=False, reason=str(exc))
        missing = self._plugins.missing(pack)
        if missing:
            return RebuildResult(
                ok=False,
                missing_plugins=missing,
                reason="plugin(s) " + ", ".join(missing) + " are not installed; the next "
                "fetch from igni server reinstalls them",
            )
        return RebuildResult()

    def clear_derived(self) -> None:
        """Remove everything derived from a pack, keeping the signed pack and its keys."""
        self._materializer.clear_derived()
        self._signed.drop_legacy_settings()

    def clear(self) -> None:
        """Remove the entire cache directory."""
        if self._layout.root.exists():
            shutil.rmtree(self._layout.root)

    def verified_pack(self, trust: PackTrust) -> StoredPackRead:
        """The stored pack, verified against ``trust``, carrying the tag it was fetched with."""
        return self._signed.read(trust, etag=self._meta.stored_etag())
