"""``host.chat.approvals``: the requests waiting for the person, and answering them."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Call
from igni_core.extensions.host.members.choice import Choice
from igni_core.extensions.host.members.tag import call, get
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.state.approvals import Approval, ApprovalMirror
from igni_core.protocol import messages as msg


class ApprovalAction(Choice):
    CONFIRM = "confirm"
    REJECT = "reject"

    @classmethod
    def refusal(cls, value: str) -> str:
        return f"action is 'confirm' or 'reject', not {value!r}"


class ApprovalAnswer(BaseModel):
    """One answer to one waiting request."""

    requirement_id: str
    action: ApprovalAction
    choice: str

    @classmethod
    def of(cls, raw: dict[str, Any]) -> ApprovalAnswer:
        requirement_id = str(raw["requirement_id"])
        return cls(
            requirement_id=requirement_id,
            action=ApprovalAction.of(str(raw["action"])),
            choice=str(raw.get("choice") or ""),
        )

    def decision(self) -> msg.HITLDecision:
        return msg.HITLDecision(
            requirement_id=self.requirement_id, action=self.action.value, choice=self.choice
        )

    def response(self) -> msg.HITLResponse:
        return msg.HITLResponse(
            requirement_id=self.requirement_id, action=self.action.value, choice=self.choice
        )


class ApprovalMembers(Members):
    @get("chat.approvals.waiting")
    async def waiting(self, c: Call) -> list[Approval]:
        return self._mirror(c.session_id).waiting(c.session_id)

    @call("chat.approvals.answer")
    async def answer(self, c: Call) -> None:
        answer = self._waiting(c, ApprovalAnswer.of(c.params))
        await self.host.backend.act(answer.response(), session_id=c.session_id)

    @call("chat.approvals.answer_all")
    async def answer_all(self, c: Call) -> None:
        decisions = [
            self._waiting(c, ApprovalAnswer.of(raw)).decision() for raw in c.params["answers"]
        ]
        await self.host.backend.act(
            msg.HITLResponseBatch(decisions=decisions), session_id=c.session_id
        )

    def _mirror(self, session_id: str) -> ApprovalMirror:
        return self.host.state.approvals.ensure(
            session_id, lambda: self.host.backend.waiting_approvals(session_id)
        )

    def _waiting(self, c: Call, answer: ApprovalAnswer) -> ApprovalAnswer:
        """``answer``, when its request is waiting in the call's session."""
        if not self._mirror(c.session_id).has(c.session_id, answer.requirement_id):
            raise HostModelError(
                ErrorCode.NOT_FOUND,
                f"no request {answer.requirement_id!r} is waiting in session {c.session_id}",
            )
        return answer


__all__ = ["ApprovalAction", "ApprovalAnswer", "ApprovalMembers"]
