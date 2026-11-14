"""Plugins a pack installs from a git source, kept in the pack's own directory."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.group_policy.signed_store import PluginSourceClaim
from igni_core.core.plugins.git import GitError
from igni_core.core.plugins.installer import PluginError, PluginInstaller

logger = logging.getLogger(__name__)


def _already_installed(exc: PluginError) -> bool:
    """Whether the installer refused because the plugin is there already.

    A message match, because the installer has no type for it yet; a typed
    ``PluginAlreadyInstalled(PluginError)`` would retire this.
    """
    message = str(exc).lower()
    return "already installed" in message or "already exists" in message


class PluginInstallCoordinator:
    """Installs, adopts, reports and prunes ``<cache>/plugins/<entry name>``.

    The pack's directory holds only what the pack installs: it is rebuilt and
    removed with the pack, and the plugin loader reads it as the
    ``group-policy`` root.
    """

    def __init__(self, layout: GroupPolicyLayout, installer: PluginInstaller | None = None) -> None:
        self._layout = layout
        self._installer = installer

    @property
    def directory(self) -> Path:
        return self._layout.dir_for("plugins")

    def install(self, entry: GroupPolicyEntry) -> None:
        """Install one ``source_url`` entry into the pack's directory, once.

        On install failure (``GitError`` / network / bad manifest) this logs
        and continues: one bad plugin must not abort the rest of the pack.
        """
        target = self._layout.plugin_dir(entry.entry_name)
        if target is None or target.is_dir() or not entry.source_url:
            # Unsafe names never get here (the materializer skips them); a
            # directory already there is the pack's install, not re-cloned on
            # every poll.
            return
        if self._installer is None:
            logger.warning(
                "Group policy plugin entry %r has source_url but no installer "
                "is configured; skipping install. Pass PluginInstaller(data_dir=...) "
                "when constructing GroupPolicyCache to enable remote sources.",
                entry.entry_name,
            )
            return
        self.adopt_user_install(entry, target)
        if target.is_dir():
            return
        try:
            self._installer.scoped_to(self.directory).install(
                entry.source_url,
                ref=entry.source_ref,
                subdir=entry.source_subdir,
                directory_name=target.name,
            )
            logger.info(
                "Group policy plugin %r installed from %s", entry.entry_name, entry.source_url
            )
        except PluginError as exc:
            if _already_installed(exc):
                logger.info(
                    "Group policy plugin %r is already installed from %s; reusing",
                    entry.entry_name,
                    entry.source_url,
                )
                return
            logger.warning("Group policy plugin %r install failed: %s", entry.entry_name, exc)
        except (GitError, OSError) as exc:
            # The system git binary, a network drop, an auth failure.
            logger.warning("Group policy plugin %r install errored: %s", entry.entry_name, exc)

    def adopt_user_install(self, entry: GroupPolicyEntry, target: Path) -> None:
        """Move a group plugin an older release put in the user's directory.

        Recognised by the source ``plugins.json`` recorded for it — the
        entry's ``source_url``. Its record is dropped either way (it is the
        pack's now, not something ``/plugin update`` manages); the first
        such directory becomes the pack's install, and any other is removed.
        """
        if self._installer is None or not entry.source_url:
            return
        try:
            released = self._installer.release_installed_from(entry.source_url)
        except (OSError, ValueError) as exc:  # a migration must not stop the pack
            logger.debug("group-policy: no user-tier install to adopt (%s)", exc)
            return
        for path in released:
            try:
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), str(target))
                    logger.info(
                        "group-policy: moved plugin %s from %s to %s",
                        entry.entry_name,
                        path,
                        target,
                    )
                else:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError as exc:
                logger.warning("group-policy: could not move %s (%s)", path, exc)

    def missing(self, pack: GroupPolicyPack) -> list[str]:
        """The pack's ``source_url`` plugins with nothing installed for them."""
        missing: list[str] = []
        for entry in pack.entries:
            if entry.kind != "plugins" or not entry.enabled or not entry.source_url:
                continue
            target = self._layout.plugin_dir(entry.entry_name)
            if target is not None and not target.is_dir():
                missing.append(entry.entry_name)
        return missing

    def lacks_any(self, claims: list[PluginSourceClaim]) -> bool:
        """Whether any claimed ``source_url`` plugin is not installed."""
        for claim in claims:
            if claim.kind != "plugins" or not claim.source_url or not claim.entry_name:
                continue
            target = self._layout.plugin_dir(claim.entry_name)
            if target is not None and not target.is_dir():
                return True
        return False

    def prune_planted(self, pack: GroupPolicyPack) -> None:
        """Remove a plugin directory the pack does not name: withdrawn, or planted."""
        if not self.directory.is_dir():
            return
        installed = {
            e.safe_name().name for e in pack.entries if e.kind == "plugins" and e.source_url
        }
        for path in self.directory.iterdir():
            if path.is_dir() and path.name not in installed:
                logger.warning(
                    "group-policy: %s is not in the organisation's signed pack; removed", path
                )
                shutil.rmtree(path, ignore_errors=True)
