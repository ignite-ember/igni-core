"""``Settings`` with the group policy cached on disk applied — the two-pass load."""

from __future__ import annotations

from pathlib import Path

from igni_core.core.config.group_policy.settings_fetcher import CachedSettingsFetcher
from igni_core.core.config.settings import Settings, load_settings


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
    """

    def __init__(self, project_dir: Path | None = None) -> None:
        self._project_dir = project_dir

    def local(self) -> Settings:
        """Local configuration only: the first pass."""
        return load_settings(project_dir=self._project_dir)

    def load(self, local: Settings | None = None) -> Settings:
        """The load that counts, from ``local`` (read now when not given)."""
        local_only = local if local is not None else self.local()
        return load_settings(
            project_dir=self._project_dir,
            group_policy_fetcher=CachedSettingsFetcher.for_settings(local_only),
        )


__all__ = ["PolicySettingsLoader"]
