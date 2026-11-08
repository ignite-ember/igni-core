"""An upgrade refreshes the production cloud rows and leaves the rest alone.

:class:`CloudModelMigrator` replaces registry rows pointing at the
production igni server host with the shipping defaults. A row pointing
at a dev/staging host is the user's own override and survives.
"""

from __future__ import annotations

from igni_core.core.config.cloud_model_migrator import CloudModelMigrator
from igni_core.core.config.model_entry import ModelRegistryEntry
from igni_core.core.config.schemas.models import ModelsConfig

PROD = "https://api.ignite-ember.sh/v1"
STAGING = "https://dev-api.ignite-ember.sh/v1"

DEFAULTS = ModelsConfig(
    default="fast",
    registry={
        "fast": ModelRegistryEntry(model_id="fast-v2", url=PROD, api_key="cloud_token"),
        "local": ModelRegistryEntry(model_id="llama", url="http://localhost:11434"),
    },
)


def _migrate(block: dict) -> ModelsConfig:
    result = CloudModelMigrator.migrate_from_dict(block, defaults=DEFAULTS)
    assert result.ok and result.models is not None
    return result.models


def test_a_shipped_cloud_row_is_refreshed_to_its_default() -> None:
    models = _migrate(
        {"default": "fast", "registry": {"fast": {"model_id": "fast-v1", "url": PROD}}}
    )
    assert models.registry["fast"] == DEFAULTS.registry["fast"]


def test_a_retired_cloud_row_becomes_the_default_model() -> None:
    models = _migrate({"default": "fast", "registry": {"old": {"model_id": "old-v0", "url": PROD}}})
    assert isinstance(models.registry["old"], ModelRegistryEntry)
    assert models.registry["old"].model_id == "fast-v2"
    assert models.registry["fast"] == DEFAULTS.registry["fast"]


def test_a_staging_row_and_a_local_row_are_kept() -> None:
    staging = {"model_id": "fast-next", "url": STAGING}
    local = {"model_id": "mine", "url": "http://localhost:8000"}
    models = _migrate({"default": "fast", "registry": {"fast": staging, "mine": local}})
    rows = models.registry
    assert rows["fast"] == ModelRegistryEntry.model_validate(staging)
    assert rows["mine"] == ModelRegistryEntry.model_validate(local)


def test_a_missing_or_invalid_block_is_a_skip() -> None:
    assert not CloudModelMigrator.migrate_from_dict(None, defaults=DEFAULTS).ok
    assert not CloudModelMigrator.migrate_from_dict({"retries": "many"}, defaults=DEFAULTS).ok
