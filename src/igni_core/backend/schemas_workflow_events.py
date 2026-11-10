"""The wire protocol between the backend and ``workflow_runtime.mjs``.

Three directions:

1. **Stdout** — one JSON line per event. The four types the backend acts
   on are classes in the :data:`WorkflowRuntimeEvent` tagged union; every
   other type (``phase_*``, ``agent_*``, ``parallel_*``, ``pipeline_*``,
   ``log``) is a :class:`WorkflowProgressEvent`, carried untouched to the
   frontend. :class:`WorkflowRuntimeEventReader` parses a line into one.
2. **Stdin** — :class:`WorkflowCancelCommand` and
   :class:`AgentResponseMessage`.
3. **Session log and push** — :class:`WorkflowEventRecord`, the one
   shape both receive.

Forwarded payloads dump with ``exclude_unset=True`` and keep unknown keys
(``extra="allow"``), so the frontend gets exactly the keys the runtime sent.
"""

from __future__ import annotations

import time
from typing import Annotated, Any, ClassVar, Generic, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    TypeAdapter,
    ValidationError,
    ValidatorFunctionWrapHandler,
    model_validator,
)

from igni_core.backend.schemas_workflows import WorkflowMeta, WorkflowRunIdentity, WorkflowRunPhase
from igni_core.protocol.json_value import WireJson

#: Per-agent timeout. 10 minutes matches ClaudeCode's default for one-shot
#: agent calls; a workflow overrides it per call with ``timeoutSeconds``.
DEFAULT_AGENT_TIMEOUT_SECONDS = 600


# ── Stdout payloads ────────────────────────────────────────────────


class _RuntimePayload(BaseModel):
    """A payload the runtime wrote. Unknown keys are kept and forwarded."""

    model_config = ConfigDict(extra="allow")


PayloadT = TypeVar("PayloadT", bound=_RuntimePayload)


class WorkflowMetaPayload(_RuntimePayload):
    meta: WorkflowMeta
    path: str = ""


class AgentRequestPayload(_RuntimePayload):
    """An ``agent()`` call the script is blocked on until it gets a reply.

    ``rejected_because`` is set, instead of the event failing to parse, when
    the payload carries an ``id`` but some other field is malformed: the
    script is then answered with a refusal rather than left waiting.
    """

    model_config = ConfigDict(extra="allow", coerce_numbers_to_str=True)

    id: str
    # What ``team.arun`` accepts as input that JSON can carry: the script may
    # pass a message dict or a list of them, not only a string.
    prompt: str | list[Any] | dict[str, Any] = ""
    # The script's own name for the call; JS passes ``null`` through, and the
    # backend never reads it.
    label: str | None = None
    phase: str | None = None
    timeout_seconds: int | float = DEFAULT_AGENT_TIMEOUT_SECONDS
    rejected_because: str | None = Field(default=None, exclude=True)

    @model_validator(mode="wrap")
    @classmethod
    def _keep_the_id(cls, data: Any, handler: ValidatorFunctionWrapHandler) -> Any:
        try:
            return handler(data)
        except ValidationError as exc:
            if not isinstance(data, dict) or "id" not in data:
                raise
            return cls.model_construct(id=str(data["id"]), rejected_because=str(exc))

    @property
    def timeout(self) -> float | None:
        """The ``asyncio.wait_for`` timeout: ``None`` (no limit) when the
        script asked for ``<= 0``, as ``workflow_runtime.mjs`` reads it."""
        return self.timeout_seconds if self.timeout_seconds > 0 else None


class WorkflowCompletedPayload(_RuntimePayload):
    status: Literal["completed", "cancelled"] = "completed"
    duration_ms: int = 0
    result: Any = None


class WorkflowFailedPayload(_RuntimePayload):
    error: str = ""
    stack: str = ""
    duration_ms: int = 0


# ── Stdout events ──────────────────────────────────────────────────


class WorkflowRuntimeEventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts: int
    run_id: str
    seq: int = Field(ge=0)


class _TypedPayloadEvent(WorkflowRuntimeEventBase, Generic[PayloadT]):
    payload: PayloadT

    def payload_dump(self) -> dict[str, Any]:
        """The payload as the runtime sent it: set fields and unknown keys only."""
        return self.payload.model_dump(exclude_unset=True)


class WorkflowMetaEvent(_TypedPayloadEvent[WorkflowMetaPayload]):
    type: Literal["workflow_meta"] = "workflow_meta"


class AgentRequestEvent(_TypedPayloadEvent[AgentRequestPayload]):
    type: Literal["agent_request"] = "agent_request"


class WorkflowCompletedEvent(_TypedPayloadEvent[WorkflowCompletedPayload]):
    type: Literal["workflow_completed"] = "workflow_completed"
    payload: WorkflowCompletedPayload = Field(default_factory=WorkflowCompletedPayload)

    @property
    def phase(self) -> WorkflowRunPhase:
        if self.payload.status == "cancelled":
            return WorkflowRunPhase.CANCELLED
        return WorkflowRunPhase.COMPLETED


class WorkflowFailedEvent(_TypedPayloadEvent[WorkflowFailedPayload]):
    type: Literal["workflow_failed"] = "workflow_failed"
    payload: WorkflowFailedPayload = Field(default_factory=WorkflowFailedPayload)

    @property
    def phase(self) -> WorkflowRunPhase:
        return WorkflowRunPhase.FAILED


class WorkflowProgressEvent(WorkflowRuntimeEventBase):
    """Every type the backend only forwards (``phase_*``, ``agent_*``,
    ``parallel_*``, ``pipeline_*``, ``log``).

    The payload stays a dict on purpose: the backend never reads it, and
    typing each one means mirroring ``workflow_runtime.mjs`` field by field.
    """

    type: str
    payload: dict[str, Any] = Field(default_factory=dict)

    def payload_dump(self) -> dict[str, Any]:
        return dict(self.payload)


_TAGS = {
    "workflow_meta": "meta",
    "agent_request": "agent_request",
    "workflow_completed": "completed",
    "workflow_failed": "failed",
}


def _event_tag(value: Any) -> str | None:
    """The union member for one raw event: known types by name, the rest progress."""
    kind = value.get("type") if isinstance(value, dict) else getattr(value, "type", None)
    if not isinstance(kind, str):
        return None
    return _TAGS.get(kind, "progress")


WorkflowRuntimeEvent = Annotated[
    Annotated[WorkflowMetaEvent, Tag("meta")]
    | Annotated[AgentRequestEvent, Tag("agent_request")]
    | Annotated[WorkflowCompletedEvent, Tag("completed")]
    | Annotated[WorkflowFailedEvent, Tag("failed")]
    | Annotated[WorkflowProgressEvent, Tag("progress")],
    Discriminator(_event_tag),
]


class WorkflowRuntimeEventReader:
    """Parses the runtime's stdout lines into :data:`WorkflowRuntimeEvent`."""

    _adapter: ClassVar[TypeAdapter[WorkflowRuntimeEvent]] = TypeAdapter(WorkflowRuntimeEvent)

    @classmethod
    def parse(cls, raw: str) -> WorkflowRuntimeEvent:
        """One line; raises :class:`ValidationError` when it is not an event."""
        return cls._adapter.validate_json(raw)

    @classmethod
    def parse_line(cls, raw: str) -> WorkflowRuntimeEvent | None:
        """One line, or ``None`` for a blank or malformed one."""
        raw = raw.strip()
        if not raw:
            return None
        try:
            return cls.parse(raw)
        except ValidationError:
            return None

    @staticmethod
    def failed_type(exc: ValidationError) -> str | None:
        """The union member a validation error came from, when it got that far."""
        errors = exc.errors()
        loc = errors[0]["loc"] if errors else ()
        return str(loc[0]) if loc else None


# ── Stdin ──────────────────────────────────────────────────────────


class WorkflowCancelCommand(BaseModel):
    """Tells the runtime to reject its in-flight ``agent()`` calls and stop."""

    type: Literal["cancel"] = "cancel"


class AgentResponseMessage(BaseModel):
    """The reply to one :class:`AgentRequestEvent`.

    ``result`` is ``Any`` because the script receives whatever JSON the
    agent produced: text for a plain prompt, an object for a schema call.
    """

    type: Literal["agent_response"] = "agent_response"
    id: str
    ok: bool
    result: Any = None
    error: str | None = None

    @classmethod
    def success(cls, request_id: str, result: Any) -> AgentResponseMessage:
        return cls(id=request_id, ok=True, result=result)

    @classmethod
    def refusal(cls, request_id: str, error: str) -> AgentResponseMessage:
        return cls(id=request_id, ok=False, error=error)


# ── Session log and push ───────────────────────────────────────────


class SyntheticTerminalPayload(BaseModel):
    """What a run that exited without a terminal event ends with."""

    status: Literal["completed", "failed", "cancelled"]
    exit_code: int | None
    synthetic: Literal[True] = True


class WorkflowEventRecord(BaseModel):
    """One progress event from a running workflow, as pushed on the
    ``workflow_event`` channel and kept in the session log."""

    # ``payload`` is the runtime's payload, forwarded as-is, or a dumped
    # SyntheticTerminalPayload.
    model_config = ConfigDict(extra="forbid")

    workflow_run_id: str = Field(description="Id of the workflow run the event belongs to.")
    name: str = Field(description="Name of the workflow.")
    ts: int = Field(description="When the event happened, in Unix epoch milliseconds.")
    seq: int = Field(
        description=(
            "Order of the event within its run, from `0`. `-1` for the final event igni "
            "adds itself when the workflow process exits without reporting an end."
        )
    )
    type: str = Field(
        description=(
            "Event kind, e.g. `workflow_meta`, `phase_*`, `agent_*`, `parallel_*`, "
            "`pipeline_*`, `log`, and the final `workflow_completed` or `workflow_failed`."
        )
    )
    payload: dict[str, WireJson] = Field(
        description=(
            "Event details; keys depend on `type`. For the final event added by igni: "
            "`status` (`completed`, `failed` or `cancelled`), `exit_code` and `synthetic`."
        )
    )

    @classmethod
    def from_event(
        cls, identity: WorkflowRunIdentity, event: WorkflowRuntimeEvent
    ) -> WorkflowEventRecord:
        return cls(
            workflow_run_id=identity.workflow_run_id,
            name=identity.name,
            ts=event.ts,
            seq=event.seq,
            type=event.type,
            payload=event.payload_dump(),
        )

    @classmethod
    def synthetic(
        cls, identity: WorkflowRunIdentity, phase: WorkflowRunPhase, exit_code: int | None
    ) -> WorkflowEventRecord:
        status = phase.terminal_status
        return cls(
            workflow_run_id=identity.workflow_run_id,
            name=identity.name,
            ts=int(time.time() * 1000),
            seq=-1,
            type="workflow_failed" if status == "failed" else "workflow_completed",
            payload=SyntheticTerminalPayload(status=status, exit_code=exit_code).model_dump(),
        )
