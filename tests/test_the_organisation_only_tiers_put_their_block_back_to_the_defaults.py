"""``OrganisationOnlyTier``, ``WorkspaceTrustTier`` and ``TelemetryTier``.

``workspace_trust`` and ``telemetry`` come from the organisation. Whatever a
user, a project or the CLI wrote is replaced with the block's defaults, and
a warning is logged only when what was written differed from them.
"""

from __future__ import annotations

import pytest

from igni_core.core.config.accumulator import SettingsAccumulator
from igni_core.core.config.schemas.telemetry import OtlpConfig, TelemetryConfig
from igni_core.core.config.tiers.organisation import (
    OrganisationOnlyTier,
    TelemetryTier,
    WorkspaceTrustTier,
)
from igni_core.core.workspace_trust.policy import WorkspaceTrustConfig
from tests.config_tier_rig import warnings_of

LOGGER = "igni_core.core.config.tiers.organisation"

TRUST_DEFAULTS = WorkspaceTrustConfig.model_construct().model_dump()
TELEMETRY_DEFAULTS = TelemetryConfig.model_construct(otlp=OtlpConfig.model_construct()).model_dump()


def _apply(tier, payload: dict) -> dict:
    return tier.apply(SettingsAccumulator(payload=payload)).payload


def test_the_base_tier_does_not_know_its_defaults():
    with pytest.raises(NotImplementedError):
        OrganisationOnlyTier().defaults()


def test_the_tiers_own_their_blocks_and_their_model_defaults():
    assert WorkspaceTrustTier.BLOCK == "workspace_trust"
    assert TelemetryTier.BLOCK == "telemetry"
    assert WorkspaceTrustTier().defaults() == TRUST_DEFAULTS
    assert TelemetryTier().defaults() == TELEMETRY_DEFAULTS
    assert "otlp" in TelemetryTier().defaults()


def _changed(defaults: dict) -> dict:
    key = next(k for k, v in defaults.items() if isinstance(v, bool))
    return {**defaults, key: not defaults[key]}


def test_a_workspace_trust_value_from_below_is_discarded_with_a_warning():
    with warnings_of(LOGGER) as warnings:
        payload = _apply(
            WorkspaceTrustTier(), {"workspace_trust": _changed(TRUST_DEFAULTS), "a": 1}
        )

    assert payload == {"workspace_trust": TRUST_DEFAULTS, "a": 1}
    assert len(warnings) == 1
    assert warnings[0].startswith("workspace_trust may be set only")


def test_a_telemetry_value_from_below_is_discarded_with_a_warning():
    written = {"otlp": {"endpoint": "https://collector.example"}}

    with warnings_of(LOGGER) as warnings:
        payload = _apply(TelemetryTier(), {"telemetry": written})

    assert payload == {"telemetry": TELEMETRY_DEFAULTS}
    assert len(warnings) == 1
    assert warnings[0].startswith("telemetry may be set only")


def test_a_block_equal_to_the_defaults_is_not_warned_about():
    with warnings_of(LOGGER) as warnings:
        payload = _apply(WorkspaceTrustTier(), {"workspace_trust": dict(TRUST_DEFAULTS)})

    assert payload == {"workspace_trust": TRUST_DEFAULTS}
    assert warnings == []


def test_keys_the_defaults_do_not_know_are_dropped_without_a_warning():
    with warnings_of(LOGGER) as warnings:
        payload = _apply(WorkspaceTrustTier(), {"workspace_trust": {"made_up": 1}})

    assert payload == {"workspace_trust": TRUST_DEFAULTS}
    assert warnings == []


@pytest.mark.parametrize("block", ["nope", None, ["x"]])
def test_a_block_that_is_not_a_mapping_is_replaced_without_a_warning(block):
    with warnings_of(LOGGER) as warnings:
        payload = _apply(WorkspaceTrustTier(), {"workspace_trust": block})

    assert payload == {"workspace_trust": TRUST_DEFAULTS}
    assert warnings == []


def test_an_absent_block_is_filled_with_the_defaults():
    assert _apply(TelemetryTier(), {"a": 1}) == {"a": 1, "telemetry": TELEMETRY_DEFAULTS}


def test_the_accumulator_it_was_given_is_not_changed():
    accumulator = SettingsAccumulator(payload={"telemetry": {"x": 1}})

    TelemetryTier().apply(accumulator)

    assert accumulator.payload == {"telemetry": {"x": 1}}
