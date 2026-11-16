"""``PermissionRequest`` / ``PermissionDenied`` fire for a call settled at the pause.

The tool-event hook's stages fire both events, but only for a call about
to run. Most approvals are settled earlier, at Agno's confirmation pause —
the backend's pause handler, the person's dialog, the headless pause
policy — and none of those fired either event (campaign R120): not when a
person was asked, not when one declined, not when ``--read-only`` refused.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from agno.models.response import ToolExecution
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.requirement import RunRequirement

from igni_core.backend.pause_handler import PauseHandler
from igni_core.backend.pending_requirements_store import PendingRequirementsStore
from igni_core.core.hooks.permission_notices import PermissionNotices
from igni_core.core.session.message_pause_policy import PausePolicyResolver
from tests.test_the_pause_handler_decides_what_the_person_sees import (
    FakeEvaluator,
    FakeRequirement,
    _deny,
    _event,
)


class _Executor:
    def __init__(self) -> None:
        self.fired: list[tuple[str, str, Any]] = []

    async def execute(self, *, event: str, payload: Any, target: str = "") -> Any:
        self.fired.append((event, target, payload))
        return SimpleNamespace(should_continue=True)


def _notices(executor: _Executor) -> PermissionNotices:
    return PermissionNotices(lambda: executor, lambda: "s-1")  # type: ignore[arg-type,return-value]


async def _settled() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_the_backend_fires_request_for_what_it_asks_and_denied_for_what_it_refuses():
    executor = _Executor()
    evaluator = FakeEvaluator({"run_shell_command": _deny("read-only session")})
    handler = PauseHandler(evaluator, PendingRequirementsStore(), notices=_notices(executor))

    handler.handle(
        _event(
            [
                FakeRequirement("run_shell_command", {"command": "mkdir d"}),
                FakeRequirement("save_file", {"file_name": "a.txt"}),
            ]
        )
    )
    await _settled()

    fired = [(event, target) for event, target, _ in executor.fired]
    assert ("PermissionDenied", "run_shell_command") in fired
    assert ("PermissionRequest", "save_file") in fired
    (denied,) = [p for e, _, p in executor.fired if e == "PermissionDenied"]
    assert denied.reason == PermissionNotices.BY_EVALUATOR
    assert denied.session_id == "s-1"


async def test_the_headless_policy_fires_denied_for_a_call_it_refuses_at_the_pause():
    executor = _Executor()
    requirement = RunRequirement(
        tool_execution=ToolExecution(
            tool_name="run_shell_command",
            tool_args={"command": "mkdir rodir"},
            requires_confirmation=True,
        )
    )
    evaluator = FakeEvaluator({"run_shell_command": _deny("read-only session")})
    paused = RunOutput(status=RunStatus.paused, requirements=[requirement])
    done = RunOutput(status=RunStatus.completed, content="ok")

    class _Team:
        async def acontinue_run(self, **kwargs: Any) -> Any:
            return done

    resolver = PausePolicyResolver(
        evaluator_ref=lambda: evaluator,  # type: ignore[arg-type,return-value]
        session_id_ref=lambda: "s-1",
        notices=_notices(executor),
    )

    await resolver.settle(_Team(), paused)  # type: ignore[arg-type]

    assert [(e, t) for e, t, _ in executor.fired] == [("PermissionDenied", "run_shell_command")]
    assert executor.fired[0][2].reason == PermissionNotices.BY_EVALUATOR


def test_without_a_running_loop_nothing_is_fired_and_nothing_breaks():
    executor = _Executor()
    _notices(executor).denied_later("save_file", {}, PermissionNotices.BY_PERSON)
    assert executor.fired == []
