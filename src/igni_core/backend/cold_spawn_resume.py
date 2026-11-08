"""Continue what a previous backend left waiting inside a ``spawn_agent`` call.

A sub-agent's approval outlives the backend that asked it
(:class:`~igni_core.core.approvals.journal.ApprovalJournal`),
and so does what hangs from it (``waiting_spawns``): the sub-agent's
paused run, and the chain of calls waiting above it — normally the main
agent's ``spawn_agent`` call. When the person answers after a restart,
nothing in this process is waiting: :class:`ColdSpawnResume` does what
the dead backend would have done.

1. Continue the sub-agent's run from Agno's stored copy with the
   decisions, through the same stream handler a live spawn uses — so a
   further pause asks the person again, and progress reaches the
   window.
2. Hand its answer to the waiting call as that tool's result: the
   caller's run is rebuilt as Agno pauses a run on a tool it executes
   outside (its messages up to the call, the call as an external
   execution, the answer as its result) and continued by run id.
3. Repeat up the chain; the last caller is the main agent, whose
   continued run streams to the session's windows like any resumed run.

The same happens on the desktop after igni is quit and started again.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from agno.agent import Agent
from agno.models.message import Message
from agno.models.response import ToolExecution
from agno.run.agent import RunOutput, RunStatus
from agno.run.requirement import RunRequirement
from pydantic import BaseModel, ConfigDict

from igni_core.backend.schemas_hitl import StreamFactory
from igni_core.core.approvals.journal import SpawnLink
from igni_core.core.hooks.untrusted import UntrustedOutputHook
from igni_core.core.sub_agent_hitl import RestoredRun
from igni_core.core.tools.orchestrate import OrchestrateTools
from igni_core.core.tools.orchestrate_agent_stream import ResumedSubAgentStreamHandler
from igni_core.core.tools.orchestrate_events import EventAppender, OnProgress, SubAgentRegistry
from igni_core.core.tools.orchestrate_spawn import TeamSpawn
from igni_core.core.tools.orchestrate_team_stream import ResumedTeamStreamHandler
from igni_core.core.tools.spawn_context import ParentCall, SpawnCalls
from igni_core.protocol import messages as msg

if TYPE_CHECKING:
    from igni_core.core.session import Session

logger = logging.getLogger(__name__)


class _HandlerWiring(BaseModel):
    """What a continued agent's stream handler reports through: the
    session's spawn tools' sinks, or none when it has no spawn tools."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    on_progress: OnProgress | None
    subagent_registry: SubAgentRegistry
    event_appender: EventAppender | None

    @classmethod
    def of(cls, tools: OrchestrateTools | None) -> _HandlerWiring:
        if tools is None:
            return cls(on_progress=None, subagent_registry=SubAgentRegistry(), event_appender=None)
        return cls(
            on_progress=tools.on_progress,
            subagent_registry=tools.subagent_registry,
            event_appender=tools.event_appender,
        )


class ColdSpawnResume:
    """Continues a restored sub-agent run and every call waiting above it."""

    def __init__(self, session: Session, stream_factory: StreamFactory) -> None:
        self._session = session
        self._stream_factory = stream_factory

    #: What a waiting call gets when the agent it waited on was being
    #: continued — its approval answered — when the backend died.
    INTERRUPTED_SPAWN = (
        "Interrupted: igni restarted while this agent was continuing after its "
        "approval was answered, and its work did not finish. Members or steps "
        "that were still running were not run again; what they did before the "
        "restart may or may not have taken effect."
    )

    async def resume(self, ready: RestoredRun) -> AsyncIterator[msg.Message]:
        """Continue a restored run whose approvals are all answered."""
        link = ready.link
        if link is None or not link.parents:
            yield msg.Info(
                text="The approval was recorded; the run that asked for it ended "
                "with the previous backend and cannot be continued."
            )
            return
        if link.parents[0].is_team_spawn:
            result = await self._continue_team(ready, link)
        else:
            agent = self._pool_agent(link.agent_name)
            if agent is None:
                yield msg.Error(text=f"The agent {link.agent_name!r} is no longer available.")
                return
            result = await self._continue_agent(
                agent,
                link.agent_name,
                chain=link.parents,
                run_id=ready.run_id,
                session_id=link.agent_session_id,
                requirements=ready.requirements,
            )
        self._session.sub_agent_hitl.forget_spawn(ready.run_id)
        async for proto in self._answer_parents(link, result):
            yield proto

    async def interrupted(self, link: SpawnLink) -> AsyncIterator[msg.Message]:
        """The agent ``link`` names was being continued when the previous
        backend died: report it failed to the call waiting on it, and
        continue that call's run.

        Not run again — the approved tool or member may already have done
        what it does, and running it twice is worse than saying so.
        """
        self._session.sub_agent_hitl.forget_spawn(link.run_id)
        if not link.parents:
            return
        async for proto in self._answer_parents(link, self.INTERRUPTED_SPAWN):
            yield proto

    async def _answer_parents(self, link: SpawnLink, result: str) -> AsyncIterator[msg.Message]:
        """Hand ``result`` to the call waiting on ``link``'s run, continue
        that run, and so on up to the main agent's."""
        answer = self.tool_result(link.parents[0], result)
        coordinator = self._session.sub_agent_hitl
        for depth, parent in enumerate(link.parents):
            run_output = self.paused_run(parent, answer, coordinator.turn_results(parent.run_id))
            coordinator.forget_turn(parent.run_id)
            if self._is_main(parent):
                main = self._session.main_team
                async for proto in self._stream_factory(
                    main.acontinue_run(
                        run_response=run_output,
                        requirements=list(run_output.requirements or []),
                        session_id=parent.session_id,
                        stream=True,
                        stream_events=True,
                    )
                ):
                    yield proto
                return
            caller = self._pool_agent(parent.agent_name)
            if caller is None:
                yield msg.Error(text=f"The agent {parent.agent_name!r} is no longer available.")
                return
            # Continued like a live sub-agent: its progress reaches the
            # window, and it can ask the person again — that pause is
            # stored with the calls still waiting above it.
            rest = link.parents[depth + 1 :]
            result = await self._continue_agent(
                caller,
                parent.agent_name,
                chain=rest,
                run_id=parent.run_id,
                session_id=parent.session_id,
                requirements=list(run_output.requirements or []),
                run_response=run_output,
            )
            # Its answer is the result of the call waiting on *it*.
            answer = self.tool_result(rest[0] if rest else parent, result)

    async def _continue_team(self, ready: RestoredRun, link: SpawnLink) -> str:
        """Rebuild the team a ``spawn_team`` call ran and continue its run.

        Agno kept the team's run: members that had finished keep their
        results in it (Agno pauses a team only once every member it
        delegated to in that turn has returned or paused), and only the
        paused members are continued with the decisions before the team
        leader goes on.
        """
        args = link.parents[0].spawn_args
        tools = self._orchestrate_tools()
        if tools is None:
            return "The team could not be rebuilt: no spawn tools in this session."
        built = TeamSpawn.build_team(
            pool=self._session.pool,
            settings=tools.settings,
            names=args.team_names,
            mode=args.mode,
            depth=tools.current_depth + 1,
            deps=tools.spawn_deps(),
        )
        if built.team is None:
            return f"The team could not be rebuilt: {built.error}"
        wiring = _HandlerWiring.of(tools)
        with SpawnCalls.restored(link.parents):
            handler = ResumedTeamStreamHandler(
                built.team,
                run_id=ready.run_id,
                session_id=link.agent_session_id,
                requirements=ready.requirements,
                on_progress=wiring.on_progress,
                hitl_coordinator=self._session.sub_agent_hitl,
                agent_path=SpawnCalls.path_to(f"team({','.join(args.team_names)})", link.parents),
                card_id="",
                subagent_registry=wiring.subagent_registry,
                event_appender=wiring.event_appender,
            )
            result, _activity = await handler.run()
        return str(result)

    async def _continue_agent(
        self,
        agent: Agent,
        name: str,
        *,
        chain: list[ParentCall],
        run_id: str,
        session_id: str,
        requirements: list[RunRequirement],
        run_response: RunOutput | None = None,
    ) -> str:
        """Run one continued agent through its stream handler, under the
        calls still waiting above it: by ``run_id`` from Agno's stored
        run, or from ``run_response`` when given."""
        wiring = _HandlerWiring.of(self._orchestrate_tools())
        with SpawnCalls.restored(chain):
            handler = ResumedSubAgentStreamHandler(
                agent,
                run_id=run_id,
                session_id=session_id,
                requirements=requirements,
                run_response=run_response,
                on_progress=wiring.on_progress,
                hitl_coordinator=self._session.sub_agent_hitl,
                agent_path=SpawnCalls.path_to(name, chain),
                card_id="",
                subagent_registry=wiring.subagent_registry,
                event_appender=wiring.event_appender,
            )
            result, _activity = await handler.run()
        return str(result)

    @staticmethod
    def tool_result(call: ParentCall, result: str) -> str:
        """What the waiting call returns: the spawned agent's or team's
        answer, headed the way a live spawn heads it."""
        args = call.spawn_args
        if call.is_team_spawn:
            head = f"[Team: {', '.join(args.team_names)}] (mode: {args.mode or 'coordinate'})"
        else:
            head = f"[Agent: {args.agent_label}]"
        return f"{head}\n[Task: {args.task}]\n\nResponse:\n{result}"

    #: The result a call gets when the restart cut it off.
    INTERRUPTED = (
        "Not run to completion: igni restarted while this call was running, and its "
        "result was lost. It may or may not have taken effect — check before relying "
        "on it, or run it again."
    )

    @classmethod
    def paused_run(
        cls, parent: ParentCall, answer: str, finished: dict[str, str] | None = None
    ) -> RunOutput:
        """``parent``'s run as Agno pauses one on tools executed outside it.

        The waiting call's result is ``answer``. Every other call of the
        same assistant turn gets its stored result when it finished, and
        :attr:`INTERRUPTED` when it was still running at the restart — it
        is not run again: a call cut off may already have done what it
        does (a deploy, a write), and doing it twice is worse than the
        model knowing it was interrupted. So the continued run sees a
        complete turn.

        Results go to the model through the rule a live call's do
        (:meth:`UntrustedOutputHook.quarantine`): an external execution
        result is handed over as it stands, with no tool hook in the way,
        so a stored shell output or the spawned agent's answer would
        otherwise reach the model unwrapped — data read as instructions,
        and outside the format it was trained on. :attr:`INTERRUPTED` is
        igni's own word about the call, not its output, and stays bare.
        """
        done = finished or {}
        executions: list[ToolExecution] = []
        requirements: list[RunRequirement] = []
        for call in parent.turn():
            if call.id == parent.tool_call_id:
                name, args = parent.tool_name, dict(parent.tool_args)
                result = UntrustedOutputHook.quarantine(name, answer)
            else:
                name, args = call.name, call.arguments
                stored = done.get(call.id)
                result = (
                    cls.INTERRUPTED
                    if stored is None
                    else UntrustedOutputHook.quarantine(name, stored)
                )
            execution = ToolExecution(
                tool_call_id=call.id,
                tool_name=name,
                tool_args=args,
                external_execution_required=True,
            )
            requirement = RunRequirement(tool_execution=execution)
            requirement.set_external_execution_result(result)
            executions.append(execution)
            requirements.append(requirement)
        return RunOutput(
            run_id=parent.run_id,
            session_id=parent.session_id,
            agent_id=parent.agent_id or parent.agent_name,
            agent_name=parent.agent_name,
            messages=[Message.from_dict(m) for m in parent.messages],
            tools=executions,
            requirements=requirements,
            status=RunStatus.paused,
        )

    def _is_main(self, parent: ParentCall) -> bool:
        return parent.agent_name == (self._session.main_team.name or "")

    def _pool_agent(self, name: str) -> Agent | None:
        agent = self._session.pool.get(name)
        if agent is None:
            logger.warning("cold resume: no agent %r in the pool", name)
            return None
        return copy.copy(agent)

    def _orchestrate_tools(self) -> OrchestrateTools | None:
        for tool in self._session.main_team.tools or []:
            if isinstance(tool, OrchestrateTools):
                return tool
        return None


__all__ = ["ColdSpawnResume"]
