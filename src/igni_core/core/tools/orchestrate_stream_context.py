"""What one stream runs, and who it reports to.

A stream is one Agno run — a specialist agent, or a coordinated team —
being driven to completion. It needs the runnable itself, the task to
start it with, and the four sinks it talks to: the progress
callback, the event-log appender, the cancellation registry and the
HITL bridge.

The sinks arrive as one bundle (:class:`StreamSinks`) and the run
itself as one bundle (:class:`StreamContext`), so a spawn hands this
to the streamer and the streamer hands the same bundle to the handler:
one shape to keep in step, rather than two.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SkipValidation

from igni_core.core.tools.orchestrate_contracts import (
    AgnoRunnable,
    EventAppender,
    HitlCoordinatorProtocol,
    OnProgress,
)
from igni_core.core.tools.orchestrate_registry import SubAgentRegistry


class StreamSinks(BaseModel):
    """The four sinks one run reports through.

    They are all late-bound: the session wires them onto the toolkit
    after it is built, so a run reads them at the moment it reports
    rather than at the moment it was constructed.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    #: Where the run's progress events go.
    on_progress: OnProgress | None = None
    #: The bridge a paused run asks for approval through.
    hitl_coordinator: SkipValidation[HitlCoordinatorProtocol | None] = None
    #: Where the run's final visualisation goes in the event log.
    event_appender: EventAppender | None = None
    #: The runs a cancel has to be able to reach. A stream built
    #: without one gets an empty registry of its own — the run is then
    #: reachable only by the caller that holds it.
    subagent_registry: SkipValidation[SubAgentRegistry] = Field(default_factory=SubAgentRegistry)


class StreamContext(BaseModel):
    """What one stream drives, and where it reports to."""

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    #: The Agno agent or team being driven.
    runnable: SkipValidation[AgnoRunnable]
    #: The task the run starts with.
    task: str = ""
    #: The chain of agent names down to this run.
    agent_path: list[str] = Field(default_factory=list)
    #: The progress card every event of this run belongs to.
    card_id: str = ""
    #: The four sinks the run reports through.
    sinks: StreamSinks = Field(default_factory=StreamSinks)


class ResumeSpec(BaseModel):
    """The paused run a continued stream picks up.

    Two kinds of paused run exist: one Agno stored, paused for
    approvals the person has now answered (continued by ``run_id``),
    and one rebuilt from a call that was waiting on another agent
    (continued from ``run_response``, with that agent's answer as the
    call's result). ``run_response`` is empty for the first kind.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    #: The Agno run to continue.
    run_id: str
    #: The session the run belongs to.
    session_id: str
    #: The requirements the run is paused on.
    requirements: list[Any] = Field(default_factory=list)
    #: The rebuilt run to continue instead of the stored one.
    run_response: Any = None


__all__ = ["ResumeSpec", "StreamContext", "StreamSinks"]
