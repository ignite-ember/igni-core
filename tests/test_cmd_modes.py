"""``/plan``, ``/accept`` and ``/bypass`` — one vocabulary, three modes.

The three commands share a single argument grammar and a single rule
for where "off" goes: back to the mode the session started in, which
under an organisation's floor is the floor. These tests drive the real
slash commands against a real evaluator and the real mode coordinator,
so a token that stops being understood, an "off" that lands somewhere
the policy forbids, or a refusal that is reported as a success shows up
as the user would see it.
"""

from __future__ import annotations

import pytest

from igni_core.backend.cmd_modes import cmd_accept, cmd_bypass, cmd_plan
from igni_core.backend.command_handler import CommandHandler
from igni_core.core.config.permission_eval import PermissionEvaluator, PermissionMode
from igni_core.core.config.settings import Settings
from igni_core.core.session.broadcast import BroadcastBus
from igni_core.core.session.core import Session
from igni_core.protocol.messages import CommandResultKind
from tests.session_coordinators import wire_coordinators


def _session(started_in: str = "default") -> Session:
    settings = Settings()
    settings.permissions.mode = started_in
    session = Session.__new__(Session)
    session.settings = settings
    session.broadcast_bus = BroadcastBus()
    session.permission_evaluator = PermissionEvaluator.from_strings(mode=started_in)
    wire_coordinators(session)
    return session


def _mode(session: Session) -> PermissionMode:
    return session.permission_evaluator.mode


class TestTheArgumentGrammar:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["/plan", "/accept", "/bypass"])
    @pytest.mark.parametrize("token", ["on", "enable", "start", "ON", "  Enable  "])
    async def test_an_on_token_enters_the_mode(self, command: str, token: str):
        session = _session()
        result = await CommandHandler(session).handle(f"{command} {token}")
        assert result.kind is CommandResultKind.MARKDOWN
        assert _mode(session) is {
            "/plan": PermissionMode.PLAN,
            "/accept": PermissionMode.ACCEPT_EDITS,
            "/bypass": PermissionMode.BYPASS_PERMISSIONS,
        }[command]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("command", "entered"),
        [
            ("/plan", PermissionMode.PLAN),
            ("/accept", PermissionMode.ACCEPT_EDITS),
            ("/bypass", PermissionMode.BYPASS_PERMISSIONS),
        ],
    )
    @pytest.mark.parametrize("token", ["off", "disable", "exit", "stop", "OFF"])
    async def test_an_off_token_leaves_the_mode(
        self, command: str, entered: PermissionMode, token: str
    ):
        session = _session()
        handler = CommandHandler(session)
        await handler.handle(f"{command} on")
        assert _mode(session) is entered
        await handler.handle(f"{command} {token}")
        assert _mode(session) is PermissionMode.DEFAULT

    @pytest.mark.asyncio
    async def test_a_bare_command_toggles_in_and_out(self):
        session = _session()
        handler = CommandHandler(session)
        await handler.handle("/accept")
        assert _mode(session) is PermissionMode.ACCEPT_EDITS
        await handler.handle("/accept toggle")
        assert _mode(session) is PermissionMode.DEFAULT

    @pytest.mark.asyncio
    async def test_a_toggle_from_another_mode_enters_the_target(self):
        """``/accept`` while in plan mode is not "leave accept": the
        session is not in accept, so the toggle goes in."""
        session = _session("plan")
        await CommandHandler(session).handle("/accept")
        assert _mode(session) is PermissionMode.ACCEPT_EDITS

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["/plan", "/accept", "/bypass"])
    @pytest.mark.parametrize("token", ["status", "show", "STATUS"])
    async def test_status_reports_the_mode_and_changes_nothing(self, command: str, token: str):
        session = _session("plan")
        session.plan.arm_research(False)
        result = await CommandHandler(session).handle(f"{command} {token}")
        assert result.kind is CommandResultKind.INFO
        assert result.content == "Permission mode: **plan**"
        assert _mode(session) is PermissionMode.PLAN
        assert session.plan.research_armed is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["/plan", "/accept", "/bypass"])
    async def test_an_unknown_token_is_an_error_naming_the_command(self, command: str):
        session = _session()
        result = await CommandHandler(session).handle(f"{command} maybe")
        assert result.kind is CommandResultKind.ERROR
        assert f"Unknown {command} argument: 'maybe'" in result.content
        assert f"{command} off" in result.content
        assert f"{command} status" in result.content
        assert _mode(session) is PermissionMode.DEFAULT

    @pytest.mark.asyncio
    async def test_the_reply_explains_entering_and_leaving(self):
        session = _session()
        handler = CommandHandler(session)
        entered = await handler.handle("/bypass on")
        assert "bypassPermissions" in entered.content
        assert "/bypass off" in entered.content
        left = await handler.handle("/bypass off")
        assert "bypassPermissions exited" in left.content

    @pytest.mark.asyncio
    async def test_the_module_bindings_take_a_handler_and_arguments(self):
        session = _session()
        handler = CommandHandler(session)
        await cmd_plan(handler, "on")
        assert _mode(session) is PermissionMode.PLAN
        await cmd_accept(handler, "on")
        assert _mode(session) is PermissionMode.ACCEPT_EDITS
        await cmd_bypass(handler, "on")
        assert _mode(session) is PermissionMode.BYPASS_PERMISSIONS


class TestLeavingReturnsToTheStartingMode:
    @pytest.mark.asyncio
    async def test_off_returns_to_the_mode_the_session_started_in(self):
        """A session that started in acceptEdits (the org floor) and
        went into plan returns to acceptEdits, not to default."""
        session = _session("acceptEdits")
        handler = CommandHandler(session)
        await handler.handle("/plan on")
        assert _mode(session) is PermissionMode.PLAN
        await handler.handle("/plan off")
        assert _mode(session) is PermissionMode.ACCEPT_EDITS

    @pytest.mark.asyncio
    async def test_off_in_the_mode_the_session_started_in_goes_to_default(self):
        session = _session("acceptEdits")
        await CommandHandler(session).handle("/accept off")
        assert _mode(session) is PermissionMode.DEFAULT

    @pytest.mark.asyncio
    async def test_a_bare_toggle_in_the_starting_mode_leaves_to_default(self):
        session = _session("plan")
        await CommandHandler(session).handle("/plan")
        assert _mode(session) is PermissionMode.DEFAULT


class TestThePlanResearchNudge:
    @pytest.mark.asyncio
    async def test_entering_plan_arms_it_and_leaving_disarms_it(self):
        session = _session()
        handler = CommandHandler(session)
        await handler.handle("/plan on")
        assert session.plan.research_armed is True
        await handler.handle("/plan off")
        assert session.plan.research_armed is False

    @pytest.mark.asyncio
    async def test_reissuing_on_in_plan_mode_does_not_rearm(self):
        session = _session("plan")
        session.plan.arm_research(False)
        await CommandHandler(session).handle("/plan on")
        assert session.plan.research_armed is False

    @pytest.mark.asyncio
    async def test_leaving_when_not_in_plan_does_not_disarm(self):
        """``/plan off`` from acceptEdits is not a transition out of
        plan, so a nudge armed for the next turn survives it."""
        session = _session("acceptEdits")
        session.plan.arm_research(True)
        await CommandHandler(session).handle("/plan off")
        assert session.plan.research_armed is True

    @pytest.mark.asyncio
    async def test_accept_and_bypass_never_touch_it(self):
        session = _session()
        handler = CommandHandler(session)
        await handler.handle("/accept on")
        await handler.handle("/bypass on")
        await handler.handle("/bypass off")
        assert session.plan.research_armed is False


class TestARefusedSwitch:
    @pytest.mark.asyncio
    async def test_it_is_an_error_with_the_refusal_and_fires_no_hook(self, monkeypatch):
        """Refusal is read from the typed switch, not guessed from the
        text. The nudge stays as it was and the mode does not move."""
        from igni_core.core.session.schemas import PermissionModeSwitch

        session = _session()
        refusal = PermissionModeSwitch(refused=True, text="Refused by managed settings.")
        monkeypatch.setattr(session.mode, "set_permission_mode", lambda _target: refusal)
        session.plan.arm_research(False)

        result = await CommandHandler(session).handle("/plan on")

        assert result.kind is CommandResultKind.ERROR
        assert result.content == "Refused by managed settings."
        assert session.plan.research_armed is False
        assert _mode(session) is PermissionMode.DEFAULT

    @pytest.mark.asyncio
    async def test_a_text_that_starts_with_error_is_not_a_refusal(self, monkeypatch):
        """The old check matched an ``Error`` prefix on the text; the
        typed ``refused`` flag is the only signal now."""
        from igni_core.core.session.schemas import PermissionModeSwitch

        session = _session()
        switch = PermissionModeSwitch(refused=False, text="Error-free mode switch.")
        monkeypatch.setattr(session.mode, "set_permission_mode", lambda _target: switch)

        result = await CommandHandler(session).handle("/plan on")

        assert result.kind is CommandResultKind.MARKDOWN
        assert result.content.startswith("Error-free mode switch.")
