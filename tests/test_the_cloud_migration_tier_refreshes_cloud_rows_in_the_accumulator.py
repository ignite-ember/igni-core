"""``CloudMigrationTier``: the migrator's output wired into the settings merge.

A valid ``models`` block gets its production igni server rows refreshed and
the rest of the payload kept. A malformed block is left exactly as it was, so
the final validation reports it on the user's terms.
"""

from __future__ import annotations

from igni_core.core.config.accumulator import SettingsAccumulator
from igni_core.core.config.model_entry import CLOUD_GATEWAY_HOST, ModelRegistryEntry
from igni_core.core.config.schemas.models import ModelsConfig
from igni_core.core.config.tiers.cloud_migration import CloudMigrationTier

PROD = f"https://{CLOUD_GATEWAY_HOST}/v1"

DEFAULTS = ModelsConfig(
    default="fast",
    registry={"fast": ModelRegistryEntry(model_id="fast-v2", url=PROD, api_key="cloud_token")},
)


def test_a_shipped_cloud_row_in_the_payload_is_brought_up_to_the_default():
    accumulator = SettingsAccumulator(
        payload={
            "air_gapped": False,
            "models": {
                "default": "fast",
                "registry": {"fast": {"model_id": "fast-v1", "url": PROD}},
            },
        }
    )

    payload = CloudMigrationTier(DEFAULTS).apply(accumulator).payload

    assert payload["models"]["registry"]["fast"]["model_id"] == "fast-v2"
    assert payload["air_gapped"] is False


def test_a_row_of_the_users_own_stays_as_it_was():
    accumulator = SettingsAccumulator(
        payload={
            "models": {
                "default": "fast",
                "registry": {"mine": {"model_id": "m", "url": "http://localhost:1"}},
            }
        }
    )

    registry = CloudMigrationTier(DEFAULTS).apply(accumulator).payload["models"]["registry"]

    assert registry["mine"]["model_id"] == "m"
    assert registry["mine"]["url"] == "http://localhost:1"


def test_a_malformed_models_block_is_left_for_the_final_validation():
    accumulator = SettingsAccumulator(payload={"models": {"retries": "many"}})

    assert CloudMigrationTier(DEFAULTS).apply(accumulator) is accumulator


def test_a_models_block_that_is_not_a_mapping_is_left_alone():
    accumulator = SettingsAccumulator(payload={"models": "gpt"})

    assert CloudMigrationTier(DEFAULTS).apply(accumulator) is accumulator


def test_a_payload_without_models_is_left_alone():
    accumulator = SettingsAccumulator(payload={"a": 1})

    assert CloudMigrationTier(DEFAULTS).apply(accumulator) is accumulator
