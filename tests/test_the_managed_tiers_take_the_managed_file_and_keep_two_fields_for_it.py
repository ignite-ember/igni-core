"""``ManagedTier`` and ``ManagedOnlyTier``: the sysadmin file, and what only it may set.

The managed file merges over everything. ``group_policy.trusted_keys`` and
``group_policy.required`` come from it alone: a value from a lower tier is
replaced by the managed one or the default, with a warning when it differed.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from igni_core.core.config.accumulator import SettingsAccumulator
from igni_core.core.config.tiers.managed import ManagedOnlyTier, ManagedTier
from tests.config_tier_rig import warnings_of

LOGGER = "igni_core.core.config.tiers.managed"


def _file(tmp_path: Path, content) -> Path:
    path = tmp_path / "managed.yaml"
    path.write_text(content if isinstance(content, str) else yaml.safe_dump(content))
    return path


def _only(path: Path | None) -> ManagedOnlyTier:
    return ManagedOnlyTier(lambda: path)


def test_the_managed_file_is_merged_over_what_is_there(tmp_path: Path):
    path = _file(tmp_path, {"permissions": {"mode": "allow"}})
    accumulator = SettingsAccumulator(
        payload={"permissions": {"mode": "ask", "web_search": "deny"}}
    )

    payload = ManagedTier(lambda: path).apply(accumulator).payload

    assert payload == {"permissions": {"mode": "allow", "web_search": "deny"}}


def test_no_managed_path_changes_nothing():
    accumulator = SettingsAccumulator(payload={"a": 1})

    assert ManagedTier(lambda: None).apply(accumulator) is accumulator


def test_a_managed_path_that_does_not_exist_changes_nothing(tmp_path: Path):
    accumulator = SettingsAccumulator(payload={"a": 1})

    assert ManagedTier(lambda: tmp_path / "absent.yaml").apply(accumulator) is accumulator


def test_the_path_is_asked_at_apply_time_each_time(tmp_path: Path):
    paths = [_file(tmp_path, {"a": 2}), None]
    tier = ManagedTier(lambda: paths.pop(0))

    assert tier.apply(SettingsAccumulator(payload={"a": 1})).payload == {"a": 2}
    assert tier.apply(SettingsAccumulator(payload={"a": 1})).payload == {"a": 1}


def test_the_managed_values_are_only_the_two_fields(tmp_path: Path):
    path = _file(
        tmp_path,
        {"group_policy": {"trusted_keys": ["k"], "required": True, "poll_seconds": 5}, "x": 1},
    )

    assert _only(path).managed_values() == {"trusted_keys": ["k"], "required": True}


def test_managed_values_are_empty_without_a_usable_file(tmp_path: Path):
    assert _only(None).managed_values() == {}
    assert _only(tmp_path / "absent.yaml").managed_values() == {}
    assert _only(_file(tmp_path, "- a\n")).managed_values() == {}
    assert _only(_file(tmp_path, "")).managed_values() == {}
    assert _only(_file(tmp_path, {"group_policy": "x"})).managed_values() == {}


def test_only_the_field_the_managed_file_sets_is_reported(tmp_path: Path):
    path = _file(tmp_path, {"group_policy": {"required": False}})

    assert _only(path).managed_values() == {"required": False}


def test_lower_tier_values_are_replaced_by_the_managed_ones(tmp_path: Path):
    path = _file(tmp_path, {"group_policy": {"trusted_keys": ["managed"], "required": True}})
    accumulator = SettingsAccumulator(
        payload={"group_policy": {"trusted_keys": ["mine"], "required": False, "poll_seconds": 9}}
    )

    with warnings_of(LOGGER) as warnings:
        block = _only(path).apply(accumulator).payload["group_policy"]

    assert block == {"trusted_keys": ["managed"], "required": True, "poll_seconds": 9}
    assert warnings == []


def test_values_a_lower_tier_set_are_discarded_back_to_the_defaults_with_a_warning():
    accumulator = SettingsAccumulator(
        payload={"group_policy": {"trusted_keys": ["mine"], "required": True, "poll_seconds": 9}}
    )

    with warnings_of(LOGGER) as warnings:
        block = _only(None).apply(accumulator).payload["group_policy"]

    assert block["trusted_keys"] != ["mine"]
    assert block["required"] is False
    assert block["poll_seconds"] == 9
    assert len(warnings) == 1
    assert "group_policy.trusted_keys, group_policy.required" in warnings[0]


def test_one_refused_field_is_the_only_one_named():
    accumulator = SettingsAccumulator(payload={"group_policy": {"required": True}})

    with warnings_of(LOGGER) as warnings:
        _only(None).apply(accumulator)

    assert len(warnings) == 1
    assert "group_policy.required" in warnings[0]
    assert "trusted_keys" not in warnings[0]


def test_a_lower_value_equal_to_the_default_is_not_warned_about():
    accumulator = SettingsAccumulator(payload={"group_policy": {"required": False}})

    with warnings_of(LOGGER) as warnings:
        block = _only(None).apply(accumulator).payload["group_policy"]

    assert block["required"] is False
    assert warnings == []


def test_a_payload_without_a_group_policy_block_gets_the_defaults(tmp_path: Path):
    path = _file(tmp_path, {"group_policy": {"required": True}})

    with warnings_of(LOGGER) as warnings:
        payload = _only(path).apply(SettingsAccumulator(payload={"a": 1})).payload

    assert payload["a"] == 1
    assert payload["group_policy"]["required"] is True
    assert "trusted_keys" in payload["group_policy"]
    assert warnings == []


def test_the_accumulator_it_was_given_is_not_changed():
    accumulator = SettingsAccumulator(payload={"group_policy": {"required": True}})

    _only(None).apply(accumulator)

    assert accumulator.payload == {"group_policy": {"required": True}}
