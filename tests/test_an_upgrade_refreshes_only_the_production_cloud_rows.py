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


def test_an_empty_registry_comes_back_unchanged() -> None:
    current = ModelsConfig(default="fast", registry={})
    assert CloudModelMigrator(current, DEFAULTS).migrate() is current


def test_the_default_model_is_added_when_the_registry_lacks_it() -> None:
    models = _migrate(
        {"default": "fast", "registry": {"mine": {"model_id": "m", "url": "http://x"}}}
    )
    assert models.registry["fast"] == DEFAULTS.registry["fast"]
    assert set(models.registry) == {"fast", "mine"}


def test_a_default_that_is_not_shipped_is_not_invented() -> None:
    models = _migrate(
        {"default": "mine", "registry": {"mine": {"model_id": "m", "url": "http://x"}}}
    )
    assert set(models.registry) == {"mine"}


def test_a_cloud_row_stays_when_there_are_no_cloud_defaults() -> None:
    defaults = ModelsConfig(default="local", registry=dict(DEFAULTS.registry))
    defaults.registry.pop("fast")
    row = {"model_id": "fast-v1", "url": PROD}
    result = CloudModelMigrator.migrate_from_dict(
        {"default": "local", "registry": {"fast": row}}, defaults
    )
    assert result.models is not None
    assert result.models.registry["fast"] == ModelRegistryEntry.model_validate(row)


def test_a_refreshed_row_is_a_copy_of_the_default() -> None:
    models = _migrate({"default": "fast", "registry": {"fast": {"model_id": "v1", "url": PROD}}})
    assert models.registry["fast"] is not DEFAULTS.registry["fast"]


def test_the_skip_says_why() -> None:
    assert CloudModelMigrator.migrate_from_dict(None, DEFAULTS).reason == "no models block"
    reason = CloudModelMigrator.migrate_from_dict({"retries": "many"}, DEFAULTS).reason
    assert reason is not None and reason.startswith("validation error: ")
