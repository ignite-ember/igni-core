"""The backend's ``Settings``, with the organisation's group policy applied."""

from __future__ import annotations

import logging
from pathlib import Path

from igni_core.core.config.group_policy.gate import GroupPolicyGate
from igni_core.core.config.group_policy.settings_fetcher import (
    CachedSettingsFetcher,
    GroupPolicyUnavailable,
)
from igni_core.core.config.settings import Settings, load_settings

logger = logging.getLogger(__name__)


class BackendSettingsLoader:
    """Loads this process's ``Settings`` with the group's policy merged in.

    Two passes. The policy cache lives under ``storage.data_dir``, which is
    itself a setting, so the first pass (local configuration only) answers
    "which directory" and the second, with the cached pack, is the one that
    counts. Neither touches the network: the pack is the one
    ``AuthController`` last wrote to disk (:class:`CachedSettingsFetcher`).

    When ``group_policy.required`` is on and no verified pack can be had, the
    backend still comes up on local settings, so the person can sign in and
    fetch one, but with :class:`GroupPolicyGate` closed: no agent run starts.
    """

    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir

    def load(self) -> Settings:
        local_only = load_settings(project_dir=self._project_dir)
        try:
            return load_settings(
                project_dir=self._project_dir,
                group_policy_fetcher=CachedSettingsFetcher.for_settings(local_only),
            )
        except GroupPolicyUnavailable as exc:
            logger.error("group-policy: %s", exc)
            GroupPolicyGate.close(str(exc))
            return local_only
