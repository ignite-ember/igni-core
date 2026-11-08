"""``CloudMigrationTier`` — igni server's model rows brought up to the shipping defaults."""

from __future__ import annotations

from typing import TYPE_CHECKING

from igni_core.core.config.accumulator import SettingsAccumulator
from igni_core.core.config.cloud_model_migrator import CloudModelMigrator
from igni_core.core.config.tiers.base import Tier

if TYPE_CHECKING:
    from igni_core.core.config.schemas.models import ModelsConfig


class CloudMigrationTier(Tier):
    """Override igni server rows with the shipping defaults so users
    always get the latest model after upgrading. Runs after the
    other tiers so a fresh migration is visible in the final
    :class:`Settings`.
    """

    def __init__(self, defaults: ModelsConfig) -> None:
        self._defaults = defaults

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        result = CloudModelMigrator.migrate_from_dict(
            accumulator.models_block(),
            defaults=self._defaults,
        )
        if not result.ok or result.models is None:
            # Malformed models block — leave the accumulator alone
            # so ``finalize`` surfaces the ValidationError on the
            # user's terms rather than mid-pipeline.
            return accumulator
        return accumulator.merge_models(result.models)
