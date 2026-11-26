"""``permissions.allow_terminal: false`` turns the window's terminal off.

Honoured only from the managed-settings file or the group policy, exactly
like ``allow_bypass``: either tier saying ``false`` is enough, and the same
field in a user's or a project's config changes nothing. Through the real
settings loader and the real ``terminal_open``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.managed_policy import ManagedPolicySource
from igni_core.core.config.permission_eval.mode_policy import ModePolicy
from igni_core.core.config.schemas.permissions import PermissionsConfig
from igni_core.core.config.settings_loader import SettingsLoader
from igni_core.protocol.rpc import RpcMethod
from igni_core.protocol.schemas.rpc import RpcErrorCode
from tests.terminal_helpers import Rig, ScriptedSpawn

OFF = "permissions:\n  allow_terminal: false\n"


@pytest.fixture(autouse=True)
def _no_managed_file(monkeypatch):
    monkeypatch.setattr(ManagedPolicySource, "platform_path", staticmethod(lambda: None))


def _managed(tmp_path: Path, monkeypatch, text: str) -> None:
    path = tmp_path / "managed-settings.yaml"
    path.write_text(text)
    monkeypatch.setattr(ManagedPolicySource, "platform_path", staticmethod(lambda: path))


def _pack(settings: dict) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g",
        group_name="Regulated",
        fetched_at=datetime.now(UTC),
        entries=[
            GroupPolicyEntry(
                kind="settings",
                entry_name="terminal",
                content=json.dumps(settings),
                content_type="json",
            )
        ],
    )


async def _open(policy: ModePolicy | None, tmp_path: Path) -> tuple[object, ScriptedSpawn]:
    spawn = ScriptedSpawn()
    rig = Rig(tmp_path, policy=policy, spawn=spawn)
    response = await rig.call(RpcMethod.TERMINAL_OPEN, {"cols": 80, "rows": 24})
    await rig.terminals.close_all()
    return response, spawn


# ── the policy ─────────────────────────────────────────────────────


def test_the_field_defaults_to_on():
    assert PermissionsConfig().allow_terminal is True
    assert ModePolicy().allow_terminal is True
    assert ModePolicy().terminal_refusal() is None


@pytest.mark.parametrize(
    ("managed", "group", "source"),
    [
        (False, None, "managed settings"),
        (None, False, "group policy"),
        (False, True, "managed settings"),
        (True, False, "group policy"),
        (False, False, "managed settings and group policy"),
    ],
)
def test_either_tier_can_turn_the_terminal_off(managed, group, source):
    def tier(value):
        return {} if value is None else {"permissions": {"allow_terminal": value}}

    policy = ModePolicy.from_tiers(tier(managed), tier(group))

    assert policy.allow_terminal is False
    assert policy.terminal_source == source
    assert policy.terminal_refusal() == (
        f"the terminal is turned off by your organisation's {source} "
        "(permissions.allow_terminal: false)"
    )


@pytest.mark.parametrize("value", [True, None, "false", 0])
def test_anything_but_false_leaves_it_on(value):
    tier = {} if value is None else {"permissions": {"allow_terminal": value}}
    policy = ModePolicy.from_tiers(tier, tier)
    assert policy.allow_terminal is True
    assert policy.terminal_source is None
    assert policy.terminal_refusal() is None


def test_turning_the_terminal_off_touches_no_mode():
    policy = ModePolicy.from_tiers({"permissions": {"allow_terminal": False}}, None)
    assert policy.allow_bypass is True
    assert policy.floors == ()


def test_a_refusal_without_a_source_names_the_policy():
    assert ModePolicy(allow_terminal=False).terminal_refusal() == (
        "the terminal is turned off by your organisation's policy "
        "(permissions.allow_terminal: false)"
    )


# ── through the loader ─────────────────────────────────────────────


def test_the_managed_file_turns_it_off(tmp_path, monkeypatch):
    _managed(tmp_path, monkeypatch, OFF)
    policy = SettingsLoader.load(project_dir=tmp_path).permissions.mode_policy
    assert policy is not None
    assert (policy.allow_terminal, policy.terminal_source) == (False, "managed settings")


def test_the_group_policy_turns_it_off(tmp_path):
    pack = _pack({"permissions": {"allow_terminal": False}})
    settings = SettingsLoader.load(project_dir=tmp_path, group_policy_fetcher=lambda: pack)
    policy = settings.permissions.mode_policy
    assert policy is not None
    assert (policy.allow_terminal, policy.terminal_source) == (False, "group policy")


def test_a_managed_true_does_not_undo_a_group_false(tmp_path, monkeypatch):
    _managed(tmp_path, monkeypatch, "permissions:\n  allow_terminal: true\n")
    pack = _pack({"permissions": {"allow_terminal": False}})
    settings = SettingsLoader.load(project_dir=tmp_path, group_policy_fetcher=lambda: pack)
    assert settings.permissions.mode_policy.terminal_refusal() is not None


def test_allow_terminal_from_user_or_project_config_is_ignored(tmp_path):
    home = Path.home() / ".igni"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(OFF)
    project = tmp_path / ".igni"
    project.mkdir()
    (project / "config.yaml").write_text(OFF)

    settings = SettingsLoader.load(project_dir=tmp_path)

    assert settings.permissions.mode_policy == ModePolicy()
    assert settings.permissions.mode_policy.terminal_refusal() is None


# ── what a window is told ──────────────────────────────────────────


async def test_terminal_open_is_refused_when_the_organisation_turned_it_off(tmp_path, monkeypatch):
    _managed(tmp_path, monkeypatch, OFF)
    policy = SettingsLoader.load(project_dir=tmp_path).permissions.mode_policy

    response, spawn = await _open(policy, tmp_path)

    assert response.code is RpcErrorCode.REFUSED
    assert response.error == (
        "terminal_open: the terminal is turned off by your organisation's managed settings "
        "(permissions.allow_terminal: false)"
    )
    assert spawn.made == []


async def test_terminal_open_answers_when_the_policy_allows_it(tmp_path, monkeypatch):
    _managed(tmp_path, monkeypatch, "permissions:\n  allow_terminal: true\n")
    policy = SettingsLoader.load(project_dir=tmp_path).permissions.mode_policy

    response, spawn = await _open(policy, tmp_path)

    assert response.error is None
    assert len(spawn.made) == 1


async def test_terminal_open_answers_with_no_policy_at_all(tmp_path):
    response, spawn = await _open(None, tmp_path)
    assert response.error is None
    assert len(spawn.made) == 1


async def test_the_policy_is_read_at_each_open(tmp_path):
    spawn = ScriptedSpawn()
    rig = Rig(tmp_path, policy=None, spawn=spawn)
    assert (await rig.call(RpcMethod.TERMINAL_OPEN, {"cols": 80, "rows": 24})).error is None

    rig.policy = ModePolicy(allow_terminal=False, terminal_source="group policy")
    refused = await rig.call(RpcMethod.TERMINAL_OPEN, {"cols": 80, "rows": 24})

    assert refused.code is RpcErrorCode.REFUSED
    assert "group policy" in (refused.error or "")
    assert len(spawn.made) == 1
    # A terminal already open keeps working.
    assert rig.terminals.count("window-1") == 1
    await rig.terminals.close_all()
