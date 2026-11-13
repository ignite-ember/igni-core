"""``GroupPolicyTier``: the pack's settings merged on top, minus what loosens a CLI deny.

Complements ``test_permission_deny_ratchet`` with the tier's own edges: no
pack, a pack without usable permissions, the record resetting on each apply,
and a fetcher that raises.
"""

from __future__ import annotations

import json

import pytest

from igni_core.core.config.accumulator import SettingsAccumulator
from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.tiers.cli import CliRatchet
from igni_core.core.config.tiers.group_policy import GroupPolicyTier
from tests.config_tier_rig import warnings_of

LOGGER = "igni_core.core.config.tiers.group_policy"


def _pack(payload: dict) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g",
        group_name="G",
        fetched_at=0.0,
        entries=[
            GroupPolicyEntry(
                kind="settings",
                entry_name="s",
                content=json.dumps(payload),
                content_type="json",
                enabled=True,
            )
        ],
    )


def test_a_user_without_a_group_keeps_the_accumulator_as_it_was():
    accumulator = SettingsAccumulator(payload={"a": 1})

    assert GroupPolicyTier(lambda: None).apply(accumulator) is accumulator


def test_the_pack_settings_win_over_what_is_there():
    tier = GroupPolicyTier(lambda: _pack({"permissions": {"web_search": "deny"}, "x": 2}))
    accumulator = SettingsAccumulator(
        payload={"permissions": {"web_search": "allow", "mode": "ask"}, "x": 1}
    )

    payload = tier.apply(accumulator).payload

    assert payload == {"permissions": {"web_search": "deny", "mode": "ask"}, "x": 2}


def test_the_fetcher_is_asked_once_per_apply():
    calls = []

    def fetcher():
        calls.append(1)
        return None

    tier = GroupPolicyTier(fetcher)
    tier.apply(SettingsAccumulator())
    tier.apply(SettingsAccumulator())

    assert len(calls) == 2


def test_a_pack_without_a_permissions_block_refuses_nothing():
    tier = GroupPolicyTier(lambda: _pack({"x": 1}), CliRatchet(denied=frozenset({"file_write"})))

    payload = tier.apply(SettingsAccumulator()).payload

    assert payload == {"x": 1}
    assert tier.refused_loosening == {}


def test_a_permissions_block_that_is_not_a_mapping_refuses_nothing():
    tier = GroupPolicyTier(
        lambda: _pack({"permissions": "allow"}), CliRatchet(denied=frozenset({"file_write"}))
    )

    with warnings_of(LOGGER) as warnings:
        tier.apply(SettingsAccumulator())

    assert tier.refused_loosening == {}
    assert warnings == []


def test_the_record_is_cleared_by_a_later_apply_with_nothing_to_refuse():
    packs = [{"permissions": {"file_write": "allow"}}, {"permissions": {"file_write": "deny"}}]
    tier = GroupPolicyTier(
        lambda: _pack(packs.pop(0)), CliRatchet(denied=frozenset({"file_write"}))
    )

    tier.apply(SettingsAccumulator())
    assert tier.refused_loosening == {"file_write": "allow"}

    tier.apply(SettingsAccumulator())
    assert tier.refused_loosening == {}


def test_the_refused_key_is_dropped_and_the_cli_deny_in_the_accumulator_stands():
    tier = GroupPolicyTier(
        lambda: _pack({"permissions": {"file_write": "allow", "web_search": "deny"}}),
        CliRatchet(denied=frozenset({"file_write"})),
    )
    accumulator = SettingsAccumulator(payload={"permissions": {"file_write": "deny"}})

    with warnings_of(LOGGER) as warnings:
        permissions = tier.apply(accumulator).payload["permissions"]

    assert permissions == {"file_write": "deny", "web_search": "deny"}
    assert len(warnings) == 1
    assert "file_write to 'allow'" in warnings[0]


def test_a_fetcher_that_raises_is_not_swallowed():
    def fetcher():
        raise RuntimeError("required policy is missing")

    with pytest.raises(RuntimeError, match="required policy"):
        GroupPolicyTier(fetcher).apply(SettingsAccumulator())
