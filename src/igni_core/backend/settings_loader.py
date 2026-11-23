"""The backend's ``Settings``, with the organisation's group policy applied."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.config.group_policy.policy_settings import PolicySettingsLoader
from igni_core.core.config.group_policy.settings_fetcher import GroupPolicyUnavailable
from igni_core.core.config.settings import Settings

if TYPE_CHECKING:
    from igni_core.core.config.cli_overrides import CliOverrides

logger = logging.getLogger(__name__)


class BackendSettingsLoader:
    """Loads this process's ``Settings`` with the group's policy merged in.

    The two-pass load is :class:`PolicySettingsLoader`'s. When
    ``group_policy.required`` is on and no verified pack can be had, the
    backend still comes up on local settings, so the person can sign in and
    fetch one, but with the :class:`RunGate` closed: no agent run starts.

    The gate is the one the composition root built and handed over, so the
    run controller, the scheduler and the workflow bridge all see the same
    refusal. A verified pack whose ``mode`` is ``cloud`` closes it too
    (:meth:`RunGate.close_for_mode`): the organisation runs igni for this
    person in the cloud.
    """

    def __init__(
        self, project_dir: Path, gate: RunGate, cli_overrides: CliOverrides | None = None
    ) -> None:
        self._loader = PolicySettingsLoader(project_dir, cli_overrides=cli_overrides)
        self._gate = gate

    def load(self) -> Settings:
        local_only = self._loader.local()
        try:
            settings = self._loader.load(local_only)
        except GroupPolicyUnavailable as exc:
            logger.error("group-policy: %s", exc)
            self._gate.close(str(exc))
            return local_only
        if self._gate.close_for_mode(self._loader.mode()):
            logger.info("group-policy: the group runs igni in the cloud; no agent run here")
        return settings
