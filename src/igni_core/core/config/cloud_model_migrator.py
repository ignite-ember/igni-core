"""``CloudModelMigrator`` — replace igni server rows with latest defaults.

Any registry row whose URL points at the production cloud host is
managed by us, so the migrator replaces it with the current shipping
default to ensure users always get the latest model after upgrading.
Rows pointing at dev/staging hosts are user-managed and left alone.

The migration works on typed :class:`ModelsConfig` /
:class:`ModelRegistryEntry` values: rows are read through
:meth:`ModelsConfig.iter_registry_entries` and every row the migration
writes is a :class:`ModelRegistryEntry`, never a raw dict.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ValidationError

from igni_core.core.config.model_entry import ModelRegistryEntry
from igni_core.core.config.schemas.models import ModelsConfig

logger = logging.getLogger(__name__)


class MigrationResult(BaseModel):
    """Typed outcome of a cloud-model migration attempt.

    A False :attr:`ok` carries a :attr:`reason` so the caller can log
    the skip cause instead of a mid-pipeline ``ValidationError``
    surfacing as a session-boot crash.
    """

    ok: bool
    models: ModelsConfig | None = None
    reason: str | None = None


class CloudModelMigrator:
    """Migrate cloud-managed registry rows to the shipping defaults.

    A coordinator over two immutable inputs (``current`` and
    ``defaults``) that yields a new :class:`ModelsConfig` via
    :meth:`migrate`; re-runs on the same instance are safe.
    """

    def __init__(self, current: ModelsConfig, defaults: ModelsConfig) -> None:
        self._current = current
        self._defaults = defaults

    @classmethod
    def migrate_from_dict(
        cls,
        models_block: dict[str, Any] | None,
        defaults: ModelsConfig,
    ) -> MigrationResult:
        """Validate the raw ``models`` block of the settings payload,
        run the migration, and return a typed :class:`MigrationResult`.

        The settings accumulator is a raw payload, so this is the
        boundary where the block is validated; an invalid block is a
        skip, not an error.
        """
        if models_block is None:
            return MigrationResult(ok=False, reason="no models block")
        try:
            current = ModelsConfig.model_validate(models_block)
        except ValidationError as exc:
            logger.debug("cloud migration skipped: models block invalid (%s)", exc)
            return MigrationResult(ok=False, reason=f"validation error: {exc}")
        return MigrationResult(ok=True, models=cls(current, defaults).migrate())

    def migrate(self) -> ModelsConfig:
        """Return a new :class:`ModelsConfig` with cloud rows updated
        to shipping defaults. When the current config has no registry
        entries, returns the input unchanged.
        """
        if not self._current.registry:
            return self._current

        default_cloud = self._default_cloud_entries()
        default_name = self._current.default
        # Rows the migration does not touch pass through in whatever
        # shape the schema holds them; every row it writes is typed.
        registry = dict(self._current.registry)

        for name, entry in self._current.iter_registry_entries():
            if not ModelRegistryEntry.is_cloud_gateway_url(entry.url or ""):
                continue
            # A cloud model still shipped is refreshed to its latest
            # default; one no longer shipped is replaced by the current
            # default model.
            replacement = default_cloud.get(name) or default_cloud.get(default_name)
            if replacement is not None:
                registry[name] = replacement.model_copy()

        if default_name and default_name not in registry and default_name in default_cloud:
            registry[default_name] = default_cloud[default_name].model_copy()

        return self._current.model_copy(update={"registry": registry})

    def _default_cloud_entries(self) -> dict[str, ModelRegistryEntry]:
        """The shipping-defaults registry narrowed to its cloud rows."""
        return {
            name: entry
            for name, entry in self._defaults.iter_registry_entries()
            if entry.matches_cloud_gateway()
        }
