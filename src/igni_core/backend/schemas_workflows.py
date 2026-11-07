"""Typed schemas for the workflow runner surface.

Four concerns:

1. ``WorkflowMeta`` — what a workflow advertises (read from the
   ``meta`` export of every ``.igni/workflows/*.mjs``). Mirrors
   the CC convention: ``{name, description, whenToUse, phases}``.

2. The runtime's stdout/stdin protocol lives in
   :mod:`igni_core.backend.schemas_workflow_events`.

3. The RPC contracts: ``WorkflowListRequest`` / ``WorkflowListItem``
   (``LIST_WORKFLOWS``), ``WorkflowRunRequest`` / ``WorkflowRunResponse``
   (``RUN_WORKFLOW``) and ``WorkflowCancelRequest`` /
   ``WorkflowCancelResponse`` (``CANCEL_WORKFLOW``).

4. A run's state and results: ``WorkflowRunPhase``,
   ``WorkflowRunIdentity``, ``WorkflowStartResult`` and
   ``MetaProbeResult``.

Following the same pattern as
:mod:`igni_core.backend.schemas_rpc`: every wire shape is a
Pydantic model, every construction site uses ``model_dump()`` at
the boundary, shape drift fails loud at validation time.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_core import ErrorDetails

# ── Discovery (LIST_WORKFLOWS) ─────────────────────────────────────


class WorkflowPhase(BaseModel):
    """One phase in a workflow's published ``meta.phases`` list."""

    model_config = ConfigDict(extra="forbid")

    title: str
    detail: str = ""


class WorkflowMeta(BaseModel):
    """The ``meta`` export of a workflow file.

    Mirrors the CC convention. The runner reads this once at
    discovery and surfaces it to the FE for the workflow
    autocomplete + describe panel.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    whenToUse: str = ""
    phases: list[WorkflowPhase] = Field(default_factory=list)


class WorkflowMetaEnvelope(BaseModel):
    """One row of the ``LIST_WORKFLOWS`` response.

    Wraps :class:`WorkflowMeta` with the on-disk path so the FE
    can deep-link / show the source. ``path`` is relative to
    the project root (typically ``.igni/workflows/<name>.mjs``), or
    absolute for a workflow outside it (``~/.igni/workflows``, the
    group policy cache).
    """

    model_config = ConfigDict(extra="forbid")

    meta: WorkflowMeta
    path: str

    def to_list_item(self) -> WorkflowListItem:
        """Flatten ``meta`` and ``path`` into the row the FE renders."""
        return WorkflowListItem(
            name=self.meta.name,
            description=self.meta.description,
            when_to_use=self.meta.whenToUse,
            phases=self.meta.phases,
            path=self.path,
        )


class WorkflowListItem(BaseModel):
    """One ``LIST_WORKFLOWS`` row: ``{name, description, whenToUse, phases, path}``.

    ``when_to_use`` goes out as ``whenToUse`` — the model serialises
    by alias, so the dispatcher's plain ``model_dump()`` emits the
    wire name.
    """

    model_config = ConfigDict(extra="forbid", validate_by_name=True, serialize_by_alias=True)

    name: str
    description: str = ""
    when_to_use: str = Field("", alias="whenToUse")
    phases: list[WorkflowPhase]
    path: str


# ── RPC contracts (LIST / RUN / CANCEL_WORKFLOW) ───────────────────


class _RpcParams(BaseModel):
    """The params of one RPC, read from the call's raw ``args`` dict.

    Keys the model doesn't name are ignored: the FE and the extension
    host send ``session_id`` too, and routing has already used it by
    the time a handler runs. Numbers are taken as strings, as the
    handlers' ``str(...)`` reads always did.

    Malformed params are a caller bug, so :meth:`from_rpc` raises —
    a ``ValueError`` worded ``"<rpc>: '<field>' is required"``, the
    text the FE and extensions have always seen.
    """

    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    rpc_name: ClassVar[str]

    @classmethod
    def from_rpc(cls, args: dict[str, Any]) -> Self:
        try:
            return cls.model_validate(args)
        except ValidationError as exc:
            raise ValueError(cls._refusal(exc.errors()[0], args)) from exc

    @classmethod
    def _refusal(cls, error: ErrorDetails, args: dict[str, Any]) -> str:
        field = str(error["loc"][0]) if error["loc"] else ""
        if error["type"] in ("missing", "string_too_short"):
            return f"{cls.rpc_name}: '{field}' is required"
        if error["type"] == "dict_type":
            got = type(args.get(field)).__name__
            return f"{cls.rpc_name}: '{field}' must be a dict, got {got}"
        return f"{cls.rpc_name}: '{field}': {error['msg']}"


class WorkflowListRequest(_RpcParams):
    """``LIST_WORKFLOWS`` params: none. Whatever the caller sends
    (``session_id`` and the like) is ignored."""

    rpc_name: ClassVar[str] = "list_workflows"


class WorkflowRunRequest(_RpcParams):
    """``RUN_WORKFLOW`` params.

    ``args`` is a free-form bag on purpose: it is handed to the
    workflow script as JSON, and each workflow reads its own keys.
    """

    rpc_name: ClassVar[str] = "run_workflow"

    name: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)

    @field_validator("args", mode="before")
    @classmethod
    def _absent_args_are_empty(cls, value: Any) -> Any:
        return value or {}


class WorkflowRunResponse(BaseModel):
    """``RUN_WORKFLOW`` response.

    Returned synchronously — the run is async on the BE side. The
    FE optimistically creates a workflow ``ChatItem`` keyed by
    ``workflow_run_id`` and folds subsequent ``workflow_event``
    pushes into it.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_run_id: str
    name: str


class WorkflowCancelRequest(_RpcParams):
    """``CANCEL_WORKFLOW`` params."""

    rpc_name: ClassVar[str] = "cancel_workflow"

    workflow_run_id: str = Field(min_length=1)

    def answer(self, cancelled: bool) -> WorkflowCancelResponse:
        """The response to this request, echoing the id it named."""
        return WorkflowCancelResponse(cancelled=cancelled, workflow_run_id=self.workflow_run_id)


class WorkflowCancelResponse(BaseModel):
    """``CANCEL_WORKFLOW`` response.

    ``cancelled`` is false when no such run is active — already
    finished, already cancelled, or an unknown id are the same
    outcome to the caller.
    """

    model_config = ConfigDict(extra="forbid")

    cancelled: bool
    workflow_run_id: str


# ── Status (used by FE reducer; also stable across the wire) ──────


WorkflowStatus = Literal["running", "completed", "failed", "cancelled"]


# ── Run state and results ──────────────────────────────────────────


class WorkflowRunPhase(StrEnum):
    """Where one run is. The terminal values are the :data:`WorkflowStatus`
    strings the frontend reads."""

    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (
            WorkflowRunPhase.COMPLETED,
            WorkflowRunPhase.FAILED,
            WorkflowRunPhase.CANCELLED,
        )

    @property
    def terminal_status(self) -> Literal["completed", "failed", "cancelled"]:
        """This phase as a terminal :data:`WorkflowStatus`; raises for a live one."""
        if self is WorkflowRunPhase.COMPLETED:
            return "completed"
        if self is WorkflowRunPhase.FAILED:
            return "failed"
        if self is WorkflowRunPhase.CANCELLED:
            return "cancelled"
        raise ValueError(f"{self.value} is not a terminal phase")


class WorkflowRunIdentity(BaseModel):
    """Which run an event belongs to: its id and the name it was started by."""

    model_config = ConfigDict(frozen=True)

    workflow_run_id: str
    name: str

    @classmethod
    def new(cls, name: str) -> WorkflowRunIdentity:
        return cls(workflow_run_id=f"wf_{uuid4().hex[:12]}", name=name)


class WorkflowStartResult(BaseModel):
    """What :meth:`WorkflowRunner.run` returns.

    A miss is an expected outcome, so it is a result rather than a raise;
    the RPC boundary turns it back into the exception callers have always
    seen (:meth:`to_response`).
    """

    ok: bool
    reason: str = ""
    failure: Literal["not_found", "runtime_missing"] | None = None
    workflow_run_id: str = ""
    name: str = ""

    @classmethod
    def started(cls, identity: WorkflowRunIdentity) -> WorkflowStartResult:
        return cls(ok=True, workflow_run_id=identity.workflow_run_id, name=identity.name)

    @classmethod
    def not_found(cls, reason: str) -> WorkflowStartResult:
        return cls(ok=False, reason=reason, failure="not_found")

    @classmethod
    def runtime_missing(cls, reason: str) -> WorkflowStartResult:
        return cls(ok=False, reason=reason, failure="runtime_missing")

    def to_response(self) -> WorkflowRunResponse:
        """The ``RUN_WORKFLOW`` response for a started run; raises on a miss.

        This is the RPC boundary, and the raise is deliberate. Both
        dispatch paths read failure from the exception: the message
        dispatcher turns it into ``RPCResponse.fail``, and the extension
        host's ``HostBackendGateway`` maps ``FileNotFoundError`` to a
        REFUSED error while letting ``RuntimeError`` through as a
        failure. Returning a failure model here would reach the
        extension host as a success. Stop raising only once both paths
        understand a typed RPC failure model.
        """
        if not self.ok:
            raise self._as_exception()
        return WorkflowRunResponse(workflow_run_id=self.workflow_run_id, name=self.name)

    def _as_exception(self) -> Exception:
        if self.failure == "not_found":
            return FileNotFoundError(self.reason)
        return RuntimeError(self.reason)


class MetaProbeResult(BaseModel):
    """What running one workflow file with ``--discovery`` produced."""

    meta: WorkflowMeta | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.meta is not None
