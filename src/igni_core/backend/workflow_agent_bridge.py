"""Answers a workflow script's ``agent()`` calls with the session's team."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from pydantic_core import PydanticSerializationError

from igni_core.backend.schemas_workflow_events import AgentRequestEvent, AgentResponseMessage
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.session.message_outcome import OutcomeKind, TurnOutcome
from igni_core.core.session.message_pause_policy import PausePolicyResolver

if TYPE_CHECKING:
    from igni_core.core.session.core import Session

logger = logging.getLogger(__name__)


class WorkflowAgentBridge:
    """Runs one ``agent_request`` on ``session.main_team`` and builds the reply.

    Every request gets a reply, ``ok=false`` when it could not be answered:
    the script is blocked on it.
    """

    def __init__(self, session: Session, gate: RunGate) -> None:
        self._session = session
        # The run gate of the backend process: no agent call answers
        # while it is closed.
        self._gate = gate

    #: How many times one agent call may stop for an approval before the
    #: bridge gives up on it, as the headless pause policy does.
    MAX_RESUMES = PausePolicyResolver.MAX_POLICY_RESUMES

    async def answer(self, request: AgentRequestEvent) -> AgentResponseMessage:
        payload = request.payload
        if payload.rejected_because:
            return AgentResponseMessage.refusal(
                payload.id, f"agent request invalid: {payload.rejected_because}"
            )
        refusal = self._gate.reason
        if refusal:
            return AgentResponseMessage.refusal(payload.id, refusal)
        team = self._session.main_team
        if team is None:
            return AgentResponseMessage.refusal(payload.id, "session.main_team not available")
        try:
            response, stopped = await asyncio.wait_for(
                self._run(team, request), timeout=payload.timeout
            )
        except asyncio.TimeoutError:
            return AgentResponseMessage.refusal(
                payload.id, f"agent call timed out after {payload.timeout_seconds}s"
            )
        except Exception as exc:  # any agent failure is an ok=false reply: that is the protocol
            logger.exception("agent bridge failed for %s", request.run_id)
            return AgentResponseMessage.refusal(payload.id, f"agent call failed: {exc}")
        if stopped:
            return AgentResponseMessage.refusal(payload.id, stopped)
        return self._serialisable(
            AgentResponseMessage.success(payload.id, self._content_of(response))
        )

    def session_id_for(self, request: AgentRequestEvent) -> str:
        """The session a workflow run's agent calls are kept in.

        Not the chat's: run under the chat's own id, every call's prompt
        and answer were saved into the chat's history, where the person
        never wrote them and the model read them back on the next turn
        (campaign R664). One per workflow run, so its calls share their
        context with each other and with nothing else.
        """
        return f"{self._session.session_id}:workflow:{request.run_id}"

    async def _run(self, team: Any, request: AgentRequestEvent) -> tuple[Any, str]:
        """The agent's answer, with every pause on the way answered.

        Returns ``(response, "")``, or ``(None, why)`` when a pause could
        not be answered. A call stopped for an approval was handed to the
        script as its answer — the pause text, as if the agent had
        replied with it (campaign R664). The policy settles what it has
        decided, as for a headless turn; the person is asked about the
        rest, as for a sub-agent's call; and a call they decline, or
        nobody can answer, stops the agent call with ``ok=false`` instead
        of resuming the run with words of igni's own.
        """
        session_id = self.session_id_for(request)
        run = await team.arun(request.payload.prompt, stream=False, session_id=session_id)
        for _ in range(self.MAX_RESUMES):
            if not self._paused(run):
                return run, ""
            policy = PausePolicyResolver(
                evaluator_ref=lambda: self._session.permission_evaluator,
                session_id_ref=lambda: session_id,
                audit=self._session.tool_audit,
            )
            run = await policy.settle(team, run)
            if not self._paused(run):
                return run, ""
            for requirement in TurnOutcome.from_run(run).requirements:
                stopped = await self._ask_person(requirement, request)
                if stopped:
                    return None, stopped
                requirement.confirm()
            run = await team.acontinue_run(
                run_response=run,
                requirements=list(run.requirements or []),
                session_id=session_id,
                stream=False,
            )
        return None, f"agent call stopped for approval more than {self.MAX_RESUMES} times"

    @staticmethod
    def _paused(run: Any) -> bool:
        return hasattr(run, "status") and TurnOutcome.from_run(run).kind is OutcomeKind.PAUSED

    async def _ask_person(self, requirement: Any, request: AgentRequestEvent) -> str:
        """Put one paused call to the person; why the agent call stops, or ``""``."""
        execution = requirement.tool_execution
        tool = str(getattr(execution, "tool_name", "") or "a tool")
        args = dict(getattr(execution, "tool_args", None) or {})
        caller = request.payload.label or "workflow"
        answer = await self._session.approval.ask(
            tool, args, f"The workflow's agent call wants to run {tool}.", [caller]
        )
        if answer is None:
            return f"agent call stopped to ask for approval of {tool}, and nobody can answer"
        if not answer:
            return f"agent call stopped: {tool} was declined"
        return ""

    @staticmethod
    def _serialisable(reply: AgentResponseMessage) -> AgentResponseMessage:
        """``reply``, or a refusal when its result cannot go out as JSON."""
        try:
            reply.model_dump_json()
        except PydanticSerializationError as exc:
            return AgentResponseMessage.refusal(
                reply.id, f"agent result is not JSON-serialisable: {exc}"
            )
        return reply

    @staticmethod
    def _content_of(response: Any) -> Any:
        """The agent's content out of a ``TeamRunOutput``.

        The response shape varies by Agno version, so the common fields are
        tried in turn. Returns the raw content (a string for a plain prompt,
        parsed JSON for a schema call) for the script to use directly.
        """
        if response is None:
            return None
        if isinstance(response, (str, dict, list, int, float, bool)):
            return response
        for attr in ("content", "output", "text", "response"):
            value = getattr(response, attr, None)
            if value is not None:
                return value
        messages = getattr(response, "messages", None)
        if messages:
            last = messages[-1]
            if hasattr(last, "content"):
                return last.content
        return str(response)
