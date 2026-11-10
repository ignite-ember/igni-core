"""The backend's ``Settings``, with the organisation's group policy applied."""

from __future__ import annotations

import logging
from pathlib import Path

from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.config.group_policy.policy_settings import PolicySettingsLoader
from igni_core.core.config.group_policy.settings_fetcher import GroupPolicyUnavailable
from igni_core.core.config.settings import Settings

logger = logging.getLogger(__name__)


class BackendSettingsLoader:
    """Loads this process's ``Settings`` with the group's policy merged in.

    The two-pass load is :class:`PolicySettingsLoader`'s. When
    ``group_policy.required`` is on and no verified pack can be had, the
    backend still comes up on local settings, so the person can sign in and
    fetch one, but with the :class:`RunGate` closed: no agent run starts.

    The gate is the one the composition root built and handed over, so the
    run controller, the scheduler and the workflow bridge all see the same
    refusal.
    """

    def __init__(self, project_dir: Path, gate: RunGate) -> None:
        self._loader = PolicySettingsLoader(project_dir)
        self._gate = gate

    def load(self) -> Settings:
        local_only = self._loader.local()
        try:
            return self._loader.load(local_only)
        except GroupPolicyUnavailable as exc:
            logger.error("group-policy: %s", exc)
            self._gate.close(str(exc))
            return local_only
