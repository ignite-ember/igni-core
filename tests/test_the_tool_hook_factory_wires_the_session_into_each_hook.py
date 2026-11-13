"""What :class:`ToolEventHookFactory` hands each hook it builds.

The factory owns the session's permission evaluator and builds a fresh
:class:`ToolEventHook` for the main team on every plugin reload. These run
the real factory and drive the hooks it creates (a tool call through the
hook), reading what the executor was told, what was refused and what the
Bypass guard asked.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agno.tools.function import Function

from igni_core.core.config.permission_eval import (
    PermissionDecision,
    PermissionEvaluator,
    PermissionMode,
)
from igni_core.core.config.settings import Settings
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.mcp.hints import ToolHints, ToolHintsRegistry
from igni_core.core.session.tool_hook_factory import ToolEventHookFactory
from igni_core.core.utils.rules_index import RulesIndex


class RecordingExecutor(HookExecutor):
    def __init__(self) -> None:
        super().__init__({})
        self.events: list[tuple[str, str]] = []

    async def execute(self, *, event: str, payload: Any, **kwargs: Any):  # type: ignore[override]
        self.events.append((event, payload.session_id))
        return await super().execute(event=event, payload=payload, **kwargs)


def _factory(
    tmp_path: Path,
    *,
    settings: Settings | None = None,
    evaluator: PermissionEvaluator | None = None,
    tool_hints: ToolHintsRegistry | None = None,
    session_id: list[str] | None = None,
    executor: RecordingExecutor | None = None,
) -> tuple[ToolEventHookFactory, SimpleNamespace, list[str]]:
    ids = session_id if session_id is not None else ["s-1"]
    hooks = SimpleNamespace(executor=executor or RecordingExecutor())
    factory = ToolEventHookFactory(
        settings or Settings(),
        RulesIndex(tmp_path),
        tmp_path,
        hooks,  # type: ignore[arg-type]
        lambda: ids[-1],
        permission_evaluator=evaluator,
        tool_hints=tool_hints,
    )
    return factory, hooks, ids


def _ran(result: Any) -> bool:
    return "TOOL-RAN-OK" in str(result)


async def _call(hook, name: str = "read_file", args: dict | None = None, agent: Any = None) -> Any:
    async def tool(**_: Any) -> str:
        return "TOOL-RAN-OK"

    return await hook(name=name, func=tool, args=args or {"path": "f.py"}, agent=agent)


class TestTheEvaluator:
    def test_it_is_built_once_and_every_access_is_the_same_object(self, tmp_path):
        factory, _, _ = _factory(tmp_path)

        assert factory.permission_evaluator is factory.permission_evaluator

    def test_it_starts_in_the_mode_the_settings_name(self, tmp_path):
        settings = Settings()
        settings.permissions.mode = PermissionMode.PLAN
        factory, _, _ = _factory(tmp_path, settings=settings)

        assert factory.permission_evaluator.mode is PermissionMode.PLAN

    def test_a_seeded_evaluator_is_the_one_used_whatever_the_settings_say(self, tmp_path):
        seed = PermissionEvaluator.from_strings(mode="acceptEdits")
        factory, _, _ = _factory(tmp_path, evaluator=seed)

        assert factory.permission_evaluator is seed

    def test_a_mode_flipped_on_it_survives_the_next_hook_being_built(self, tmp_path):
        factory, _, _ = _factory(tmp_path)
        factory.permission_evaluator.set_mode(PermissionMode.PLAN)
        factory.create()

        assert factory.permission_evaluator.mode is PermissionMode.PLAN

    def test_without_a_hints_table_the_catalog_is_left_to_the_default(self, tmp_path):
        factory, _, _ = _factory(tmp_path)

        assert factory._catalog() is None

    def test_with_a_hints_table_the_modes_classify_by_that_table(self, tmp_path):
        table = ToolHintsRegistry()
        table.declare(
            "x",
            {
                "mcp_ext:x.read": ToolHints(
                    read_only=True, destructive=False, idempotent=True, open_world=False
                )
            },
        )
        factory, _, _ = _factory(tmp_path, tool_hints=table)
        factory.permission_evaluator.set_mode(PermissionMode.PLAN)

        catalog = factory._catalog()
        assert catalog is not None and catalog.tool_hints is table
        evaluator = factory.permission_evaluator
        assert evaluator.evaluate("mcp_ext:x.read", {}) is PermissionDecision.ALLOW
        assert evaluator.evaluate("mcp_ext:x.other", {}) is PermissionDecision.DEFER


class TestInBypass:
    def test_it_is_true_only_in_bypass_mode_and_follows_the_live_mode(self, tmp_path):
        factory, _, _ = _factory(tmp_path)
        assert factory.in_bypass is False

        factory.permission_evaluator.set_mode(PermissionMode.BYPASS_PERMISSIONS)
        assert factory.in_bypass is True

        factory.permission_evaluator.set_mode(PermissionMode.ACCEPT_EDITS)
        assert factory.in_bypass is False


class TestCreate:
    async def test_the_hook_reports_to_the_current_executor_with_the_current_session_id(
        self, tmp_path
    ):
        first = RecordingExecutor()
        factory, hooks, ids = _factory(tmp_path, executor=first)

        assert _ran(await _call(factory.create()))
        assert ("PreToolUse", "s-1") in first.events

        # The session replaced its executor (a hooks reload) and rebound its id.
        second = RecordingExecutor()
        hooks.executor = second
        ids.append("s-2")
        await _call(factory.create())

        assert ("PreToolUse", "s-2") in second.events
        assert all(sid == "s-1" for _, sid in first.events)

    async def test_the_hook_judges_a_call_by_the_cached_evaluators_live_mode(self, tmp_path):
        factory, _, _ = _factory(tmp_path)
        hook = factory.create()
        assert _ran(await _call(hook, "Write", {"path": "a.txt", "content": "x"}))

        factory.permission_evaluator.set_mode(PermissionMode.PLAN)
        refused = await _call(factory.create(), "Write", {"path": "a.txt", "content": "x"})

        assert not _ran(refused)

    async def test_the_configured_protected_paths_and_blocked_commands_reach_the_hook(
        self, tmp_path
    ):
        settings = Settings()
        settings.safety.protected_paths = ["secret.txt"]
        settings.safety.blocked_commands = ["frobnicate"]
        factory, _, _ = _factory(tmp_path, settings=settings)

        blocked_path = await _call(factory.create(), "save_file", {"file_name": "secret.txt"})
        blocked_cmd = await _call(
            factory.create(), "run_shell_command", {"command": "frobnicate --all"}
        )

        assert not _ran(blocked_path)
        assert not _ran(blocked_cmd)

    async def test_a_guard_and_asker_set_later_reach_the_hooks_created_after(self, tmp_path):
        factory, _, _ = _factory(tmp_path)
        factory.permission_evaluator.set_mode(PermissionMode.BYPASS_PERMISSIONS)
        asked: list[str] = []

        class Guard:
            async def check(self, tool_name: str, args: dict, agent_path: list[str] | None = None):
                asked.append(tool_name)
                return "Blocked by the test guard."

        assert _ran(await _call(factory.create()))
        assert asked == []

        factory.bypass_guard = Guard()  # type: ignore[assignment]
        assert await _call(factory.create()) == "Blocked by the test guard."
        assert asked == ["read_file"]

    async def test_an_asker_set_later_is_asked_about_a_call_that_skipped_its_pause(self, tmp_path):
        # A tool built without a pause, under a rule that asks about writes.
        factory, _, _ = _factory(
            tmp_path, evaluator=PermissionEvaluator.from_strings(mode="default", ask=["Write"])
        )
        write = Function(name="Write", entrypoint=lambda **_: "x", requires_confirmation=False)
        agent = SimpleNamespace(tools=[write])
        args = {"file_name": "a.txt"}
        asked: list[str] = []

        async def ask_person(tool_name, tool_args, reason, agent_path):
            asked.append(tool_name)
            return True

        # Nobody to ask: refused.
        assert not _ran(await _call(factory.create(), "Write", args, agent))
        assert asked == []

        factory.ask_person = ask_person
        assert _ran(await _call(factory.create(), "Write", args, agent))
        assert asked == ["Write"]


class TestBuildBypassGuard:
    def _asked(self, tmp_path, *, explain: bool, timeout: float = 30.0, model_ref=None):
        settings = Settings()
        settings.safety.bypass_guard.use_model = False
        settings.safety.bypass_guard.explain = explain
        settings.safety.bypass_guard.explain_timeout = timeout
        factory, _, _ = _factory(tmp_path, settings=settings)
        calls: list[dict[str, Any]] = []

        async def ask(tool_name, args, reason, agent_path, *, follow_up=None):
            calls.append({"reason": reason, "follow_up": follow_up})
            return True

        guard = factory.build_bypass_guard(
            ask, model_ref or (lambda: SimpleNamespace()), ToolHintsRegistry()
        )
        return factory, guard, calls

    async def test_the_guard_asks_only_while_the_session_is_in_bypass(self, tmp_path):
        factory, guard, calls = self._asked(tmp_path, explain=False)
        command = {"command": "rm -rf ~"}

        assert await guard.check("run_shell_command", command) is None
        assert calls == []

        factory.permission_evaluator.set_mode(PermissionMode.BYPASS_PERMISSIONS)
        assert await guard.check("run_shell_command", command) is None
        assert len(calls) == 1

        factory.permission_evaluator.set_mode(PermissionMode.DEFAULT)
        await guard.check("run_shell_command", command)
        assert len(calls) == 1

    async def test_with_explain_off_the_question_carries_no_follow_up(self, tmp_path):
        factory, guard, calls = self._asked(tmp_path, explain=False)
        factory.permission_evaluator.set_mode(PermissionMode.BYPASS_PERMISSIONS)

        await guard.check("run_shell_command", {"command": "rm -rf ~"})

        assert calls[0]["follow_up"] is None

    async def test_with_explain_on_the_follow_up_asks_the_model_the_ref_names_now(self, tmp_path):
        class Completions:
            def __init__(self, content: str) -> None:
                self.content = content

            async def create(self, **_: Any):
                message = SimpleNamespace(content=self.content)
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        def model(content: str):
            client = SimpleNamespace(chat=SimpleNamespace(completions=Completions(content)))
            return SimpleNamespace(id="m", get_async_client=lambda: client)

        current = [model("Deletes your home directory")]
        factory, guard, calls = self._asked(tmp_path, explain=True, model_ref=lambda: current[0])
        factory.permission_evaluator.set_mode(PermissionMode.BYPASS_PERMISSIONS)

        await guard.check("run_shell_command", {"command": "rm -rf ~"})
        follow_up = calls[0]["follow_up"]
        assert follow_up is not None
        assert (await follow_up()).startswith("Deletes your home directory")

        # The session switched models; the next explanation uses the new one.
        current[0] = model("Removes everything under home")
        assert (await follow_up()).startswith("Removes everything under home")

    async def test_the_explanation_gives_up_after_the_configured_timeout(self, tmp_path):
        class Hangs:
            async def create(self, **_: Any):
                await asyncio.sleep(3600)

        client = SimpleNamespace(chat=SimpleNamespace(completions=Hangs()))
        slow = SimpleNamespace(id="m", get_async_client=lambda: client)
        factory, guard, calls = self._asked(
            tmp_path, explain=True, timeout=0.05, model_ref=lambda: slow
        )
        factory.permission_evaluator.set_mode(PermissionMode.BYPASS_PERMISSIONS)
        await guard.check("run_shell_command", {"command": "rm -rf ~"})

        assert await asyncio.wait_for(calls[0]["follow_up"](), timeout=5) is None


@pytest.mark.parametrize("mode", [PermissionMode.PLAN, PermissionMode.DEFAULT])
def test_a_seeded_evaluator_keeps_its_mode_in_hooks_created_from_it(tmp_path, mode):
    seed = PermissionEvaluator.from_strings(mode=mode.value)
    factory, _, _ = _factory(tmp_path, evaluator=seed)
    factory.create()

    assert factory.permission_evaluator.mode is mode
