"""A sub-agent's pause is answered by the session's policy, and never hangs headless.

Agno pauses a sub-agent's call to a ``requires_confirmation`` tool before
any policy has seen it. The main agent's pauses are settled from the
permission evaluator; a sub-agent's went straight to the HITL bridge, so
under ``--read-only`` / plan mode its ``ls`` was asked about, a command
read-only refuses was asked about, an allow rule was ignored, and in a
headless run nobody answered and the process hung until killed (campaign
R092, R106, R128, R188, R741).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from agno.models.response import ToolExecution
from agno.run import agent as ev
from agno.run.requirement import RunRequirement

from igni_core.core.config.permission_eval.pipeline import PermissionEvaluator
from igni_core.core.sub_agent_hitl import SubAgentHITLCoordinator
from igni_core.core.tools.orchestrate_agent_stream import SubAgentStreamHandler
from igni_core.core.tools.orchestrate_events import AgentPausedEvent, RunErrorEvent
from igni_core.core.tools.orchestrate_registry import SubAgentRegistry
from igni_core.core.tools.orchestrate_stream_context import StreamContext, StreamSinks


def _requirement(command: str) -> RunRequirement:
    return RunRequirement(
        tool_execution=ToolExecution(
            tool_name="run_shell_command",
            tool_args={"command": command},
            tool_call_id=f"c-{command}",
            requires_confirmation=True,
        )
    )


class _Runnable:
    def __init__(self, tape: list[Any], follow_up: list[Any]) -> None:
        self.tape, self.follow_up = tape, follow_up
        self.continued: list[dict[str, Any]] = []

    def arun(self, task: str, *, stream: bool = True, stream_events: bool = False) -> Any:
        return self._play(self.tape)

    def acontinue_run(self, run_response: Any = None, **kwargs: Any) -> Any:
        self.continued.append(kwargs)
        return self._play(self.follow_up)

    @staticmethod
    async def _play(tape: list[Any]):
        for event in tape:
            yield event

    async def aget_run_output(self, *, run_id: str, session_id: str) -> Any:
        return None

    async def aget_last_run_output(self, *, session_id: str) -> Any:
        return None


def _run(coordinator: SubAgentHITLCoordinator, *reqs: RunRequirement):
    events: list[Any] = []
    runnable = _Runnable(
        [ev.RunPausedEvent(run_id="r", session_id="s", requirements=list(reqs))],
        [ev.RunCompletedEvent(content="done")],
    )
    handler = SubAgentStreamHandler(
        StreamContext(
            runnable=runnable,
            task="t",
            agent_path=["explorer"],
            card_id="",
            sinks=StreamSinks(
                on_progress=events.append,
                event_appender=None,
                hitl_coordinator=coordinator,
                subagent_registry=SubAgentRegistry(),
            ),
        )
    )
    return handler, runnable, events


def _coordinator(evaluator: PermissionEvaluator | None) -> SubAgentHITLCoordinator:
    coordinator = SubAgentHITLCoordinator()
    coordinator.use_policy(lambda: evaluator)
    return coordinator


class TestThePolicyAnswersFirst:
    @pytest.mark.asyncio
    async def test_plan_mode_runs_a_read_only_command_without_asking(self):
        coordinator = _coordinator(PermissionEvaluator.from_strings(mode="plan"))
        ls = _requirement("ls -la")

        handler, runnable, events = _run(coordinator, ls)
        await handler.run()

        assert ls.confirmation is True
        assert not [e for e in events if isinstance(e, AgentPausedEvent)]
        assert coordinator.waiting_count == 0
        (call,) = runnable.continued
        assert call["requirements"] == [ls]

    @pytest.mark.asyncio
    async def test_a_deny_rule_refuses_with_its_reason_without_asking(self):
        coordinator = _coordinator(PermissionEvaluator.from_strings(deny=["Bash(rm *)"]))
        rm = _requirement("rm -rf build")

        handler, runnable, _ = _run(coordinator, rm)
        await handler.run()

        assert rm.confirmation is False
        assert (rm.confirmation_note or "").startswith("Blocked:")
        assert len(runnable.continued) == 1

    @pytest.mark.asyncio
    async def test_an_allow_rule_is_honoured(self):
        coordinator = _coordinator(PermissionEvaluator.from_strings(allow=["Bash(make *)"]))
        make = _requirement("make test")

        handler, runnable, _ = _run(coordinator, make)
        await handler.run()

        assert make.confirmation is True and len(runnable.continued) == 1

    def test_without_an_evaluator_every_requirement_is_left_for_a_person(self):
        coordinator = _coordinator(None)
        req = _requirement("ls")

        assert coordinator.settle_by_policy([req]) == [req]
        assert req.confirmation is None


class TestNobodyToAsk:
    @pytest.mark.asyncio
    async def test_an_unattended_pause_ends_the_sub_agent_run_and_is_recorded(self):
        coordinator = _coordinator(PermissionEvaluator.from_strings())
        coordinator.mark_unattended()

        handler, runnable, events = _run(coordinator, _requirement("curl example.com"))
        await handler.run()

        assert runnable.continued == []
        assert [e.error for e in events if isinstance(e, RunErrorEvent)] == [
            "paused: no HITL bridge available"
        ]
        assert coordinator.waiting_count == 0
        assert coordinator.take_unanswered() == ["run_shell_command"]
        assert coordinator.take_unanswered() == []

    def test_the_session_hands_the_coordinator_its_evaluator(self):
        import inspect

        from igni_core.core.session.parts.runtime import RuntimeBuilder

        source = inspect.getsource(RuntimeBuilder.build_runtime_services)
        assert "use_policy(" in source and "permission_evaluator" in source

    def test_a_headless_run_marks_the_coordinator_unattended(self):
        import inspect

        from igni_core.core.session.single_message_run import SingleMessageRun

        assert "mark_unattended()" in inspect.getsource(SingleMessageRun.run)


class TestATeamMembersPause:
    """The same for a spawn_team member: its pause went straight to the
    bridge, so under ``--read-only`` its ``cat a.txt`` was asked about, and
    in a headless run nobody answered and the team hung beyond
    ``sub_team_timeout``, whose clock leaves the wait out (campaign R128)."""

    @staticmethod
    def _team(coordinator: SubAgentHITLCoordinator, *reqs: RunRequirement):
        from agno.run import team as team_ev

        from igni_core.core.tools.orchestrate_team_stream import TeamStreamHandler

        events: list[Any] = []
        runnable = _Runnable(
            [team_ev.RunPausedEvent(run_id="t", session_id="s", requirements=list(reqs))],
            [team_ev.RunCompletedEvent(content="done")],
        )
        handler = TeamStreamHandler(
            StreamContext(
                runnable=runnable,
                task="t",
                agent_path=["review-team"],
                card_id="",
                sinks=StreamSinks(
                    on_progress=events.append,
                    event_appender=None,
                    hitl_coordinator=coordinator,
                    subagent_registry=SubAgentRegistry(),
                ),
            )
        )
        return handler, runnable, events

    @pytest.mark.asyncio
    async def test_read_only_runs_a_members_read_without_asking(self):
        coordinator = _coordinator(PermissionEvaluator.from_strings(mode="plan"))
        cat = _requirement("cat a.txt")

        handler, runnable, events = self._team(coordinator, cat)
        await asyncio.wait_for(handler.run(), timeout=5)

        assert cat.confirmation is True
        assert not [e for e in events if isinstance(e, AgentPausedEvent)]
        assert coordinator.waiting_count == 0
        (call,) = runnable.continued
        assert call["requirements"] == [cat]

    @pytest.mark.asyncio
    async def test_an_unattended_members_pause_ends_the_team_run_and_is_recorded(self):
        coordinator = _coordinator(PermissionEvaluator.from_strings())
        coordinator.mark_unattended()

        handler, runnable, _ = self._team(coordinator, _requirement("curl example.com"))
        await asyncio.wait_for(handler.run(), timeout=5)

        assert runnable.continued == []
        assert coordinator.waiting_count == 0
        assert coordinator.take_unanswered() == ["run_shell_command"]
