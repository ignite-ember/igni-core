"""An organisation's mode is a floor a session cannot switch past (CC-40).

``Session.mode.set_permission_mode`` used to flip the live evaluator with no
check, so ``/accept`` and ``/bypass`` could move a session looser than
the mode its managed settings or group policy set. These tests go
through the real loader (a managed file on disk, a group pack through
the fetcher) and the real slash commands.

The other half is that nothing changes for anyone without a policy:
fully automatic stays one command, or one flag, away.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from igni_core.backend.command_handler import CommandHandler
from igni_core.cli.options import CliOptions
from igni_core.core.config.cli_overrides import CliOverrides
from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.managed_policy import ManagedPolicySource
from igni_core.core.config.permission_eval import (
    PermissionDecision,
    PermissionEvaluator,
    PermissionMode,
)
from igni_core.core.config.permission_eval.mode_policy import ModePolicy, is_at_least_as_strict
from igni_core.core.config.settings import Settings
from igni_core.core.config.settings_loader import SettingsLoader
from igni_core.core.session.broadcast import BroadcastBus
from igni_core.core.session.core import Session
from igni_core.core.session.schemas import PermissionModeSwitch
from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator
from igni_core.core.tools.slash import SlashCommandTool
from igni_core.protocol.messages import CommandResultKind
from tests.session_coordinators import wire_coordinators


@pytest.fixture(autouse=True)
def _no_managed_file(monkeypatch):
    """No stray OS-level managed file; tests that want one set it."""
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
                entry_name="modes",
                content=json.dumps(settings),
                content_type="json",
            )
        ],
    )


def _user_config(text: str) -> None:
    home = Path.home() / ".igni"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(text)


def _session(settings: Settings) -> Session:
    """A session as far as the mode commands reach: its settings and
    the evaluator built from them."""
    session = Session.__new__(Session)
    session.settings = settings
    session.broadcast_bus = BroadcastBus()
    session.permission_evaluator = PermissionEvaluator.from_strings(mode=settings.permissions.mode)
    wire_coordinators(session)
    return session


def _mode(session: Session) -> PermissionMode:
    return session.permission_evaluator.mode


# ── The ranking ──────────────────────────────────────────────────────


class TestTheRanking:
    def test_strictest_to_loosest(self):
        order = [
            PermissionMode.DEFAULT,
            PermissionMode.ACCEPT_EDITS,
            PermissionMode.BYPASS_PERMISSIONS,
        ]
        for i, floor in enumerate(order):
            for j, candidate in enumerate(order):
                assert is_at_least_as_strict(candidate, floor) is (j <= i)
        for strict in (PermissionMode.PLAN, PermissionMode.DONT_ASK):
            for floor in order:
                assert is_at_least_as_strict(strict, floor)

    def test_plan_and_dont_ask_do_not_stand_in_for_each_other(self):
        """``plan`` runs reads ``dontAsk`` would deny; ``dontAsk`` runs
        whatever its allow rules name, edits included. Neither floor
        admits the other."""
        assert not is_at_least_as_strict(PermissionMode.PLAN, PermissionMode.DONT_ASK)
        assert not is_at_least_as_strict(PermissionMode.DONT_ASK, PermissionMode.PLAN)


# ── A floor from managed settings or group policy ───────────────────


class TestAManagedFloor:
    @pytest.mark.asyncio
    async def test_accept_and_bypass_are_refused_below_a_managed_default(
        self, tmp_path, monkeypatch
    ):
        _managed(tmp_path, monkeypatch, "permissions:\n  mode: default\n")
        session = _session(SettingsLoader.load(project_dir=tmp_path))
        handler = CommandHandler(session)

        for command in ("/accept", "/accept on", "/bypass", "/bypass on"):
            result = await handler.handle(command)
            assert result.kind is CommandResultKind.ERROR, command
            assert "managed settings" in result.content
            assert _mode(session) is PermissionMode.DEFAULT

    @pytest.mark.asyncio
    async def test_a_stricter_mode_is_still_reachable(self, tmp_path, monkeypatch):
        _managed(tmp_path, monkeypatch, "permissions:\n  mode: acceptEdits\n")
        session = _session(SettingsLoader.load(project_dir=tmp_path))
        handler = CommandHandler(session)
        assert _mode(session) is PermissionMode.ACCEPT_EDITS

        await handler.handle("/plan on")
        assert _mode(session) is PermissionMode.PLAN
        # Back up to the floor is fine; past it is not.
        await handler.handle("/plan off")
        assert _mode(session) is PermissionMode.ACCEPT_EDITS
        result = await handler.handle("/bypass on")
        assert result.kind is CommandResultKind.ERROR
        assert _mode(session) is PermissionMode.ACCEPT_EDITS

    def test_a_group_policy_sets_a_floor_too(self, tmp_path):
        pack = _pack({"permissions": {"mode": "plan"}})
        settings = SettingsLoader.load(project_dir=tmp_path, group_policy_fetcher=lambda: pack)
        session = _session(settings)

        switch = session.mode.set_permission_mode("default")
        assert switch.refused
        assert "group policy" in switch.text
        assert _mode(session) is PermissionMode.PLAN

    def test_a_flag_cannot_lift_a_managed_floor(self, tmp_path, monkeypatch):
        _managed(tmp_path, monkeypatch, "permissions:\n  mode: default\n")
        settings = SettingsLoader.load(
            cli_overrides=CliOverrides.from_options(CliOptions(auto_approve=True)),
            project_dir=tmp_path,
        )
        assert settings.permissions.mode == "default"
        assert _session(settings).mode.set_permission_mode("acceptEdits").refused

    def test_a_user_config_mode_is_not_a_floor(self, tmp_path):
        """Only the organisation's tiers make a floor. A mode in the
        user's own config is where the session starts, nothing more."""
        _user_config("permissions:\n  mode: plan\n")
        session = _session(SettingsLoader.load(project_dir=tmp_path))
        assert _mode(session) is PermissionMode.PLAN
        assert "→ bypassPermissions" in session.mode.set_permission_mode("bypassPermissions").text


# ── allow_bypass: false ─────────────────────────────────────────────


class TestBypassCanBeForbidden:
    def test_the_flag_cannot_start_a_session_in_bypass(self, tmp_path, monkeypatch):
        _managed(tmp_path, monkeypatch, "permissions:\n  allow_bypass: false\n")
        settings = SettingsLoader.load(
            cli_overrides=CliOverrides.from_options(CliOptions(auto_approve=True)),
            project_dir=tmp_path,
        )
        assert settings.permissions.mode == "default"

    @pytest.mark.asyncio
    async def test_the_command_cannot_enter_bypass(self, tmp_path, monkeypatch):
        _managed(tmp_path, monkeypatch, "permissions:\n  allow_bypass: false\n")
        session = _session(SettingsLoader.load(project_dir=tmp_path))
        result = await CommandHandler(session).handle("/bypass on")
        assert result.kind is CommandResultKind.ERROR
        assert "allow_bypass" in result.content
        assert _mode(session) is PermissionMode.DEFAULT
        # Every other mode is still open: there is no floor.
        await CommandHandler(session).handle("/accept on")
        assert _mode(session) is PermissionMode.ACCEPT_EDITS

    @pytest.mark.asyncio
    async def test_the_tool_paths_cannot_enter_bypass(self, tmp_path):
        """What a tool or the HITL "accept all" flip reaches is
        ``Session.mode.set_permission_mode``; the agent's slash-command tool
        refuses ``/bypass`` before it gets that far."""
        pack = _pack({"permissions": {"allow_bypass": False}})
        settings = SettingsLoader.load(project_dir=tmp_path, group_policy_fetcher=lambda: pack)
        session = _session(settings)

        switch = session.mode.set_permission_mode("bypassPermissions")
        assert switch.refused
        assert "group policy" in switch.text
        assert _mode(session) is PermissionMode.DEFAULT

        reply = await SlashCommandTool(session).slash_command("/bypass on")
        assert _mode(session) is PermissionMode.DEFAULT
        assert "bypass" in reply.lower()

    def test_allow_bypass_from_user_or_project_config_is_ignored(self, tmp_path):
        _user_config("permissions:\n  allow_bypass: false\n")
        project = tmp_path / ".igni"
        project.mkdir()
        (project / "config.yaml").write_text("permissions:\n  allow_bypass: false\n")

        settings = SettingsLoader.load(
            cli_overrides=CliOverrides.from_options(CliOptions(auto_approve=True)),
            project_dir=tmp_path,
        )
        assert settings.permissions.mode_policy == ModePolicy()
        assert settings.permissions.mode == "bypassPermissions"
        assert "→" in _session(settings).mode.set_permission_mode("default").text


# ── off returns where the session started ───────────────────────────


class TestOffRestoresTheStartingMode:
    @pytest.mark.asyncio
    async def test_off_returns_to_the_starting_mode(self, tmp_path):
        _user_config("permissions:\n  mode: acceptEdits\n")
        session = _session(SettingsLoader.load(project_dir=tmp_path))
        handler = CommandHandler(session)

        await handler.handle("/bypass on")
        assert _mode(session) is PermissionMode.BYPASS_PERMISSIONS
        await handler.handle("/bypass off")
        assert _mode(session) is PermissionMode.ACCEPT_EDITS

        await handler.handle("/plan")
        await handler.handle("/plan")
        assert _mode(session) is PermissionMode.ACCEPT_EDITS

    @pytest.mark.asyncio
    async def test_leaving_the_starting_mode_itself_goes_to_default(self, tmp_path):
        _user_config("permissions:\n  mode: acceptEdits\n")
        session = _session(SettingsLoader.load(project_dir=tmp_path))
        await CommandHandler(session).handle("/accept off")
        assert _mode(session) is PermissionMode.DEFAULT

    @pytest.mark.asyncio
    async def test_off_under_a_floor_lands_on_the_floor(self, tmp_path, monkeypatch):
        _managed(tmp_path, monkeypatch, "permissions:\n  mode: acceptEdits\n")
        session = _session(SettingsLoader.load(project_dir=tmp_path))
        handler = CommandHandler(session)
        await handler.handle("/plan on")
        await handler.handle("/plan off")
        assert _mode(session) is PermissionMode.ACCEPT_EDITS


# ── No policy: fully automatic is still there ───────────────────────


class TestWithNoPolicyFullyAutomaticIsOneStepAway:
    @pytest.mark.asyncio
    async def test_bypass_runs_a_shell_command_without_asking(self, tmp_path):
        settings = SettingsLoader.load(project_dir=tmp_path)
        assert settings.permissions.mode_policy == ModePolicy()
        session = _session(settings)

        await CommandHandler(session).handle("/bypass on")
        assert _mode(session) is PermissionMode.BYPASS_PERMISSIONS
        decision = session.permission_evaluator.evaluate(
            "run_shell_command", {"command": "npm test"}
        )
        assert decision is PermissionDecision.ALLOW

    def test_the_auto_approve_flag_runs_a_shell_command_without_asking(self, tmp_path):
        settings = SettingsLoader.load(
            cli_overrides=CliOverrides.from_options(CliOptions(auto_approve=True)),
            project_dir=tmp_path,
        )
        assert settings.permissions.mode == "bypassPermissions"
        decision = _session(settings).permission_evaluator.evaluate(
            "run_shell_command", {"command": "npm test"}
        )
        assert decision is PermissionDecision.ALLOW


# ── Managed and group together: the stricter holds ──────────────────


class TestTheStricterTierWins:
    @pytest.mark.parametrize(
        ("managed", "group", "floor"),
        [
            ("acceptEdits", "default", PermissionMode.DEFAULT),
            ("default", "acceptEdits", PermissionMode.DEFAULT),
            ("bypassPermissions", "plan", PermissionMode.PLAN),
        ],
    )
    def test_the_stricter_floor_holds_whichever_tier_set_it(self, managed, group, floor):
        policy = ModePolicy.from_tiers(
            {"permissions": {"mode": managed}}, {"permissions": {"mode": group}}
        )
        assert policy.floor is floor
        looser = {
            PermissionMode.DEFAULT: PermissionMode.ACCEPT_EDITS,
            PermissionMode.PLAN: PermissionMode.DEFAULT,
        }[floor]
        assert policy.refusal(looser) is not None
        assert policy.refusal(floor) is None

    @pytest.mark.parametrize(
        ("managed", "group"),
        [(False, True), (True, False), (False, None), (None, False)],
    )
    def test_either_tier_can_turn_bypass_off(self, managed, group):
        def tier(value):
            return {} if value is None else {"permissions": {"allow_bypass": value}}

        policy = ModePolicy.from_tiers(tier(managed), tier(group))
        assert policy.allow_bypass is False
        assert policy.refusal(PermissionMode.BYPASS_PERMISSIONS) is not None

    def test_a_managed_true_does_not_undo_a_group_false(self, tmp_path, monkeypatch):
        _managed(tmp_path, monkeypatch, "permissions:\n  allow_bypass: true\n")
        pack = _pack({"permissions": {"allow_bypass": False}})
        settings = SettingsLoader.load(project_dir=tmp_path, group_policy_fetcher=lambda: pack)
        switch = _session(settings).mode.set_permission_mode("bypassPermissions")
        assert switch.refused
        assert "group policy" in switch.text


# ── --auto-approve under allow_bypass: false ────────────────────────


class TestAutoApproveIsIgnoredWhenBypassIsOff:
    def test_the_flag_loosens_nothing(self, tmp_path, monkeypatch, capsys):
        from igni_core.cli.run_settings import CliSettingsLoader

        _managed(tmp_path, monkeypatch, "permissions:\n  allow_bypass: false\n")
        monkeypatch.chdir(tmp_path)
        settings = CliSettingsLoader(CliOptions(auto_approve=True)).load()

        perms = settings.permissions
        assert perms.mode == "default"
        for field in ("file_write", "shell_execute", "git_push", "git_destructive"):
            assert getattr(perms, field) != "allow", field
        assert "--auto-approve is turned off" in capsys.readouterr().err

    def test_other_flags_still_apply(self, tmp_path, monkeypatch):
        from igni_core.cli.run_settings import CliSettingsLoader

        _managed(tmp_path, monkeypatch, "permissions:\n  allow_bypass: false\n")
        monkeypatch.chdir(tmp_path)
        settings = CliSettingsLoader(CliOptions(auto_approve=True, no_web=True)).load()
        assert settings.permissions.shell_execute != "allow"
        assert settings.permissions.web_fetch == "deny"

    def test_without_the_policy_the_flag_is_untouched(self, tmp_path, monkeypatch):
        from igni_core.cli.run_settings import CliSettingsLoader

        monkeypatch.chdir(tmp_path)
        settings = CliSettingsLoader(CliOptions(auto_approve=True)).load()
        assert settings.permissions.mode == "bypassPermissions"
        assert settings.permissions.shell_execute == "allow"


# ── The approval dialog's "accept all edits" flip ───────────────────


class TestARefusedFlipFromAnApprovalReachesThePerson:
    @pytest.mark.asyncio
    async def test_the_refusal_is_reported_and_the_approval_still_goes_through(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        from igni_core.backend.hitl_controller import HitlController
        from igni_core.backend.pending_requirements_store import PendingRequirementsStore
        from igni_core.backend.schemas_pause import PendingRequirement
        from igni_core.protocol import messages as msg

        async def nothing():
            if False:
                yield  # pragma: no cover

        refusal = (
            "Error: acceptEdits is looser than the default mode set by your "
            "organisation's managed settings; this session cannot leave default "
            "for a looser mode."
        )
        session = MagicMock()
        session.mode.set_permission_mode = MagicMock(
            return_value=PermissionModeSwitch(refused=True, text=refusal)
        )
        session.session_id = "s"
        session.main_team = MagicMock()
        session.main_team.acontinue_run = MagicMock(return_value=nothing())
        session.hook_executor = MagicMock()
        session.hook_executor.execute = AsyncMock(
            return_value=SimpleNamespace(should_continue=False, message="")
        )
        session.sub_agent_hitl = SubAgentHITLCoordinator()
        store = PendingRequirementsStore()
        store.register("req-1", PendingRequirement(req=MagicMock(), run_id="run-1"))

        decisions = [
            msg.HITLDecision(
                requirement_id="req-1",
                action="confirm",
                choice="once",
                set_permission_mode="acceptEdits",
            )
        ]
        out = [m async for m in HitlController(session, store).resolve_batch(decisions)]

        errors = [m for m in out if isinstance(m, msg.Error)]
        assert [e.text for e in errors] == [refusal]
        assert session.main_team.acontinue_run.call_count == 1
