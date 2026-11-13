"""``SettingsAccumulator`` carries the merged config through the tiers.

Each step returns a new accumulator, so a caller can keep an earlier
snapshot; an empty override is no step at all; the typed models block is
dumped once; and the payload is validated only at ``to_settings``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.core.config.accumulator import SettingsAccumulator
from igni_core.core.config.schemas.models import ModelsConfig
from igni_core.core.config.settings import Settings


def test_seeding_copies_the_defaults_payload() -> None:
    defaults = {"a": 1, "models": {"default": "x"}}
    acc = SettingsAccumulator.from_defaults(defaults)
    defaults["a"] = 2
    defaults["extra"] = True
    assert acc.payload == {"a": 1, "models": {"default": "x"}}


def test_a_merge_is_a_new_accumulator_and_the_old_snapshot_is_unchanged() -> None:
    first = SettingsAccumulator.from_defaults({"display": {"a": 1, "b": 2}, "keep": "yes"})
    second = first.merge({"display": {"b": 3}, "new": 1})
    assert second is not first
    assert second.payload == {"display": {"a": 1, "b": 3}, "keep": "yes", "new": 1}
    assert first.payload == {"display": {"a": 1, "b": 2}, "keep": "yes"}


def test_an_empty_override_returns_the_same_accumulator() -> None:
    acc = SettingsAccumulator.from_defaults({"a": 1})
    assert acc.merge({}) is acc


def test_a_scalar_replaces_a_block() -> None:
    acc = SettingsAccumulator.from_defaults({"display": {"a": 1}})
    assert acc.merge({"display": None}).payload == {"display": None}


def test_the_models_block_is_a_dict_or_none() -> None:
    assert SettingsAccumulator.from_defaults({}).models_block() is None
    assert SettingsAccumulator.from_defaults({"models": "oops"}).models_block() is None
    assert SettingsAccumulator.from_defaults({"models": None}).models_block() is None
    assert SettingsAccumulator.from_defaults({"models": {"default": "x"}}).models_block() == {
        "default": "x"
    }


def test_merging_models_dumps_the_typed_block_and_leaves_the_source_alone() -> None:
    acc = SettingsAccumulator.from_defaults({"models": {"default": "old"}, "other": 1})
    out = acc.merge_models(ModelsConfig(default="new"))
    assert out is not acc
    assert out.payload["models"] == ModelsConfig(default="new").model_dump()
    assert out.payload["other"] == 1
    assert acc.payload["models"] == {"default": "old"}


def test_to_settings_builds_the_class_and_an_invalid_payload_raises() -> None:
    settings = SettingsAccumulator.from_defaults(Settings.default_dict()).to_settings(Settings)
    assert isinstance(settings, Settings)
    bad = SettingsAccumulator.from_defaults({"models": {"retries": "many"}})
    with pytest.raises(ValidationError):
        bad.to_settings(Settings)
