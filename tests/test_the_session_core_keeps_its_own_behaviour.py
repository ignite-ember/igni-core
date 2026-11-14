"""What :class:`Session` itself does, as opposed to what it delegates.

A real session, composed on a throwaway project (``tests/session_factory``).
Pinned here, one by one:

* the model switch refuses an unknown name and says what is available;
* a refreshed group pack rebuilds the pools and the team, and a failure
  to do so is reported as ``False`` rather than raised;
* the ``SessionStart`` hook gets the session's id and whether it was
  resumed, and a hook that raises never stops startup;
* ``/hooks reload`` replaces the team's tool-event hook (and only that);
* a trust change adopts a fresh evaluator, survives a manager that will
  not stop, and keeps the old settings when none are given;
* the permission mode, memory hooks and the debug dump at their edges.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from igni_core.backend.command_handler import CommandHandler
from igni_core.core.activity.background import background
from igni_core.core.config.group_policy.revalidation import PolicyRevalidation
from igni_core.core.config.settings import Settings
from igni_core.core.hooks.memory_sync import MemorySyncHook
from igni_core.core.hooks.tool_hook import ToolEventHook
from igni_core.core.hooks.turn_results import TurnResultHook
from igni_core.core.mcp.hints import ToolHintsRegistry
from igni_core.core.session.parts.agents import AgentsBuilder
from igni_core.core.session.run_debug import RunMessagesDebugDumper
from igni_core.protocol.messages import CommandAction
from tests.session_factory import build_session


@pytest.fixture
def session(tmp_path: Path):
    return build_session(tmp_path)


def test_a_fresh_session_has_no_evaluator_and_no_dialogue_hook(session) -> None:
    assert session.on_new_dialogue is None


def test_a_session_keeps_the_tool_hints_it_is_given(tmp_path: Path) -> None:
    hints = ToolHintsRegistry()

    assert build_session(tmp_path, tool_hints=hints).tool_hints is hints


class TestSetDefaultModel:
    def test_an_unknown_name_changes_nothing_and_lists_the_models(self, session) -> None:
        before = session.settings.models.default

        result = session.set_default_model("nope")

        assert result.ok is False
        assert result.model_name == "nope"
        assert result.available == sorted(session.settings.models.registry)
        assert session.settings.models.default == before

    def test_an_empty_name_is_unknown(self, session) -> None:
        assert session.set_default_model("").ok is False

    def test_a_known_name_is_made_the_default_and_the_team_rebuilt(self, session) -> None:
        session.settings.models.registry["second"] = session.settings.models.registry["stub"]
        team_before = session.main_team

        result = session.set_default_model("second")

        assert result.ok is True
        assert session.settings.models.default == "second"
        assert session.main_team is not team_before


class TestReloadGroupAgents:
    def test_a_refreshed_pack_rebuilds_the_pools_and_the_team(self, session) -> None:
        team_before = session.main_team

        assert session.reload_group_agents() is True
        assert session.main_team is not team_before

    def test_a_failed_rebuild_is_reported_not_raised(
        self, session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def boom(self: Any) -> None:
            raise RuntimeError("pool build broke")

        monkeypatch.setattr(AgentsBuilder, "build_pools", boom)

        with caplog.at_level(logging.WARNING):
            assert session.reload_group_agents() is False
        assert "pool build broke" in caplog.text

    def test_a_failed_team_build_is_reported_not_raised(
        self, session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(self: Any) -> None:
            raise RuntimeError("team build broke")

        monkeypatch.setattr(AgentsBuilder, "build_main_team", boom)

        assert session.reload_group_agents() is False


class TestSessionStartHook:
    class _Executor:
        def __init__(self, error: Exception | None = None) -> None:
            self.calls: list[tuple[str, Any]] = []
            self.error = error

        async def execute(self, *, event: str, payload: Any) -> None:
            self.calls.append((event, payload))
            if self.error:
                raise self.error

    async def test_the_hook_gets_the_id_and_whether_the_session_was_resumed(
        self, session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        executor = self._Executor()
        session.hook_executor = executor  # type: ignore[assignment]
        session.identity.set_named(True)

        await session.fire_session_start_hook()

        [(event, payload)] = executor.calls
        assert event == "SessionStart"
        assert payload.session_id == session.session_id
        assert payload.resumed is True

    async def test_a_new_session_is_not_resumed(
        self, session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        executor = self._Executor()
        session.hook_executor = executor  # type: ignore[assignment]
        session.identity.set_named(False)

        await session.fire_session_start_hook()

        assert executor.calls[0][1].resumed is False

    async def test_a_hook_that_raises_does_not_stop_startup_and_is_logged(
        self,
        session,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        session.hook_executor = self._Executor(RuntimeError("hook broke"))  # type: ignore[assignment]

        with caplog.at_level(logging.WARNING):
            await session.fire_session_start_hook()

        assert "hook broke" in caplog.text


class TestReloadHooks:
    def test_the_team_gets_a_new_tool_event_hook_and_keeps_its_other_hooks(self, session) -> None:
        def other(*_: Any) -> None: ...

        old = [h for h in session.main_team.tool_hooks if isinstance(h, ToolEventHook)]
        session.main_team.tool_hooks = [*session.main_team.tool_hooks, other]

        session.reload_hooks()

        tool_hooks = session.main_team.tool_hooks
        events = [h for h in tool_hooks if isinstance(h, ToolEventHook)]
        assert len(events) == 1
        assert all(events[0] is not o for o in old)
        assert other in tool_hooks

    def test_it_does_not_rebuild_the_team(self, session) -> None:
        team = session.main_team

        session.reload_hooks()

        assert session.main_team is team

    def test_it_answers_how_many_hooks_are_loaded(self, session) -> None:
        assert session.reload_hooks() == session.hooks._registry.total_hooks


class TestToolEventHook:
    def test_it_re_reads_the_live_evaluator(self, session) -> None:
        session.permission_evaluator = None

        hook = session.tool_event_hook()

        assert isinstance(hook, ToolEventHook)
        assert session.permission_evaluator is session.tool_hook_factory.permission_evaluator

    def test_each_call_makes_a_fresh_hook(self, session) -> None:
        assert session.tool_event_hook() is not session.tool_event_hook()


class TestPermissionModeValue:
    def test_no_evaluator_reads_as_default(self, session) -> None:
        session.permission_evaluator = None

        assert session.permission_mode_value == "default"

    def test_the_evaluators_mode_is_the_wire_string(self, session) -> None:
        session.tool_event_hook()

        assert session.permission_mode_value == session.permission_evaluator.mode.value


class TestMemorySyncHooks:
    def test_without_a_mirror_only_the_turn_results_are_kept(self, session) -> None:
        session.memory_mirror = None

        [hook] = session.memory_sync_hooks()

        assert isinstance(hook, TurnResultHook)

    def test_with_a_mirror_the_memory_writes_are_stored_too(self, session) -> None:
        session.memory_mirror = object()  # type: ignore[assignment]

        hooks = session.memory_sync_hooks()

        assert [type(h) for h in hooks] == [TurnResultHook, MemorySyncHook]


class TestApplyWorkspaceTrust:
    async def test_without_settings_the_current_ones_stay(self, session) -> None:
        settings = session.settings

        await session.apply_workspace_trust()

        assert session.settings is settings

    async def test_fresh_settings_replace_the_old(self, session) -> None:
        fresh = Settings()
        fresh.models = session.settings.models

        await session.apply_workspace_trust(fresh)

        assert session.settings is fresh

    async def test_the_evaluator_adopts_the_new_permissions_and_keeps_the_mode(
        self, session
    ) -> None:
        evaluator = session.tool_hook_factory.permission_evaluator
        mode = evaluator.mode

        await session.apply_workspace_trust()

        assert session.tool_hook_factory.permission_evaluator is evaluator
        assert evaluator.mode == mode

    async def test_both_managers_are_stopped_even_when_one_will_not_stop(
        self, session, caplog: pytest.LogCaptureFixture
    ) -> None:
        stopped: list[str] = []

        class _Stuck:
            async def shutdown_all(self) -> None:
                stopped.append("lsp")
                raise RuntimeError("will not stop")

        class _Fine:
            async def shutdown_all(self) -> None:
                stopped.append("monitor")

        session.lsp_manager = _Stuck()  # type: ignore[assignment]
        session.monitor_manager = _Fine()  # type: ignore[assignment]

        with caplog.at_level(logging.WARNING):
            await session.apply_workspace_trust()

        assert stopped == ["lsp", "monitor"]
        assert "_Stuck" in caplog.text
        assert "will not stop" in caplog.text


class TestHandleMessage:
    class _Handler:
        last_failure = "the model refused"

        async def handle(self, message: str, media: Any) -> str:
            return f"reply to {message}"

    async def test_a_debug_dump_that_raises_does_not_fail_the_turn(
        self, session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(team: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(RunMessagesDebugDumper, "dump_team", staticmethod(boom))
        session.message_handler = self._Handler()  # type: ignore[assignment]

        assert await session.handle_message("hi") == "reply to hi"

    def test_the_last_failure_is_the_handlers(self, session) -> None:
        session.message_handler = self._Handler()  # type: ignore[assignment]

        assert session.last_turn_failure == "the model refused"


class TestContextBudget:
    def test_a_rebuild_resizes_to_the_new_models_window(self, session) -> None:
        session.settings.models.registry["wide"] = session.settings.models.registry[
            "stub"
        ].model_copy(update={"context_window": 4096})

        session.set_default_model("wide")

        assert session.context_budget.window == 4096


class TestANewDialogueOnARealSession:
    """``/clear`` through a real session: the id rotates everywhere and the
    server's answer about the group pack comes back as a result."""

    async def test_an_unreachable_portal_is_a_warning_in_the_result_not_a_raise(
        self, session, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def revalidate() -> PolicyRevalidation:
            return PolicyRevalidation(error="portal unreachable")

        session.on_new_dialogue = revalidate
        old = session.session_id

        with caplog.at_level(logging.WARNING, logger="igni_core.backend.cmd_session"):
            result = await CommandHandler(session).handle("/clear")
            await background.drain()

        assert result.action == CommandAction.CLEAR
        assert session.session_id != old
        assert "group-policy revalidation failed: portal unreachable" in caplog.text

    async def test_a_revalidation_that_changed_nothing_says_nothing(
        self, session, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def revalidate() -> PolicyRevalidation:
            return PolicyRevalidation(changed=False)

        session.on_new_dialogue = revalidate

        with caplog.at_level(logging.WARNING, logger="igni_core.backend.cmd_session"):
            await CommandHandler(session).handle("/clear")
            await background.drain()

        assert "revalidation" not in caplog.text

    async def test_the_new_id_reaches_the_team_the_persister_and_the_message_handler(
        self, session
    ) -> None:
        await CommandHandler(session).handle("/clear")
        await background.drain()

        new = session.session_id
        assert session.main_team.session_id == new
        assert session.persistence.session_id == new
        # The handler is built once and never rebuilt: it must read the live id.
        assert session.message_handler._deps.session_id_ref() == new
