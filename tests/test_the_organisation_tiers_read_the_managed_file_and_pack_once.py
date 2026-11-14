"""``OrganisationTiers``: the managed file and the group pack, read once, on their own.

The workspace-trust policy, the mode policy and the final permission
fields are decided from these two tiers alone, and must see the very file
and pack the merge used. So each is asked for once per load, a missing or
unreadable one reads as empty, the managed file wins over the pack, and a
malformed trust policy fails closed.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pytest
import yaml

from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.organisation_tiers import OrganisationTiers
from igni_core.core.config.permission_eval.mode_policy import ModePolicy


def _pack(settings: dict | None = None, *, enabled: bool = True) -> GroupPolicyPack:
    entries = []
    if settings is not None:
        entries.append(
            GroupPolicyEntry(
                kind="settings",
                entry_name="s",
                content=json.dumps(settings),
                content_type="json",
                enabled=enabled,
            )
        )
    return GroupPolicyPack(
        group_id="g", group_name="Acme", fetched_at=datetime.now(timezone.utc), entries=entries
    )


class Counting:
    """A provider or fetcher that counts how often it is asked."""

    def __init__(self, value) -> None:
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value


def _tiers(managed: dict | str | None = None, pack: GroupPolicyPack | None = None, *, tmp_path):
    path = None
    if managed is not None:
        path = tmp_path / "managed.yaml"
        path.write_text(managed if isinstance(managed, str) else yaml.safe_dump(managed))
    provider = Counting(path)
    fetcher = Counting(pack)
    return OrganisationTiers(managed_path_provider=provider, group_policy_fetcher=fetcher), (
        provider,
        fetcher,
    )


class TestReadingEachTierOnce:
    def test_the_managed_path_is_asked_for_once_even_when_it_is_none(self, tmp_path):
        tiers, (provider, _) = _tiers(tmp_path=tmp_path)

        assert tiers.managed_path() is None
        assert tiers.managed_path() is None
        tiers.managed_settings()

        assert provider.calls == 1

    def test_the_pack_is_fetched_once_even_when_there_is_none(self, tmp_path):
        tiers, (_, fetcher) = _tiers(tmp_path=tmp_path)

        assert tiers.group_pack() is None
        assert tiers.group_pack() is None
        tiers.group_settings()
        tiers.workspace_trust_policy()

        assert fetcher.calls == 1

    def test_the_merge_plan_fetches_through_the_same_cache(self, tmp_path):
        pack = _pack({"a": 1})
        tiers, (_, fetcher) = _tiers(pack=pack, tmp_path=tmp_path)

        merge_fetch = tiers.group_policy_fetcher
        assert merge_fetch is not None
        assert merge_fetch() is pack
        assert tiers.group_pack() is pack
        assert fetcher.calls == 1

    def test_without_a_fetcher_there_is_no_pack_and_nothing_to_fetch_with(self, tmp_path):
        tiers = OrganisationTiers(managed_path_provider=lambda: None, group_policy_fetcher=None)

        assert tiers.group_pack() is None
        assert tiers.group_policy_fetcher is None
        assert tiers.group_settings() == {}

    def test_the_managed_file_is_parsed_once(self, tmp_path):
        tiers, _ = _tiers({"a": 1}, tmp_path=tmp_path)
        first = tiers.managed_settings()
        (tmp_path / "managed.yaml").write_text("a: 2\n")

        assert tiers.managed_settings() == {"a": 1}
        assert tiers.managed_settings() is first


class TestWhatEachTierSays:
    def test_no_managed_file_and_no_pack_is_empty(self, tmp_path):
        tiers, _ = _tiers(tmp_path=tmp_path)

        assert tiers.managed_settings() == {}
        assert tiers.group_settings() == {}

    def test_a_path_whose_file_is_missing_is_empty(self, tmp_path):
        tiers = OrganisationTiers(
            managed_path_provider=lambda: tmp_path / "absent.yaml", group_policy_fetcher=None
        )

        assert tiers.managed_settings() == {}

    @pytest.mark.parametrize("text", ["- a\n- b\n", "", "just a string\n"])
    def test_a_managed_file_that_is_not_a_mapping_is_empty(self, tmp_path, text):
        tiers, _ = _tiers(text, tmp_path=tmp_path)

        assert tiers.managed_settings() == {}

    def test_the_pack_settings_are_its_enabled_settings_entries(self, tmp_path):
        tiers, _ = _tiers(pack=_pack({"permissions": {"mode": "plan"}}), tmp_path=tmp_path)

        assert tiers.group_settings() == {"permissions": {"mode": "plan"}}

    def test_a_disabled_entry_says_nothing(self, tmp_path):
        tiers, _ = _tiers(pack=_pack({"a": 1}, enabled=False), tmp_path=tmp_path)

        assert tiers.group_settings() == {}


class TestTheWorkspaceTrustPolicy:
    def test_with_nothing_set_it_is_the_default_ask(self, tmp_path):
        tiers, _ = _tiers(tmp_path=tmp_path)

        assert tiers.workspace_trust_policy().mode == "ask"

    def test_the_group_sets_it(self, tmp_path):
        pack = _pack({"workspace_trust": {"mode": "trusted_remotes", "trusted_remotes": ["a/*"]}})
        tiers, _ = _tiers(pack=pack, tmp_path=tmp_path)

        policy = tiers.workspace_trust_policy()

        assert policy.mode == "trusted_remotes"
        assert policy.trusted_remotes == ["a/*"]

    def test_the_managed_file_overrides_the_group_field_by_field(self, tmp_path):
        pack = _pack(
            {"workspace_trust": {"mode": "trusted_remotes", "trusted_remotes": ["group/*"]}}
        )
        tiers, _ = _tiers({"workspace_trust": {"mode": "never"}}, pack, tmp_path=tmp_path)

        policy = tiers.workspace_trust_policy()

        assert policy.mode == "never"
        assert policy.trusted_remotes == ["group/*"]

    def test_a_block_that_is_not_a_mapping_in_one_tier_is_ignored(self, tmp_path):
        tiers, _ = _tiers(
            {"workspace_trust": "never"},
            _pack({"workspace_trust": {"mode": "trusted_remotes"}}),
            tmp_path=tmp_path,
        )

        assert tiers.workspace_trust_policy().mode == "trusted_remotes"

    def test_an_invalid_policy_fails_closed_and_says_so(self, tmp_path, caplog):
        tiers, _ = _tiers({"workspace_trust": {"mode": "whenever"}}, tmp_path=tmp_path)

        with caplog.at_level(logging.WARNING, logger="igni_core.core.config.organisation_tiers"):
            policy = tiers.workspace_trust_policy()

        assert policy.mode == "never"
        assert "treating every folder as untrusted" in caplog.text


class TestThePermissionFieldsThatAreFinal:
    def test_nothing_set_means_nothing_final(self, tmp_path):
        tiers, _ = _tiers(tmp_path=tmp_path)

        assert tiers.final_permission_fields() == frozenset()

    def test_a_field_set_by_either_tier_is_final(self, tmp_path):
        tiers, _ = _tiers(
            {"permissions": {"mode": "plan", "allow": []}},
            _pack({"permissions": {"deny": [], "allow_bypass": False}}),
            tmp_path=tmp_path,
        )

        assert tiers.final_permission_fields() == frozenset(
            {"mode", "allow", "deny", "allow_bypass"}
        )

    def test_permissions_that_are_not_a_mapping_set_nothing(self, tmp_path):
        tiers, _ = _tiers(
            {"permissions": ["x"]}, _pack({"permissions": "strict"}), tmp_path=tmp_path
        )

        assert tiers.final_permission_fields() == frozenset()

    def test_keys_are_coerced_to_text(self, tmp_path):
        tiers, _ = _tiers({"permissions": {1: "x"}}, tmp_path=tmp_path)

        assert tiers.final_permission_fields() == frozenset({"1"})


class TestTheModePolicy:
    def test_it_is_built_from_the_two_tiers_alone(self, tmp_path):
        tiers, _ = _tiers(
            {"permissions": {"mode": "plan"}},
            _pack({"permissions": {"allow_bypass": False}}),
            tmp_path=tmp_path,
        )

        policy = tiers.mode_policy()

        assert policy.allow_bypass is False
        assert policy.bypass_source == ModePolicy.GROUP
        assert policy.floor is not None
        assert policy.floor.value == "plan"

    def test_with_no_tiers_there_is_no_floor_and_bypass_is_allowed(self, tmp_path):
        policy = _tiers(tmp_path=tmp_path)[0].mode_policy()

        assert policy.allow_bypass is True
        assert policy.floor is None
