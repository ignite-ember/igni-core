"""``Tier``, ``CliTier`` and ``CliRatchet``: what the command line sets and what it tightened.

The base tier is a no-op so a skipped layer passes the accumulator through.
The CLI tier merges the typed overrides, and the ratchet records which
permissions the CLI set to their tightest value (and ``hooks.group``), so
the tiers above it can refuse to loosen them.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.cli.options import CliOptions
from igni_core.core.config.accumulator import SettingsAccumulator
from igni_core.core.config.cli_overrides import CliOverrides
from igni_core.core.config.tiers.base import Tier
from igni_core.core.config.tiers.cli import CliRatchet, CliTier


def _overrides(**flags) -> CliOverrides:
    return CliOverrides.from_options(CliOptions(**flags))


def test_the_base_tier_hands_back_the_accumulator_it_was_given():
    accumulator = SettingsAccumulator(payload={"a": 1})

    assert Tier().apply(accumulator) is accumulator


def test_a_tier_that_does_not_override_apply_changes_nothing():
    class Skipped(Tier):
        pass

    accumulator = SettingsAccumulator(payload={"permissions": {"mode": "ask"}})

    assert Skipped().apply(accumulator).payload == {"permissions": {"mode": "ask"}}


def test_no_overrides_leave_the_accumulator_untouched():
    accumulator = SettingsAccumulator(payload={"a": 1})

    assert CliTier(None).apply(accumulator) is accumulator


def test_the_overrides_are_merged_over_what_is_there():
    accumulator = SettingsAccumulator(
        payload={"permissions": {"mode": "ask", "web_search": "allow"}}
    )
    overrides = _overrides(strict=True)

    merged = CliTier(overrides).apply(accumulator).payload

    assert merged["permissions"]["shell_execute"] == "deny"
    assert merged["permissions"]["web_search"] == "allow"
    assert merged["hooks"] == {"group": "ask"}


def test_an_empty_overrides_payload_changes_nothing():
    accumulator = SettingsAccumulator(payload={"a": 1})

    assert CliTier(_overrides()).apply(accumulator).payload == {"a": 1}


def test_no_overrides_make_an_empty_ratchet():
    ratchet = CliRatchet.of(None)

    assert ratchet.denied == frozenset()
    assert ratchet.hooks_group is None


def test_strict_records_its_denies_and_the_hooks_mode():
    ratchet = CliRatchet.of(_overrides(strict=True))

    assert {"shell_execute", "file_write"} <= ratchet.denied
    assert ratchet.hooks_group == "ask"


def test_read_only_is_a_ratchet_as_true_not_as_deny():
    ratchet = CliRatchet.of(_overrides(read_only=True))

    assert "read_only" in ratchet.denied
    assert CliRatchet.tightest("read_only") is True
    assert CliRatchet.tightest("shell_execute") == "deny"


def test_a_permission_the_cli_only_loosened_is_not_a_ratchet():
    ratchet = CliRatchet.of(_overrides(auto_approve=True))

    assert ratchet.denied == frozenset()


def test_a_cli_without_read_only_does_not_ratchet_read_only():
    assert "read_only" not in CliRatchet.of(_overrides()).denied


def test_the_refusals_name_each_loosened_field_sorted_with_the_value_tried():
    ratchet = CliRatchet(denied=frozenset({"web_search", "file_write", "read_only"}))

    refusals = ratchet.refusals(
        {"web_search": "allow", "file_write": "ask", "read_only": False, "mode": "allow"}
    )

    assert refusals == {"file_write": "ask", "read_only": "False", "web_search": "allow"}
    assert list(refusals) == ["file_write", "read_only", "web_search"]


def test_a_group_value_equal_to_the_tightest_is_not_a_refusal():
    ratchet = CliRatchet(denied=frozenset({"file_write", "read_only"}))

    assert ratchet.refusals({"file_write": "deny", "read_only": True}) == {}


def test_a_group_that_leaves_a_field_unset_or_none_is_not_refused():
    ratchet = CliRatchet(denied=frozenset({"file_write", "web_search"}))

    assert ratchet.refusals({"file_write": None}) == {}


def test_the_ratchet_cannot_be_changed_once_made():
    ratchet = CliRatchet.of(_overrides(strict=True))

    with pytest.raises(ValidationError):
        ratchet.denied = frozenset()
