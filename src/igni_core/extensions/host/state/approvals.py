"""The approvals waiting, by session: the host's mirror of the backend's."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from igni_core.extensions.host.signal_values import Signal
from igni_core.protocol import messages as msg


class Approval(Signal):
    """One request waiting for the person — and ``chat.approvals.requested``'s value."""

    WIRE: ClassVar[str] = "chat.approvals.requested"

    requirement_id: str
    session_id: str
    tool_name: str
    friendly_name: str
    #: The tool's arguments, as Agno gives them: open by nature.
    tool_args: dict[str, Any]
    details: str
    agent_path: list[str]

    @classmethod
    def requested(cls, session_id: str, request: msg.HITLRequest) -> Approval:
        return cls(
            requirement_id=request.requirement_id,
            session_id=session_id,
            tool_name=request.tool_name,
            friendly_name=request.friendly_name,
            tool_args=dict(request.tool_args),
            details=request.details,
            agent_path=list(request.agent_path),
        )


class ApprovalMirror:
    """Approvals waiting, by session, by requirement id.

    A mirror of the backend's, which are in the database
    (``ApprovalJournal``): kept from what is broadcast, and read from the
    backend for a session it has not heard of — after a restart.
    """

    def __init__(self) -> None:
        self._by_session: dict[str, dict[str, Approval]] = {}

    def knows(self, session_id: str) -> bool:
        return session_id in self._by_session

    def ensure(self, session_id: str, load: Callable[[], list[msg.Message]]) -> ApprovalMirror:
        """This mirror, with the session read from the backend (``load``)
        the first time it is asked about — after a restart nothing has
        been broadcast yet."""
        if not self.knows(session_id):
            self.restore(session_id, load())
        return self

    def waiting(self, session_id: str) -> list[Approval]:
        return list(self._by_session.get(session_id, {}).values())

    def has(self, session_id: str, requirement_id: str) -> bool:
        return requirement_id in self._by_session.get(session_id, {})

    def record(self, session_id: str, request: msg.HITLRequest) -> Approval:
        approval = Approval.requested(session_id, request)
        self._by_session.setdefault(session_id, {})[request.requirement_id] = approval
        return approval

    def restore(self, session_id: str, waiting: list[msg.Message]) -> None:
        """Take the session's waiting approvals from the backend, quietly.

        The backend keeps them in the database
        (:class:`~igni_core.core.approvals.journal.ApprovalJournal`);
        after a restart this is how the host learns of them before any
        window attaches and has them shown again. Nothing is emitted:
        they were requested before, and an extension that listened then
        heard it. The session is known afterwards even when nothing is
        waiting, so the backend is not asked again.
        """
        approvals = self._by_session.setdefault(session_id, {})
        for paused in waiting:
            if isinstance(paused, msg.RunPaused):
                for request in paused.requirements:
                    if request.requirement_id not in approvals:
                        self.record(session_id, request)

    def resolve(self, session_id: str, requirement_id: str) -> None:
        self._by_session.get(session_id, {}).pop(requirement_id, None)

    def set_details(self, session_id: str, requirement_id: str, details: str) -> None:
        approvals = self._by_session.get(session_id, {})
        found = approvals.get(requirement_id)
        if found is not None:
            approvals[requirement_id] = found.model_copy(update={"details": details})

    def clear(self, session_id: str) -> None:
        self._by_session.pop(session_id, None)


__all__ = ["Approval", "ApprovalMirror"]
