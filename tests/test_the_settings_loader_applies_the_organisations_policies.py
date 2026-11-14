"""``SettingsLoader.load``: the organisation's policies, applied after the merge.

The tier order is pinned with the merge plan; here is what the loader adds on
top of it: the workspace-trust policy handed to the trust service it is given,
the starting mode clamped with a warning that says so, and the permission
fields an organisation set recorded as final.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from igni_core.core.config.cli_overrides import CliOverrides, PermissionOverrides
from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.managed_policy import ManagedPolicySource
from igni_core.core.config.permission_eval.schemas import PermissionMode
from igni_core.core.config.settings_loader import SettingsLoader
from igni_core.core.workspace_trust import WorkspaceTrust, process_trust, trust_service
from igni_core.core.workspace_trust.store import WorkspaceTrustStore


def managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str | None) -> None:
    path = None
    if text is not None:
        path = tmp_path / "managed-settings.yaml"
        path.write_text(text)
    monkeypatch.setattr(ManagedPolicySource, "platform_path", staticmethod(lambda: path))


def pack(payload: dict) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g1",
        group_name="Platform",
        fetched_at=datetime.now(timezone.utc),
        entries=[
            GroupPolicyEntry(
                kind="settings",
                entry_name="policy",
                content=json.dumps(payload),
                content_type="json",
            )
        ],
    )


def bypass() -> CliOverrides:
    return CliOverrides(permissions=PermissionOverrides(mode=PermissionMode.BYPASS_PERMISSIONS))


def trust(tmp_path: Path) -> WorkspaceTrust:
    return WorkspaceTrust(store=WorkspaceTrustStore(path=tmp_path / "trust.json"))


class TestTheStartingModeIsClamped:
    def test_a_mode_the_organisation_forbids_is_replaced_and_the_warning_names_both(
        self, tmp_path, monkeypatch, caplog
    ):
        managed(tmp_path, monkeypatch, "permissions:\n  allow_bypass: false\n")

        with caplog.at_level(logging.WARNING):
            settings = SettingsLoader.load(cli_overrides=bypass(), workspace_trust=trust(tmp_path))

        assert settings.permissions.mode == PermissionMode.DEFAULT
        assert (
            "Starting permission mode bypassPermissions is not allowed by your "
            "organisation's policy; starting in default instead."
        ) in caplog.messages

    def test_a_mode_it_allows_is_kept_and_nothing_is_said(self, tmp_path, monkeypatch, caplog):
        managed(tmp_path, monkeypatch, None)

        with caplog.at_level(logging.WARNING):
            settings = SettingsLoader.load(cli_overrides=bypass(), workspace_trust=trust(tmp_path))

        assert settings.permissions.mode == PermissionMode.BYPASS_PERMISSIONS
        assert [r for r in caplog.records if "organisation's policy" in r.getMessage()] == []

    def test_the_policy_is_recorded_on_the_permissions(self, tmp_path, monkeypatch):
        managed(tmp_path, monkeypatch, "permissions:\n  allow_bypass: false\n")

        settings = SettingsLoader.load(workspace_trust=trust(tmp_path))

        assert settings.permissions.mode_policy is not None
        assert settings.permissions.mode_policy.refusal(PermissionMode.BYPASS_PERMISSIONS)

    def test_a_group_pack_forbids_it_too(self, tmp_path, monkeypatch):
        managed(tmp_path, monkeypatch, None)

        settings = SettingsLoader.load(
            cli_overrides=bypass(),
            group_policy_fetcher=lambda: pack({"permissions": {"allow_bypass": False}}),
            workspace_trust=trust(tmp_path),
        )

        assert settings.permissions.mode == PermissionMode.DEFAULT


class TestTheTrustPolicy:
    def test_the_given_service_receives_the_organisations_policy(self, tmp_path, monkeypatch):
        managed(tmp_path, monkeypatch, "workspace_trust:\n  mode: never\n")
        given = trust(tmp_path)
        before = process_trust().policy

        SettingsLoader.load(workspace_trust=given)

        assert given.policy.mode == "never"
        assert process_trust().policy is before

    def test_without_one_the_process_service_receives_it(self, tmp_path, monkeypatch):
        managed(tmp_path, monkeypatch, "workspace_trust:\n  mode: never\n")
        process = trust(tmp_path)
        trust_service.replace(process)

        SettingsLoader.load()

        assert process.policy.mode == "never"

    def test_a_group_pack_sets_it_and_the_managed_file_overrides_it(self, tmp_path, monkeypatch):
        managed(tmp_path, monkeypatch, "workspace_trust:\n  mode: never\n")
        given = trust(tmp_path)

        SettingsLoader.load(
            group_policy_fetcher=lambda: pack({"workspace_trust": {"mode": "trusted_remotes"}}),
            workspace_trust=given,
        )
        assert given.policy.mode == "never"

        managed(tmp_path, monkeypatch, None)
        SettingsLoader.load(
            group_policy_fetcher=lambda: pack({"workspace_trust": {"mode": "trusted_remotes"}}),
            workspace_trust=given,
        )
        assert given.policy.mode == "trusted_remotes"


class TestWithoutAnOrganisation:
    def test_no_fetcher_no_managed_file_is_the_defaults(self, tmp_path, monkeypatch):
        managed(tmp_path, monkeypatch, None)

        settings = SettingsLoader.load(workspace_trust=trust(tmp_path))

        assert settings.permissions.mode == PermissionMode.DEFAULT
        assert settings.permissions.final_fields == frozenset()

    def test_a_fetcher_that_finds_no_pack_changes_nothing(self, tmp_path, monkeypatch):
        managed(tmp_path, monkeypatch, None)
        calls: list[int] = []

        def fetch():
            calls.append(1)

        settings = SettingsLoader.load(
            cli_overrides=bypass(), group_policy_fetcher=fetch, workspace_trust=trust(tmp_path)
        )

        assert calls
        assert settings.permissions.mode == PermissionMode.BYPASS_PERMISSIONS
        assert settings.permissions.final_fields == frozenset()


class TestFinalFields:
    def test_a_managed_level_is_recorded_so_it_reads_as_set(self, tmp_path, monkeypatch):
        managed(tmp_path, monkeypatch, "permissions:\n  shell_execute: ask\n")

        settings = SettingsLoader.load(workspace_trust=trust(tmp_path))

        assert settings.permissions.final_fields == frozenset({"shell_execute"})
        assert "run_shell_command" in settings.permissions.category_rules().ask

    def test_group_and_managed_fields_are_both_final(self, tmp_path, monkeypatch):
        managed(tmp_path, monkeypatch, "permissions:\n  shell_execute: ask\n")

        settings = SettingsLoader.load(
            group_policy_fetcher=lambda: pack({"permissions": {"web_fetch": "ask"}}),
            workspace_trust=trust(tmp_path),
        )

        assert settings.permissions.final_fields == frozenset({"shell_execute", "web_fetch"})
