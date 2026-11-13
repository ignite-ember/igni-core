"""``RawModelRegistry``: ``models.registry`` of a payload before it is validated.

An entry that is a mapping is a row; anything else is carried through for
``Settings`` to refuse. Nothing here mutates the payload it was read from.
"""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from igni_core.core.config.tiers.raw_model_registry import RawModelRegistry

PAYLOAD = {
    "air_gapped": False,
    "models": {
        "default": "a",
        "registry": {
            "a": {"url": "u", "api_key_cmd": "c", "api_key": "k", "model_id": "m"},
            "b": {"model_id": "m2"},
            "junk": "not a mapping",
            "none": None,
        },
    },
}


def _registry() -> RawModelRegistry:
    registry = RawModelRegistry.of(PAYLOAD)
    assert registry is not None
    return registry


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"models": None},
        {"models": "x"},
        {"models": {}},
        {"models": {"registry": None}},
        {"models": {"registry": ["a"]}},
        {"models": {"registry": "a"}},
    ],
)
def test_a_payload_without_a_mapping_registry_has_none(payload):
    assert RawModelRegistry.of(payload) is None


def test_an_empty_registry_mapping_is_still_a_registry():
    registry = RawModelRegistry.of({"models": {"registry": {}}})

    assert registry is not None
    assert registry.rows() == {}


def test_the_rows_are_the_entries_that_are_mappings():
    assert set(_registry().rows()) == {"a", "b"}


def test_a_row_is_found_by_name_only_when_it_is_a_mapping():
    registry = _registry()

    assert registry.row("b") == {"model_id": "m2"}
    assert registry.row("junk") is None
    assert registry.row("none") is None
    assert registry.row("absent") is None


def test_rows_setting_lists_each_field_a_row_sets_sorted():
    registry = _registry()

    assert registry.rows_setting(["api_key_cmd", "api_key", "api_key_env"]) == {
        "a": ["api_key", "api_key_cmd"]
    }


def test_rows_setting_takes_a_one_shot_iterable_of_fields():
    registry = _registry()

    assert registry.rows_setting(f for f in ("model_id", "api_key")) == {
        "a": ["api_key", "model_id"],
        "b": ["model_id"],
    }


def test_rows_setting_with_no_fields_names_no_row():
    assert _registry().rows_setting([]) == {}


def test_without_takes_the_named_fields_out_of_the_named_rows_only():
    stripped = _registry().without({"a": ["api_key_cmd", "api_key"]})

    assert stripped.row("a") == {"url": "u", "model_id": "m"}
    assert stripped.row("b") == {"model_id": "m2"}


def test_without_accepts_a_one_shot_iterable_of_fields():
    stripped = _registry().without({"a": (f for f in ("api_key_cmd", "url"))})

    assert stripped.row("a") == {"api_key": "k", "model_id": "m"}


def test_without_leaves_unknown_and_non_mapping_rows_alone():
    stripped = _registry().without({"absent": ["x"], "junk": ["x"], "none": ["x"]})

    assert stripped.entries == _registry().entries


def test_without_does_not_change_the_registry_it_was_called_on():
    registry = _registry()

    registry.without({"a": ["url"]})

    assert registry.row("a") is not None and "url" in registry.row("a")


def test_into_puts_the_registry_back_and_keeps_everything_else():
    stripped = _registry().without({"a": ["api_key_cmd"]})

    payload = stripped.into(PAYLOAD)

    assert payload["air_gapped"] is False
    assert payload["models"]["default"] == "a"
    assert "api_key_cmd" not in payload["models"]["registry"]["a"]
    assert payload["models"]["registry"]["junk"] == "not a mapping"


def test_into_leaves_the_payload_it_was_given_as_it_was():
    original = copy.deepcopy(PAYLOAD)

    _registry().without({"a": ["url"]}).into(PAYLOAD)

    assert original == PAYLOAD


def test_into_a_payload_with_no_models_block_raises():
    with pytest.raises(KeyError):
        _registry().into({"a": 1})


def test_the_registry_is_immutable():
    with pytest.raises(ValidationError):
        _registry().entries = {}
