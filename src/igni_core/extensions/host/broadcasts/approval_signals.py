"""Approvals asked for, detailed, answered — told, and kept in the mirror."""

from __future__ import annotations

from typing import ClassVar

from igni_core.core.approvals.events import ApprovalChange
from igni_core.extensions.host.broadcasts.signal import Handler
from igni_core.extensions.host.signal_values import Emitter, Signal
from igni_core.extensions.host.state.approvals import Approval, ApprovalMirror
from igni_core.protocol import messages as msg

APPROVALS_CHANGED = "chat.approvals.changed"


class RunPausedSignal(Signal):
    WIRE: ClassVar[str] = "chat.run_paused"

    session_id: str
    run_id: str
    requirements: list[Approval]


class ApprovalResolved(Signal):
    WIRE: ClassVar[str] = "chat.approvals.resolved"

    session_id: str
    requirement_id: str


class ApprovalDetails(Signal):
    WIRE: ClassVar[str] = "chat.approvals.details"

    session_id: str
    requirement_id: str
    details: str


class ApprovalSignals:
    """Translates the approval traffic, keeping :class:`ApprovalMirror` in step."""

    def __init__(self, emitter: Emitter, mirror: ApprovalMirror) -> None:
        self._emitter = emitter
        self._mirror = mirror

    def handlers(self) -> dict[type[msg.Message], Handler]:
        return {
            msg.RunPaused: self.run_paused,
            msg.HITLRequest: self.hitl_request,
            msg.RequirementResolved: self.requirement_resolved,
            msg.RequirementDetails: self.requirement_details,
        }

    def changed(self, change: ApprovalChange) -> None:
        """An approval appeared or went away (``core/approvals/events.py``)."""
        self._emitter.value(APPROVALS_CHANGED, change.model_dump(mode="json"))

    def run_paused(self, m: msg.RunPaused) -> None:
        approvals = [self._mirror.record(m.session_id, r) for r in m.requirements]
        self._emitter.send(
            RunPausedSignal(session_id=m.session_id, run_id=m.run_id, requirements=approvals)
        )
        for approval in approvals:
            self._emitter.send(approval)

    def hitl_request(self, m: msg.HITLRequest) -> None:
        self._emitter.send(self._mirror.record(m.session_id, m))

    def requirement_resolved(self, m: msg.RequirementResolved) -> None:
        self._mirror.resolve(m.session_id, m.requirement_id)
        self._emitter.send(ApprovalResolved.of(m))

    def requirement_details(self, m: msg.RequirementDetails) -> None:
        self._mirror.set_details(m.session_id, m.requirement_id, m.details)
        self._emitter.send(ApprovalDetails.of(m))


__all__ = ["ApprovalDetails", "ApprovalResolved", "ApprovalSignals", "RunPausedSignal"]
