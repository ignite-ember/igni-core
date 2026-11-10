"""The events an orchestrate run puts on the wire.

Every progress event a sub-agent or a team member produces is one of
these models. They form a closed set: each class's ``type`` is a
``Literal``, so the union of them is a discriminated union and a
reader can tell the kinds apart by asking the type, not by guessing
at a key.

Two things every event carries:

* ``agent_path`` — the dot-joined identity of the agent that
  produced it (``"architect.editor"``). The frontend uses it as the
  tree-node id.
* ``card_id`` — the progress card the event belongs to. It is
  stamped by the handler that emits it, so every event of one run
  lands on the same card across interleaved info items, a page
  refresh, and concurrent spawns.

The union is what a progress callback receives: see
:data:`igni_core.core.tools.orchestrate_contracts.OnProgress`.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

import jiter
from pydantic import BaseModel, ConfigDict, Field


class OrchestrateEventBase(BaseModel):
    """What every orchestrate event carries.

    The ``type`` is left open here and narrowed to a ``Literal`` on
    each subclass, which is what makes the union discriminated.
    """

    model_config = ConfigDict(populate_by_name=True)

    type: str
    agent_path: str
    card_id: str | None = None


class ToolStartedEvent(OrchestrateEventBase):
    """A tool call started."""

    type: Literal["tool_started"] = Field(default="tool_started", frozen=True)
    tool: str
    tool_call_id: str | None = None
    args: str = ""


class ToolCompletedEvent(OrchestrateEventBase):
    """A tool call finished — or failed, which is what ``is_error``
    says."""

    type: Literal["tool_completed"] = Field(default="tool_completed", frozen=True)
    tool: str
    tool_call_id: str | None = None
    result: str = ""
    is_error: bool = False


class AgentStartedEvent(OrchestrateEventBase):
    """A member of the run started."""

    type: Literal["agent_started"] = Field(default="agent_started", frozen=True)
    agent: str
    parent: str | None = None
    run_id: str | None = None
    task: str | None = None


class AgentPausedEvent(OrchestrateEventBase):
    """The run paused, waiting for someone to answer its approvals."""

    type: Literal["agent_paused"] = Field(default="agent_paused", frozen=True)
    count: int = 1


class AgentCompletedEvent(OrchestrateEventBase):
    """The run finished."""

    type: Literal["agent_completed"] = Field(default="agent_completed", frozen=True)
    is_error: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0


class RunErrorEvent(OrchestrateEventBase):
    """The run failed."""

    type: Literal["run_error"] = Field(default="run_error", frozen=True)
    error: str


class ContentPreviewEvent(OrchestrateEventBase):
    """A preview of what the agent is writing, mid-stream."""

    type: Literal["content_preview"] = Field(default="content_preview", frozen=True)
    text: str


class TaskCreatedEvent(OrchestrateEventBase):
    """A team task was created."""

    type: Literal["task_created"] = Field(default="task_created", frozen=True)
    title: str = ""
    assignee: str = ""


class TaskUpdatedEvent(OrchestrateEventBase):
    """A team task changed status."""

    type: Literal["task_updated"] = Field(default="task_updated", frozen=True)
    status: str = ""


class VisualizationDeltaEvent(OrchestrateEventBase):
    """One ``visualization_delta`` wire event.

    Sent to the frontend either as a progressive delta — the partial
    JSON as the visualizer sub-agent streams its tool-call arguments
    — or as a final one carrying the fully-parsed spec
    (``final=True``) once the tool call is complete.

    The frontend partial-parses ``spec_json`` on every delta and
    swaps in the completed spec when ``final`` is true. ``spec_id``
    dedupes so multiple deltas for the same card update in place
    instead of stacking. The wire uses the key name ``json`` (kept
    via the alias) to match the frontend contract without renaming
    the field there.
    """

    type: Literal["visualization_delta"] = Field(default="visualization_delta", frozen=True)
    spec_id: str
    spec_json: str = Field(alias="json")
    final: bool = False
    #: The card this delta belongs to. It is stamped like every other
    #: event's, but it is not part of this event's wire shape: the
    #: frontend routes a visualisation by its ``spec_id``, and the
    #: persisted log entry has to keep the shape it always had.
    card_id: str | None = Field(default=None, exclude=True)

    @classmethod
    def from_partial_args(
        cls,
        *,
        agent_path: str,
        spec_id: str,
        args_partial: str,
        final: bool = False,
    ) -> VisualizationDeltaEvent | None:
        """Build a delta event from a partial JSON tool-call arg string.

        Given the visualizer sub-agent's streaming ``arguments_partial``
        (e.g. ``'{"spec": {"root": "r", "elem'``), extract the
        ``spec`` sub-object and wrap it in a
        :class:`VisualizationDeltaEvent`. Uses ``jiter.from_json``
        with ``partial_mode='trailing-strings'``: tolerantly parses
        the incomplete outer object, salvaging as much nested
        structure as landed.

        Returns ``None`` when the arguments do not parse yet or
        ``spec`` is not a dict — callers can skip emission without an
        explicit try/except.
        """
        if not args_partial:
            return None
        try:
            parsed = jiter.from_json(args_partial.encode(), partial_mode="trailing-strings")
        except ValueError:
            # ``jiter`` raises ``ValueError`` for input it cannot
            # read at all; a truncated document parses to what it can.
            # Anything else is a bug in the caller, and hiding it
            # behind a "no spec yet" would lose it.
            return None
        if not isinstance(parsed, dict):
            return None
        spec = parsed.get("spec")
        if not isinstance(spec, dict):
            return None
        return cls(
            agent_path=agent_path,
            spec_id=spec_id,
            spec_json=json.dumps(spec),
            final=final,
        )


#: One orchestrate event — one of the kinds above, told apart by its
#: ``type``.
OrchestrateEvent = Annotated[
    ToolStartedEvent
    | ToolCompletedEvent
    | AgentStartedEvent
    | AgentPausedEvent
    | AgentCompletedEvent
    | RunErrorEvent
    | ContentPreviewEvent
    | TaskCreatedEvent
    | TaskUpdatedEvent
    | VisualizationDeltaEvent,
    Field(discriminator="type"),
]


__all__ = [
    "AgentCompletedEvent",
    "AgentPausedEvent",
    "AgentStartedEvent",
    "ContentPreviewEvent",
    "OrchestrateEvent",
    "OrchestrateEventBase",
    "RunErrorEvent",
    "TaskCreatedEvent",
    "TaskUpdatedEvent",
    "ToolCompletedEvent",
    "ToolStartedEvent",
    "VisualizationDeltaEvent",
]
