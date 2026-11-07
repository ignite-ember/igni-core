"""Answers a workflow script's ``agent()`` calls with the session's team."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from pydantic_core import PydanticSerializationError

from igni_core.backend.schemas_workflow_events import AgentRequestEvent, AgentResponseMessage
from igni_core.core.config.group_policy import GroupPolicyGate

if TYPE_CHECKING:
    from igni_core.core.session.core import Session

logger = logging.getLogger(__name__)


class WorkflowAgentBridge:
    """Runs one ``agent_request`` on ``session.main_team`` and builds the reply.

    Every request gets a reply, ``ok=false`` when it could not be answered:
    the script is blocked on it.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    async def answer(self, request: AgentRequestEvent) -> AgentResponseMessage:
        payload = request.payload
        if payload.rejected_because:
            return AgentResponseMessage.refusal(
                payload.id, f"agent request invalid: {payload.rejected_because}"
            )
        refusal = GroupPolicyGate.reason()
        if refusal:
            return AgentResponseMessage.refusal(payload.id, refusal)
        team = self._session.main_team
        if team is None:
            return AgentResponseMessage.refusal(payload.id, "session.main_team not available")
        try:
            response = await asyncio.wait_for(
                team.arun(payload.prompt, stream=False), timeout=payload.timeout
            )
        except asyncio.TimeoutError:
            return AgentResponseMessage.refusal(
                payload.id, f"agent call timed out after {payload.timeout_seconds}s"
            )
        except Exception as exc:  # any agent failure is an ok=false reply: that is the protocol
            logger.exception("agent bridge failed for %s", request.run_id)
            return AgentResponseMessage.refusal(payload.id, f"agent call failed: {exc}")
        return self._serialisable(
            AgentResponseMessage.success(payload.id, self._content_of(response))
        )

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
