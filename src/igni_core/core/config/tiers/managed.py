"""The sysadmin's managed-settings file: what it sets, and what only it may set."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from igni_core.core.config.accumulator import SettingsAccumulator
from igni_core.core.config.config_io import YamlSource
from igni_core.core.config.managed_policy import ManagedPolicySource
from igni_core.core.config.schemas.storage_and_paths import GroupPolicyConfig
from igni_core.core.config.tiers.base import Tier

logger = logging.getLogger(__name__)


class ManagedTier(Tier):
    """Sysadmin-controlled managed-policy YAML file. Runs LAST in
    the pipeline so it wins over CLI — the "you can't
    ``--auto-approve`` your way out of org policy" tier.

    Takes an INJECTED path-provider callable rather than reaching
    directly for :meth:`ManagedPolicySource.platform_path`. The
    :class:`SettingsLoader` passes :meth:`OrganisationTiers.managed_path`
    so its own policy reads see the same file. Default provider is
    :meth:`ManagedPolicySource.platform_path` so direct plan construction
    gets the natural behaviour.
    """

    def __init__(
        self,
        path_provider: Callable[[], Path | None] = ManagedPolicySource.platform_path,
    ) -> None:
        self._path_provider = path_provider

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        path = self._path_provider()
        if path is None:
            return accumulator
        return accumulator.merge(YamlSource(path).load())


class ManagedOnlyTier(Tier):
    """Settings only the managed-settings file (or the environment) may set.

    ``group_policy.trusted_keys`` and ``group_policy.required`` decide whether
    the organisation's group policy is enforced at all. Whoever could set
    them could pin a key of their own, or make an unusable pack harmless —
    so a value from the user's config, a project's, the CLI or the group
    pack itself is discarded here, and only the managed file's survives.
    The environment (``IGNI_GROUP_POLICY_TRUSTED_KEYS``,
    ``IGNI_GROUP_POLICY_REQUIRED``) is applied afterwards by
    ``GroupPolicyConfig`` — it is a deployment's decision, made where the
    process is started.

    Runs after :class:`ManagedTier`, reading the same file through the same
    path provider.
    """

    #: ``group_policy`` fields only the managed file may set.
    FIELDS: ClassVar[tuple[str, ...]] = ("trusted_keys", "required")

    def __init__(
        self,
        path_provider: Callable[[], Path | None] = ManagedPolicySource.platform_path,
    ) -> None:
        self._path_provider = path_provider

    def managed_values(self) -> dict[str, Any]:
        """What the managed file sets of :attr:`FIELDS`, and nothing else."""
        path = self._path_provider()
        payload = YamlSource(path).load() if path is not None else {}
        block = payload.get("group_policy") if isinstance(payload, dict) else None
        if not isinstance(block, dict):
            return {}
        return {field: block[field] for field in self.FIELDS if field in block}

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        managed = self.managed_values()
        defaults = GroupPolicyConfig.model_construct().model_dump()
        payload = dict(accumulator.payload)
        block = dict(payload.get("group_policy") or {})
        refused = [
            field
            for field in self.FIELDS
            if field not in managed and field in block and block[field] != defaults[field]
        ]
        if refused:
            logger.warning(
                "group_policy.%s may be set only in the managed-settings file or the "
                "environment; the value from user, project, CLI or group settings is ignored.",
                ", group_policy.".join(refused),
            )
        for field in self.FIELDS:
            block[field] = managed.get(field, defaults[field])
        payload["group_policy"] = block
        return SettingsAccumulator(payload=payload)
