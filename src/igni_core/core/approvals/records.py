"""What the approval journal hands back: an approval's origin, a stored
approval and a paused sub-agent run's link to the calls above it."""

from __future__ import annotations

from enum import StrEnum

from agno.run.requirement import RunRequirement
from pydantic import BaseModel, ConfigDict, Field

from igni_core.core.tools.spawn_context import ParentCall
from igni_core.protocol import messages as msg


class ApprovalOrigin(StrEnum):
    #: The main agent's run paused on it.
    MAIN = "main"
    #: A sub-agent's run, inside a ``spawn_agent`` call, paused on it.
    SUB_AGENT = "sub_agent"
    #: Decided by the permission policy; kept for the resume of its run.
    AUTO_RESOLVED = "auto_resolved"
    #: Not an approval: the main agent's run, its approvals answered, is
    #: being continued. Left behind only by a backend that died meanwhile.
    CONTINUING = "continuing"

    @property
    def asks_a_person(self) -> bool:
        """Whether a window is shown this approval (and told it ended)."""
        return self in (ApprovalOrigin.MAIN, ApprovalOrigin.SUB_AGENT)


class SpawnLink(BaseModel):
    """What continuing a paused sub-agent run cold needs."""

    run_id: str
    session_id: str
    agent_session_id: str
    agent_name: str
    parents: list[ParentCall] = Field(default_factory=list)


class StoredApproval(BaseModel):
    """One approval read back from the journal."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    requirement_id: str
    session_id: str
    run_id: str
    origin: ApprovalOrigin
    requirement: RunRequirement
    request: msg.HITLRequest | None = None
    agent_path: list[str] = Field(default_factory=list)


__all__ = ["ApprovalOrigin", "SpawnLink", "StoredApproval"]
