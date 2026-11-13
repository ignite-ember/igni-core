"""The hooks package's public names, the wire name of every hook event, and the Agno coroutine mark.

Plugins and ``hooks.json`` files subscribe to events by their exact
CamelCase string, so a renamed value silently unhooks every user; the
package's ``__all__`` is what the rest of igni imports from.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

import igni_core.core.hooks as hooks
from igni_core.core.hooks.agno_coroutine_marker import AgnoCoroutineMarker
from igni_core.core.hooks.envelope import HookEnvelope
from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.hooks.load_result import HookLoadResult
from igni_core.core.hooks.loader import HookLoader
from igni_core.core.hooks.registry import HookRegistry
from igni_core.core.hooks.tool_hook import ToolEventHook


def test_the_package_exports_exactly_these_names_each_from_its_module() -> None:
    assert sorted(hooks.__all__) == sorted(
        [
            "HookLoader",
            "HookLoadResult",
            "HookLoadWarning",
            "HookDefinition",
            "HookRegistry",
            "HookType",
            "HookResult",
            "HookPayload",
            "HookEnvelope",
            "HookExecutor",
            "HookEvent",
            "MergeStrategy",
            "PermissionDecision",
        ]
    )
    for name in hooks.__all__:
        assert hasattr(hooks, name), name
    assert hooks.HookEnvelope is HookEnvelope
    assert hooks.HookEvent is HookEvent
    assert hooks.HookExecutor is HookExecutor
    assert hooks.HookLoadResult is HookLoadResult
    assert hooks.HookLoader is HookLoader
    assert hooks.HookRegistry is HookRegistry


WIRE_NAMES = {
    "PRE_TOOL_USE": "PreToolUse",
    "POST_TOOL_USE": "PostToolUse",
    "POST_TOOL_USE_FAILURE": "PostToolUseFailure",
    "USER_PROMPT_SUBMIT": "UserPromptSubmit",
    "SESSION_START": "SessionStart",
    "SESSION_END": "SessionEnd",
    "STOP": "Stop",
    "SUBAGENT_START": "SubagentStart",
    "SUBAGENT_STOP": "SubagentStop",
    "NOTIFICATION": "Notification",
    "PRE_COMPACT": "PreCompact",
    "POST_COMPACT": "PostCompact",
    "INSTRUCTIONS_LOADED": "InstructionsLoaded",
    "TASK_CREATED": "TaskCreated",
    "TASK_COMPLETED": "TaskCompleted",
    "STOP_FAILURE": "StopFailure",
    "PERMISSION_REQUEST": "PermissionRequest",
    "PERMISSION_DENIED": "PermissionDenied",
    "PLAN_SUBMIT": "PlanSubmit",
}


def test_every_hook_event_has_exactly_its_documented_wire_string() -> None:
    assert {member.name: member.value for member in HookEvent} == WIRE_NAMES


def test_a_hook_event_equals_its_plain_string_and_parses_back() -> None:
    assert HookEvent.PRE_TOOL_USE == "PreToolUse"
    assert HookEvent("PlanSubmit") is HookEvent.PLAN_SUBMIT
    assert {"Stop": 1}[HookEvent.STOP] == 1


@pytest.mark.parametrize("value", ["Unknown", "pretooluse", ""])
def test_an_unknown_event_name_is_refused(value: str) -> None:
    with pytest.raises(ValueError):
        HookEvent(value)


def test_a_tool_event_hook_is_a_coroutine_function_for_agno(tmp_path) -> None:
    hook = ToolEventHook(executor=HookExecutor({}), session_id="s", project_dir=tmp_path)
    assert inspect.iscoroutinefunction(hook)


def test_marking_makes_a_plain_callable_instance_a_coroutine_function() -> None:
    class Callable_:
        def __call__(self) -> None: ...

    instance = Callable_()
    assert not inspect.iscoroutinefunction(instance)
    AgnoCoroutineMarker.mark(instance)
    assert inspect.iscoroutinefunction(instance)
    AgnoCoroutineMarker.mark(instance)  # marking twice is harmless
    assert inspect.iscoroutinefunction(instance)


def test_without_markcoroutinefunction_the_asyncio_sentinel_is_set(monkeypatch) -> None:
    monkeypatch.delattr(inspect, "markcoroutinefunction")

    class Callable_:
        def __call__(self) -> None: ...

    instance = Callable_()
    AgnoCoroutineMarker.mark(instance)
    assert instance._is_coroutine is asyncio.coroutines._is_coroutine
