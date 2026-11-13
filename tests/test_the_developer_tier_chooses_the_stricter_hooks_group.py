"""``DeveloperOnlyTier``: ``hooks.group`` is the stricter of the user's config and the CLI.

Anything an upstream tier put in ``hooks.group`` is discarded, with a warning
only when it differs from the value that stands.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.config.accumulator import SettingsAccumulator
from igni_core.core.config.tiers.cli import CliRatchet
from igni_core.core.config.tiers.developer import DeveloperOnlyTier
from tests.config_tier_rig import warnings_of

LOGGER = "igni_core.core.config.tiers.developer"


def _user(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def _group(tier: DeveloperOnlyTier, payload: dict | None = None) -> str:
    return tier.apply(SettingsAccumulator(payload=payload or {})).payload["hooks"]["group"]


def test_with_no_user_config_and_no_cli_the_group_hooks_run():
    assert _group(DeveloperOnlyTier(None)) == "run"


def test_with_no_user_config_the_cli_value_stands():
    tier = DeveloperOnlyTier(None, CliRatchet(hooks_group="ask"))

    assert _group(tier) == "ask"


def test_a_user_config_that_does_not_exist_reads_as_nothing(tmp_path: Path):
    assert _group(DeveloperOnlyTier(tmp_path / "absent.yaml")) == "run"


@pytest.mark.parametrize("text", ["- a\n", "hooks: nope\n", "hooks:\n  group:\n", "hooks: {}\n"])
def test_a_user_config_that_sets_nothing_usable_reads_as_run(tmp_path: Path, text: str):
    assert _group(DeveloperOnlyTier(_user(tmp_path, text))) == "run"


def test_the_stricter_of_the_user_and_the_cli_wins_both_ways(tmp_path: Path):
    refuse = _user(tmp_path, "hooks:\n  group: refuse\n")
    assert _group(DeveloperOnlyTier(refuse, CliRatchet(hooks_group="ask"))) == "refuse"

    run = _user(tmp_path, "hooks:\n  group: run\n")
    assert _group(DeveloperOnlyTier(run, CliRatchet(hooks_group="ask"))) == "ask"


def test_a_typo_in_the_user_config_reads_as_ask_and_says_so(tmp_path: Path):
    path = _user(tmp_path, "hooks:\n  group: rnu\n")

    with warnings_of(LOGGER) as warnings:
        value = _group(DeveloperOnlyTier(path))

    assert value == "ask"
    assert len(warnings) == 1
    assert "'rnu'" in warnings[0] and "Using 'ask'" in warnings[0]


def test_a_value_from_a_lower_or_group_tier_is_discarded_with_a_warning():
    tier = DeveloperOnlyTier(None, CliRatchet(hooks_group="ask"))

    with warnings_of(LOGGER) as warnings:
        value = _group(tier, {"hooks": {"group": "run", "other": 1}})

    assert value == "ask"
    assert len(warnings) == 1
    assert "'run'" in warnings[0] and "'ask' stands" in warnings[0]


def test_an_upstream_value_equal_to_the_one_that_stands_is_not_warned_about():
    with warnings_of(LOGGER) as warnings:
        value = _group(DeveloperOnlyTier(None), {"hooks": {"group": "run"}})

    assert value == "run"
    assert warnings == []


def test_other_hooks_settings_and_the_rest_of_the_payload_survive():
    accumulator = SettingsAccumulator(payload={"hooks": {"timeout": 5}, "a": 1})

    payload = DeveloperOnlyTier(None).apply(accumulator).payload

    assert payload == {"hooks": {"timeout": 5, "group": "run"}, "a": 1}


def test_a_null_hooks_block_is_replaced():
    assert _group(DeveloperOnlyTier(None), {"hooks": None}) == "run"


def test_the_accumulator_it_was_given_is_not_changed():
    accumulator = SettingsAccumulator(payload={"hooks": {"group": "refuse"}})

    DeveloperOnlyTier(None).apply(accumulator)

    assert accumulator.payload == {"hooks": {"group": "refuse"}}
