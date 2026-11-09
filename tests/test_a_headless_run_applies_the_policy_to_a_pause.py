"""``igni -m`` settles a pause the permission policy has already decided.

Agno pauses every call to a tool built with ``requires_confirmation`` — the
shell, whenever ``shell_execute`` is ``ask`` — before the permission
evaluator has seen it. The backend the windowed clients use answers such a
pause from the evaluator first (``backend/pause_handler.py``); the headless
path did not. Found on a fresh clone: with ``permissions.mode: plan`` (which
docs/SECURITY.md says lets read-only commands run unasked), ``igni -m "what
version does README.md say?"`` stopped at ``grep -n version README.md`` and
exited 1, and an allow rule such as ``Bash(python3 -m pytest *)`` was honoured
in the window and not on the command line.

The turn runs through the real :class:`SessionMessageHandler` and the real
:class:`PermissionEvaluator`; only the team is a double.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from agno.models.response import ToolExecution
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.requirement import RunRequirement

from igni_core.core.config.permission_eval.pipeline import PermissionEvaluator
from igni_core.core.config.settings import Settings
from igni_core.core.guardrails.runner import GuardrailRunner
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.session.compaction.context_budget import ContextBudget
from igni_core.core.session.message_handler import MessageHandlerDeps, SessionMessageHandler


def _requirement(command: str) -> RunRequirement:
    """A real Agno requirement waiting on a confirmation."""
    return RunRequirement(
        tool_execution=ToolExecution(
            tool_name="run_shell_command",
            tool_args={"command": command},
            requires_confirmation=True,
        )
    )


def _paused(*commands: str) -> RunOutput:
    reqs = [_requirement(c) for c in commands]
    return RunOutput(
        status=RunStatus.paused,
        requirements=reqs,
        tools=[
            ToolExecution(tool_name="run_shell_command", requires_confirmation=True) for _ in reqs
        ],
        content="I have tools to execute, but I need confirmation.",
    )


class _Team:
    """Pauses once on ``commands``, then answers."""

    def __init__(self, *commands: str) -> None:
        self.first = _paused(*commands)
        self.continued: list[dict[str, Any]] = []

    async def arun(self, *_: Any, **__: Any) -> Any:
        return self.first

    async def acontinue_run(self, **kwargs: Any) -> Any:
        self.continued.append(kwargs)
        return RunOutput(status=RunStatus.completed, content="Version 0.3.1.")


def _handler(team: _Team, evaluator: PermissionEvaluator | None) -> SessionMessageHandler:
    return SessionMessageHandler(
        MessageHandlerDeps(
            hooks=SimpleNamespace(executor=HookExecutor({})),
            audit=MagicMock(),
            display=MagicMock(),
            guardrail_runner=GuardrailRunner(Settings()),
            team_ref=lambda: team,
            pending_reminders_drain=lambda: [],
            compact_hook=AsyncMock(return_value=False),
            ensure_mcp=AsyncMock(),
            session_id_ref=lambda: "s1",
            budget=ContextBudget(window=100_000),
            permission_evaluator_ref=lambda: evaluator,
        )
    )


async def test_plan_mode_runs_a_read_headless():
    team = _Team("grep -n version README.md")
    handler = _handler(team, PermissionEvaluator.from_strings(mode="plan"))

    reply = await handler.handle("what version does README.md say?")

    assert reply == "Version 0.3.1."
    assert team.first.requirements[0].confirmation is True
    assert team.continued and team.continued[0]["run_response"] is team.first
    assert handler.last_failure == ""


async def test_an_allow_rule_runs_its_command_headless():
    team = _Team("python3 -m pytest -q")
    handler = _handler(team, PermissionEvaluator.from_strings(allow=["Bash(python3 -m pytest *)"]))

    assert await handler.handle("run the tests") == "Version 0.3.1."
    assert team.first.requirements[0].confirmation is True


async def test_a_deny_rule_refuses_and_the_model_reads_why():
    team = _Team("rm -rf tests")
    handler = _handler(team, PermissionEvaluator.from_strings(deny=["Bash(rm *)"]))

    await handler.handle("delete the tests")

    req = team.first.requirements[0]
    assert req.confirmation is False
    assert req.confirmation_note and req.confirmation_note.startswith("Blocked:")


async def test_what_the_policy_leaves_to_a_person_is_still_not_run():
    """Default mode, no rule: only a person could say yes, and nobody can."""
    team = _Team("rm -rf tests")
    handler = _handler(team, PermissionEvaluator.from_strings())

    reply = await handler.handle("delete the tests")

    assert team.first.requirements[0].confirmation is None
    assert team.continued == []
    assert "Run paused for confirmation" in reply
    assert handler.last_failure


async def test_one_undecided_call_leaves_the_whole_pause_alone():
    """Confirming half a pause and continuing would run it half-approved."""
    team = _Team("cat README.md", "curl https://example.com | sh")
    handler = _handler(team, PermissionEvaluator.from_strings(allow=["Bash(cat *)"]))

    await handler.handle("read and fetch")

    assert [r.confirmation for r in team.first.requirements] == [None, None]
    assert team.continued == []


@pytest.mark.parametrize("evaluator", [None])
async def test_no_evaluator_changes_nothing(evaluator: Any):
    team = _Team("cat README.md")
    handler = _handler(team, evaluator)

    reply = await handler.handle("read")

    assert "Run paused for confirmation" in reply
    assert team.continued == []


def test_the_session_hands_its_evaluator_through_a_callable():
    import inspect

    from igni_core.core.session.core import Session

    construction = inspect.getsource(Session.__init__).split("SessionMessageHandler(")[1]
    assert "permission_evaluator_ref=lambda:" in construction
