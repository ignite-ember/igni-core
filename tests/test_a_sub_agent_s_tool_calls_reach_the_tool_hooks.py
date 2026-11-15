"""A sub-agent's and a team member's tool calls fire ``PreToolUse`` / ``PostToolUse``.

HOOKS.md: ``PreToolUse`` fires before any tool call. Agents ``spawn_agent``
and ``spawn_team`` start carried only the audit hook, so their calls fired
neither event, and a PreToolUse security hook never saw them nor blocked
one (campaign R128).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.core.hooks.schemas import HookResult, PermissionDecision
from igni_core.core.hooks.subagent_tool_hook import SubAgentToolHook


class _Executor:
    def __init__(self, pre: HookResult | None = None) -> None:
        self.fired: list[tuple[str, str, Any]] = []
        self._pre = pre or HookResult()

    async def execute(self, *, event: str, payload: Any, target: str = "") -> HookResult:
        self.fired.append((event, target, payload))
        return self._pre if event == "PreToolUse" else HookResult()


async def test_a_call_fires_pre_then_post_and_runs():
    executor = _Executor()
    hook = SubAgentToolHook(executor, "s-1")  # type: ignore[arg-type]
    ran: list[dict] = []

    def ls(command: str) -> str:
        ran.append({"command": command})
        return "a.txt"

    result = await hook(name="run_shell_command", func=ls, args={"command": "ls"})

    assert result == "a.txt" and ran == [{"command": "ls"}]
    assert [(e, t) for e, t, _ in executor.fired] == [
        ("PreToolUse", "run_shell_command"),
        ("PostToolUse", "run_shell_command"),
    ]
    assert executor.fired[0][2].session_id == "s-1"


@pytest.mark.parametrize(
    "pre",
    [
        HookResult(should_continue=False, message="BLOCKME is not allowed"),
        HookResult(permission_decision=PermissionDecision.DENY, message="BLOCKME is not allowed"),
    ],
)
async def test_a_hook_that_blocks_keeps_the_call_from_running(pre: HookResult):
    executor = _Executor(pre)
    hook = SubAgentToolHook(executor, "s-1")  # type: ignore[arg-type]

    def never(**_: Any) -> str:
        raise AssertionError("a blocked call ran")

    result = await hook(name="run_shell_command", func=never, args={"command": "echo BLOCKME"})

    assert result == "BLOCKME is not allowed"
    assert [e for e, _, _ in executor.fired] == ["PreToolUse"]


async def test_a_call_that_raises_fires_post_tool_use_failure():
    executor = _Executor()
    hook = SubAgentToolHook(executor, "s-1")  # type: ignore[arg-type]

    async def broken(**_: Any) -> str:
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        await hook(name="save_file", func=broken, args={})

    assert [e for e, _, _ in executor.fired] == ["PreToolUse", "PostToolUseFailure"]


def test_it_goes_first_once_and_not_at_all_without_hooks():
    executor = _Executor()
    audit = SimpleNamespace()
    hooks = SubAgentToolHook.on([audit], executor, "s-1")  # type: ignore[arg-type]
    assert isinstance(hooks[0], SubAgentToolHook) and hooks[1] is audit
    assert SubAgentToolHook.on(hooks, executor, "s-1") == hooks  # type: ignore[arg-type]
    assert SubAgentToolHook.on([audit], None, "s-1") == [audit]


async def test_a_team_member_carries_it():
    """The team build wires it on: each member copy ``spawn_team`` runs has it."""
    from igni_core.core.tools.orchestrate_spawn_team import TeamSpawn
    from igni_core.core.tools.orchestrate_spawn_types import TeamBuildRequest

    member = SimpleNamespace(tool_hooks=None)
    pool = SimpleNamespace(get=lambda name: member, missing=lambda name: "missing", db=None)
    settings = SimpleNamespace(orchestration=SimpleNamespace(max_task_iterations=2))
    built: list[Any] = []

    deps = SimpleNamespace(
        team_factory=lambda spec: built.append(spec) or SimpleNamespace(),
        model_registry_factory=lambda settings: SimpleNamespace(get_model=lambda name=None: None),
    )
    request = TeamBuildRequest(
        pool=pool,  # type: ignore[arg-type]
        settings=settings,  # type: ignore[arg-type]
        names=["explorer"],
        mode="coordinate",
        hook_executor=_Executor(),  # type: ignore[arg-type]
        session_id="s-1",
    )
    TeamSpawn.build_team(request, deps)  # type: ignore[arg-type]
    (spec,) = built
    (first,) = spec.members
    assert isinstance(first.tool_hooks[0], SubAgentToolHook)
