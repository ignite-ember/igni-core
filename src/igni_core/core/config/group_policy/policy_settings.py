"""``Settings`` with the group policy cached on disk applied — the two-pass load."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from igni_core.core.config.group_policy.pack import GroupMode
from igni_core.core.config.group_policy.settings_fetcher import (
    CachedSettingsFetcher,
    GroupPolicyUnavailable,
)
from igni_core.core.config.settings import Settings
from igni_core.core.config.settings_loader import SettingsLoader

if TYPE_CHECKING:
    from igni_core.core.config.cli_overrides import CliOverrides


class PolicySettingsLoader:
    """Loads ``Settings`` the way every igni process does, policy included.

    Two passes. The policy cache lives under ``storage.data_dir``, which is
    itself a setting, so the first pass (local configuration only) answers
    "which directory" and the second, with the cached pack, is the one that
    counts. Neither touches the network: the pack is the one
    ``GroupPolicySync`` last wrote to disk (:class:`CachedSettingsFetcher`).

    What to do when a required policy cannot be had differs per caller — the
    backend boots with its gate closed, the CLI refuses — so :meth:`load`
    raises :class:`~.settings_fetcher.GroupPolicyUnavailable` and leaves the
    decision to them, with :meth:`local` as the fallback.

    ``cli_overrides`` are the process's command-line flags, merged in both
    passes at the CLI tier: above the folder's own files, below the
    organisation's.

    :meth:`mode` says where the pack the last :meth:`load` applied says igni
    runs for the group, so the caller can refuse runs in ``cloud`` mode.
    """

    def __init__(
        self, project_dir: Path | None = None, *, cli_overrides: CliOverrides | None = None
    ) -> None:
        self._project_dir = project_dir
        self._cli_overrides = cli_overrides
        #: The fetcher the last :meth:`load` read the pack through.
        self._fetcher: CachedSettingsFetcher | None = None

    def local(self) -> Settings:
        """Local configuration only: the first pass."""
        return SettingsLoader.load(cli_overrides=self._cli_overrides, project_dir=self._project_dir)

    def load(self, local: Settings | None = None) -> Settings:
        """The load that counts, from ``local`` (read now when not given)."""
        local_only = local if local is not None else self.local()
        self._fetcher = CachedSettingsFetcher.for_settings(local_only)
        return SettingsLoader.load(
            cli_overrides=self._cli_overrides,
            project_dir=self._project_dir,
            group_policy_fetcher=self._fetcher,
        )

    def mode(self) -> GroupMode:
        """The group's mode, as the pack the last :meth:`load` applied says it.

        No load yet, no verified pack, or a required one that could not be
        had: :attr:`GroupMode.local_and_cloud`, what igni did before there
        was a mode. Reads nothing more: the fetcher remembers its pack.
        """
        return mode_of(self._fetcher)


def mode_of(fetcher: CachedSettingsFetcher | None) -> GroupMode:
    """The mode of the pack ``fetcher`` read; :attr:`GroupMode.local_and_cloud` without one."""
    if fetcher is None:
        return GroupMode.local_and_cloud
    try:
        pack = fetcher()
    except GroupPolicyUnavailable:
        return GroupMode.local_and_cloud
    return pack.mode if pack is not None else GroupMode.local_and_cloud


__all__ = ["PolicySettingsLoader", "mode_of"]
