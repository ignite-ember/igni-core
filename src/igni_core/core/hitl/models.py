"""What a pending question is, and the handles the relay passes around.

Four values travel through the sub-agent HITL bridge:

* :class:`PendingEntry` — one question waiting for a person.
* :class:`PendingHandle` — a question plus the id the person answers
  it by.
* :class:`DetailUpdate` — a better explanation for a question already
  on screen.
* :class:`RestoredRun` — a paused sub-agent run read back after a
  restart, all its approvals answered.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from agno.run.requirement import RunRequirement
from pydantic import BaseModel, ConfigDict, Field, SkipValidation

from igni_core.core.approvals.records import SpawnLink
from igni_core.protocol import messages as msg
from igni_core.protocol.tool_friendly_names import ToolFriendlyNames


class ToolExecutionLike(Protocol):
    """The tool call a question is about.

    Agno's ``ToolExecution`` and our own
    :class:`~igni_core.core.session.tool_approval.GuardToolExecution`
    both answer to this shape.
    """

    tool_name: str
    tool_args: dict[str, Any]


class HitlRequirement(Protocol):
    """What the coordinator and the dialog touch on a requirement that
    is not an Agno ``RunRequirement`` (a single-call safety question).

    Implemented by
    :class:`~igni_core.core.session.tool_approval.GuardRequirement`.
    """

    tool_execution: ToolExecutionLike
    details: str
    details_pending: bool

    def confirm(self) -> None: ...

    def reject(self, note: str = "") -> None: ...


class PendingEntry(BaseModel):
    """One in-flight sub-agent HITL requirement.

    ``requirement`` is Agno's ``RunRequirement`` or a
    :class:`HitlRequirement` safety question. Both are objects this
    module does not own, so the entry keeps them as they are (no
    validation) and reads them through the accessors below — the one
    place that knows Agno's requirement carries no explanation.

    ``arbitrary_types_allowed`` because ``event`` is an
    :class:`asyncio.Event` (not a Pydantic type). Mutable — the
    ``surfaced`` flag flips once and the event resolves once, both
    inside the coordinator's own methods.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    requirement: SkipValidation[RunRequirement | HitlRequirement]
    run_id: str
    # Chain of agents that produced this requirement, parent → leaf.
    # ``["architect"]`` for a tool the architect specialist requested;
    # ``["architect", "reviewer"]`` if the architect spawned a reviewer
    # that then asked for permission. Surfaced to the FE so the dialog
    # can show *which* specialist is asking, not just the tool name.
    agent_path: list[str] = Field(default_factory=list)
    surfaced: bool = False  # True once forwarded to FE via list_new_pending
    event: asyncio.Event = Field(default_factory=asyncio.Event)

    @property
    def tool_name(self) -> str:
        """The tool the question is about."""
        execution = getattr(self.requirement, "tool_execution", None)
        if execution is None:
            return ""
        return str(getattr(execution, "tool_name", "") or "")

    @property
    def tool_args(self) -> dict[str, Any]:
        """The arguments of the call the question is about."""
        execution = getattr(self.requirement, "tool_execution", None)
        if execution is None:
            return {}
        return dict(getattr(execution, "tool_args", {}) or {})

    @property
    def details(self) -> str:
        """Why the call is being put to the person, if it says."""
        return str(getattr(self.requirement, "details", "") or "")

    @property
    def details_pending(self) -> bool:
        """Whether that explanation is still being written."""
        return bool(getattr(self.requirement, "details_pending", False))

    def explain(self, details: str) -> None:
        """Say why the call is being put to the person.

        Agno's requirement carries no explanation — only a safety
        question does — so the text is set on whichever requirement
        this entry holds, and the "still being written" flag is
        cleared.
        """
        self.requirement.details = details
        if hasattr(self.requirement, "details_pending"):
            self.requirement.details_pending = False

    def request(self, req_id: str) -> msg.HITLRequest:
        """The request a window is shown for this entry, as ``req_id``."""
        raw_name = self.tool_name
        return msg.HITLRequest(
            requirement_id=req_id,
            tool_name=raw_name,
            friendly_name=ToolFriendlyNames.friendly_name(raw_name),
            tool_args=self.tool_args,
            agent_path=list(self.agent_path),
            details=self.details,
            details_pending=self.details_pending,
        )


class PendingHandle(BaseModel):
    """A pending question and the id the person answers it by."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    requirement_id: str
    entry: PendingEntry


class DetailUpdate(BaseModel):
    """A better explanation for a question already on screen."""

    requirement_id: str
    details: str


class RestoredRun(BaseModel):
    """A sub-agent run read back after a restart, all its approvals answered."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str
    requirements: list[SkipValidation[RunRequirement]]
    link: SpawnLink | None = None


__all__ = [
    "DetailUpdate",
    "HitlRequirement",
    "PendingEntry",
    "PendingHandle",
    "RestoredRun",
    "ToolExecutionLike",
]
