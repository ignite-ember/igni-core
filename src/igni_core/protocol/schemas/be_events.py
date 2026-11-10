"""Messages igni sends a client — run / tool / task / status / command events.

Run-scoped events inherit :class:`RunScopedMessage` from
:mod:`.envelope` so ``run_id`` + ``parent_run_id`` live on one
base class (flat on the wire, so no FE client breaks).

TODO — ``HITLRequest.tool_args`` stays ``dict[str, Any]`` for
now. Tightening to a shared ``ToolInvocationArgs`` model would
also close over :class:`ToolStarted.args_summary`,
:class:`ToolCompleted.summary`, and the Agno tool-arg surface —
deferred until Agno's tool-arg schema stabilises.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from igni_core.protocol.json_value import JSON_OBJECT_EXTRA, WireJson
from igni_core.protocol.schemas.enums import (
    CommandAction,
    CommandResultKind,
    OrchestrationTaskStatus,
)
from igni_core.protocol.schemas.envelope import Direction, Message, RunScopedMessage

# Note: :class:`PermissionModeName` (for ``StatusUpdate.permission_mode``)
# and :class:`SchedulerEventType` (for ``SchedulerEvent.event_type``)
# are referenced by field docstrings but not imported here —
# those fields stay ``str`` on the wire for forward-compat.
# Producers import the enums directly from :mod:`.enums`.


class ContentDelta(Message):
    """A chunk of the model's reply, streamed as it is written.

    Append each chunk's ``text`` to what came before to build the reply.
    """

    type: Literal["content_delta"] = "content_delta"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    text: str = Field(
        default="", description="The next piece of text, to append to the earlier ones."
    )
    is_thinking: bool = Field(
        default=False,
        description="True when the text is the model's reasoning rather than its visible reply.",
    )


class ToolStarted(RunScopedMessage):
    """A tool call has begun."""

    type: Literal["tool_started"] = "tool_started"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    tool_name: str = Field(default="", description="The tool's name, as the model called it.")
    friendly_name: str = Field(
        default="",
        description="A human-readable name for the tool, for display; empty when there is none.",
    )
    args_summary: str = Field(
        default="", description="A short, human-readable summary of the call's arguments."
    )


class ToolCompleted(RunScopedMessage):
    """A tool call has finished."""

    type: Literal["tool_completed"] = "tool_completed"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    summary: str = Field(default="", description="A short, one-line summary of the result.")
    full_result: str = Field(default="", description="The tool's full output.")
    has_markup: bool = Field(
        default=False,
        description="True when `full_result` contains rich-text markup rather than plain text.",
    )
    diff_rows: list[tuple[str, str]] | None = Field(
        default=None,
        description=(
            "For a tool that changed a file: the diff to show, as `[text, style]` pairs, one per "
            "line. Null when the tool produced no diff."
        ),
    )
    # The TUI renders ``✗`` with red styling instead of ``✓`` so the user
    # can see at a glance which calls in a batch actually failed.
    is_error: bool = Field(
        default=False,
        description=(
            "True when the tool failed: it raised, or returned text starting with `Error:`."
        ),
    )
    is_rejected: bool = Field(
        default=False,
        description=(
            "True when the call never ran: the person rejected it when asked for approval, "
            "or nobody could be asked."
        ),
    )


class ToolError(RunScopedMessage):
    """A tool call failed."""

    type: Literal["tool_error"] = "tool_error"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    error: str = Field(default="", description="What went wrong, as a human-readable message.")


class ModelCompleted(RunScopedMessage):
    """One model call within a run has finished; its token counts are known."""

    type: Literal["model_completed"] = "model_completed"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    input_tokens: int = Field(default=0, description="Tokens sent to the model in this call.")
    output_tokens: int = Field(default=0, description="Tokens the model produced in this call.")


class RunStarted(RunScopedMessage):
    """An agent or team run has begun."""

    type: Literal["run_started"] = "run_started"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    agent_name: str = Field(
        default="", description="The name of the agent (or team) doing the run."
    )
    model: str = Field(default="", description="The id of the model the run uses.")


class RunCompleted(RunScopedMessage):
    """An agent or team run has finished."""

    type: Literal["run_completed"] = "run_completed"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    input_tokens: int = Field(default=0, description="Tokens sent to the model over the whole run.")
    output_tokens: int = Field(
        default=0, description="Tokens the model produced over the whole run, reasoning included."
    )
    reasoning_tokens: int = Field(
        default=0,
        description=(
            "The part of `output_tokens` spent on the model's reasoning. The visible reply is "
            "`output_tokens - reasoning_tokens`."
        ),
    )
    duration: float = Field(default=0.0, description="How long the run took, in seconds.")


class StreamingDone(RunScopedMessage):
    """The model's reply is complete, though the run is still wrapping up.

    igni may spend a few more seconds saving and summarising before
    ``run_completed``. A client can let the person type and send their
    next message as soon as this arrives: igni starts the next run once
    the current one has fully finished.
    """

    # The BE serialises the next ``run_message`` behind an internal lock
    # until the post-stream tail (compression, memory, persistence,
    # metrics) drains inside Agno.

    type: Literal["streaming_done"] = "streaming_done"
    direction: ClassVar[Direction] = Direction.TO_CLIENT


class RunError(Message):
    """The run failed and has stopped."""

    type: Literal["run_error"] = "run_error"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    error: str = Field(default="", description="What went wrong, as a human-readable message.")


class ReasoningStarted(RunScopedMessage):
    """The model has started reasoning ("thinking") before it replies."""

    type: Literal["reasoning_started"] = "reasoning_started"
    direction: ClassVar[Direction] = Direction.TO_CLIENT


class HITLRequest(Message):
    """A tool call that needs the person's approval before it runs.

    The run waits until a client answers with ``hitl_response`` (or
    ``hitl_response_batch``), quoting ``requirement_id``.
    """

    type: Literal["hitl_request"] = "hitl_request"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    requirement_id: str = Field(default="", description="The request's id; quote it in the answer.")
    tool_name: str = Field(default="", description="The tool's name, as the model called it.")
    friendly_name: str = Field(
        default="",
        description="A human-readable name for the tool, for display; empty when there is none.",
    )
    # ``tool_args`` stays ``dict[str, Any]`` because it carries
    # tool-invocation payloads produced by Agno / third-party MCP
    # servers that we don't control the schema of. Tightening this
    # to a shared ``ToolInvocationArgs`` model is a follow-up (would
    # need to be shared with ``ToolStarted`` / ``ToolCompleted``
    # and depends on a closed Agno tool-arg surface — deferred).
    tool_args: dict[str, WireJson] = Field(
        default_factory=dict, description="The arguments the tool would be called with."
    )
    details: str = Field(
        default="",
        description="Why approval is being asked, for the person; empty when there is no reason given.",
    )
    details_pending: bool = Field(
        default=False,
        description=(
            "True when `details` is still being written; it arrives later in a "
            "`requirement_details` message. Show a placeholder until then."
        ),
    )
    agent_path: list[str] = Field(
        default_factory=list,
        description=(
            "The chain of agents that made the call, outermost first. Empty (or one entry) "
            'for the main agent; for example `["architect", "reviewer"]` when a reviewer '
            "started by an architect asked."
        ),
    )

    @classmethod
    def from_agno_requirement(
        cls,
        requirement: Any,
        friendly_name_lookup: Any,
    ) -> HITLRequest:
        """Build a ``HITLRequest`` from an Agno pause requirement.

        ``friendly_name_lookup`` is any callable with the shape
        ``(tool_name: str, default: str = "") -> str`` — in practice
        :meth:`AgnoToolEventFormatter.friendly_name`. Injected rather
        than imported so the protocol layer stays leaf (no Agno /
        formatter imports at module scope).
        """
        tool_exec = getattr(requirement, "tool_execution", None)
        raw_name = str(getattr(tool_exec, "tool_name", "") if tool_exec else "")
        return cls(
            requirement_id=str(id(requirement)),
            tool_name=raw_name,
            friendly_name=friendly_name_lookup(raw_name, default=""),
            tool_args=dict(getattr(tool_exec, "tool_args", {}) if tool_exec else {}),
        )


class TaskCreated(Message):
    """A team run has created a task for one of its members."""

    # ``status`` is a raw ``str`` on the wire; producers SHOULD use
    # ``OrchestrationTaskStatus`` members. Kept as ``str`` so an
    # Agno-emitted status we haven't yet enumerated survives verbatim.

    type: Literal["task_created"] = "task_created"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    task_id: str = Field(default="", description="The task's id.")
    title: str = Field(default="", description="What the task is, in a few words.")
    assignee: str = Field(
        default="", description="The name of the member doing it; empty when unassigned."
    )
    status: str = Field(
        default=OrchestrationTaskStatus.PENDING.value,
        description=(
            "The task's status: `pending`, `running`, `completed`, `failed` or `cancelled`. "
            "Other values may appear; treat them as unknown."
        ),
    )


class TaskUpdated(Message):
    """A team run's task changed status or assignee."""

    type: Literal["task_updated"] = "task_updated"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    task_id: str = Field(default="", description="The task's id, as in `task_created`.")
    status: str = Field(
        default="",
        description=(
            "The task's new status: `pending`, `running`, `completed`, `failed` or "
            "`cancelled`. Other values may appear; treat them as unknown."
        ),
    )
    assignee: str = Field(
        default="", description="The name of the member doing it; empty when unassigned."
    )


class TaskIteration(Message):
    """A team run has started another round of work on its tasks."""

    type: Literal["task_iteration"] = "task_iteration"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    iteration: int = Field(default=0, description="The number of the round now under way.")
    max_iterations: int = Field(default=0, description="The most rounds the run will take.")


class TaskSnapshot(BaseModel):
    """One task of a team run, as it stands now."""

    # Pydantic's dict-coercion means ``TaskStateUpdated(tasks=[{...}])``
    # still works for callers that pass raw dicts.

    task_id: str = Field(default="", description="The task's id.")
    title: str = Field(default="", description="What the task is, in a few words.")
    status: str = Field(
        default="",
        description=(
            "The task's status: `pending`, `running`, `completed`, `failed` or `cancelled`. "
            "Other values may appear; treat them as unknown."
        ),
    )
    assignee: str = Field(
        default="", description="The name of the member doing it; empty when unassigned."
    )

    @classmethod
    def from_agno(cls, task: Any) -> TaskSnapshot:
        """Extract a snapshot from an Agno task object.

        Reads via ``getattr`` so this stays tolerant of the Agno
        team-event task shape (which is a dataclass in current
        versions but has changed shape historically). ``status``
        is a raw ``str`` — producers can compare to
        :class:`OrchestrationTaskStatus` members for typed
        dispatch, but the wire preserves the original Agno
        string verbatim.
        """
        return cls(
            task_id=str(getattr(task, "task_id", "")),
            title=str(getattr(task, "title", "")),
            status=str(getattr(task, "status", "")),
            assignee=str(getattr(task, "assignee", "") or ""),
        )


class TaskStateUpdated(Message):
    """Every task of a team run, as they stand now."""

    type: Literal["task_state_updated"] = "task_state_updated"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    tasks: list[TaskSnapshot] = Field(
        default_factory=list, description="All the run's tasks; replaces any earlier list."
    )


class CommandResult(Message):
    """The result of a slash command: text to show, an action for the client to take, or both."""

    # The single Python subclass ``igni_core.backend.command_result.CommandResult``
    # inherits from this class and adds the factories and behaviour
    # methods; the backend class IS the wire model.

    type: Literal["command_result"] = "command_result"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    kind: CommandResultKind = Field(
        default=CommandResultKind.INFO, description="How to show `content`."
    )
    content: str = Field(
        default="",
        description=(
            "The text to show. For some actions it carries the action's value instead: the "
            "new session id for `fork`, the prompt to send for `run_prompt`."
        ),
    )
    action: CommandAction = Field(
        default=CommandAction.NONE,
        description="What the client should do in response; empty for nothing but showing `content`.",
    )
    display_content: str = Field(
        default="",
        description=(
            "For `run_prompt`: the text to show in the chat in place of `content`, when the "
            "prompt sent to the agent differs from what the person should see. Empty to show "
            "`content`."
        ),
    )


class StatusUpdate(Message):
    """The session's current state, for a status bar: token use, model, account and permission mode."""

    type: Literal["status_update"] = "status_update"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    input_tokens: int = Field(
        default=0, description="Tokens sent to the model so far in this session."
    )
    output_tokens: int = Field(
        default=0, description="Tokens the model has produced so far in this session."
    )
    context_tokens: int = Field(
        default=0, description="Tokens the conversation currently takes up in the model's context."
    )
    max_context: int = Field(
        default=0, description="The model's context window, in tokens; 0 when unknown."
    )
    model: str = Field(default="", description="The id of the model in use.")
    # Defaults True so an older client, which never read the field,
    # keeps its previous behaviour.
    model_configured: bool = Field(
        default=True,
        description="False when no model is set up, so a run could not reach one.",
    )
    cloud_connected: bool = Field(default=False, description="True when signed in to igni cloud.")
    cloud_org: str = Field(
        default="",
        description="The igni cloud organisation signed in to; empty when not signed in.",
    )
    cloud_email: str = Field(
        default="", description="The signed-in account's email address; empty when not signed in."
    )
    cloud_name: str = Field(
        default="",
        description=(
            "The signed-in account's display name. Empty until igni has fetched it; show "
            "`cloud_email` meanwhile."
        ),
    )
    # Producers SHOULD use ``PermissionModeName`` members; kept as
    # ``str`` for forward-compat with any future mode.
    permission_mode: str = Field(
        default="default",
        description=(
            "The active permission mode: `default`, `plan`, `acceptEdits`, "
            "`bypassPermissions` or `dontAsk`. Other values may appear; treat them as unknown."
        ),
    )
    # Passed through untyped: the BE has no use for these values, and
    # the client validates every one against its own allowlist.
    theme: dict[str, WireJson] | None = Field(
        default=None,
        description=(
            "Brand colour overrides from the signed-in account's group, keyed by theme token. "
            "Null for igni's own palette."
        ),
    )


class SessionListEntry(BaseModel):
    """One saved session, as listed by ``session_list``.

    More fields may be present than those listed here; ignore any you
    do not know.
    """

    # Produced by ``SessionListRow.from_agno``. Permissive
    # (``extra='allow'``) so a new persistence-side field reaches the
    # wire without a schema-drift error. Named ``SessionListEntry`` to
    # avoid colliding with Agno's ``SessionSummary``.

    model_config = ConfigDict(extra="allow", json_schema_extra=JSON_OBJECT_EXTRA)

    session_id: str = Field(default="", description="The session's id.")
    name: str = Field(default="", description="The session's name; empty when it has none.")
    created_at: int = Field(default=0, description="When the session was created, in Unix seconds.")
    updated_at: int = Field(
        default=0, description="When the session last changed, in Unix seconds."
    )
    run_count: int = Field(default=0, description="How many runs the session holds.")
    summary: str = Field(
        default="", description="A short summary of the conversation; empty when there is none."
    )
    agent_name: str = Field(default="", description="The agent the session talks to.")


class SessionListResult(Message):
    """The answer to ``session_list``: the saved sessions."""

    type: Literal["session_list_result"] = "session_list_result"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    sessions: list[SessionListEntry] = Field(
        default_factory=list, description="The saved sessions."
    )


class SessionCleared(Message):
    """The session's conversation was cleared or compacted (replaced by a summary) to free context."""

    type: Literal["session_cleared"] = "session_cleared"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    new_session_id: str = Field(
        default="", description="The session's id from now on; it may be the same as before."
    )
    summary: str = Field(
        default="",
        description="The summary that now stands in for the earlier conversation; empty when there is none.",
    )


class Info(Message):
    """A short informational message to show the person."""

    type: Literal["info"] = "info"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    text: str = Field(default="", description="The message.")


class Error(Message):
    """An error message to show the person."""

    type: Literal["error"] = "error"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    text: str = Field(default="", description="What went wrong.")


class SchedulerEvent(Message):
    """A scheduled task started or finished."""

    # ``event_type`` stays a raw ``str``; producers SHOULD use
    # ``SchedulerEventType`` members.

    type: Literal["scheduler_event"] = "scheduler_event"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    task_id: str = Field(default="", description="The scheduled task's id.")
    description: str = Field(default="", description="What the task does, as it was scheduled.")
    event_type: str = Field(
        default="",
        description=(
            "What happened: `started`, `completed`, or `failed` (sometimes sent as `error`). "
            "Other values may appear; treat them as unknown."
        ),
    )
    result: str = Field(
        default="",
        description="The task's output when it finished, or the error when it failed; empty when it started.",
    )


class RunPaused(RunScopedMessage):
    """The run is paused until the person approves or rejects one or more tool calls.

    Answer every request with ``hitl_response_batch`` (or each one with
    ``hitl_response``) to let the run continue.
    """

    type: Literal["run_paused"] = "run_paused"
    direction: ClassVar[Direction] = Direction.TO_CLIENT
    requirements: list[HITLRequest] = Field(
        default_factory=list, description="The tool calls waiting for an answer."
    )


__all__ = [
    "ContentDelta",
    "ToolStarted",
    "ToolCompleted",
    "ToolError",
    "ModelCompleted",
    "RunStarted",
    "RunCompleted",
    "StreamingDone",
    "RunError",
    "ReasoningStarted",
    "HITLRequest",
    "TaskCreated",
    "TaskUpdated",
    "TaskIteration",
    "TaskSnapshot",
    "TaskStateUpdated",
    "CommandResult",
    "StatusUpdate",
    "SessionListEntry",
    "SessionListResult",
    "SessionCleared",
    "Info",
    "Error",
    "SchedulerEvent",
    "RunPaused",
]
