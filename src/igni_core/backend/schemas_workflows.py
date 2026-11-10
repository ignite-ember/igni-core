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
   ``WorkflowCancelOutcome``, ``WorkflowRunIdentity``, ``WorkflowStartResult`` and
   ``MetaProbeResult``.

Following the same pattern as
:mod:`igni_core.backend.schemas_rpc`: every wire shape is a
Pydantic model, every construction site uses ``model_dump()`` at
the boundary, shape drift fails loud at validation time.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

# ── Discovery (LIST_WORKFLOWS) ─────────────────────────────────────


class WorkflowPhase(BaseModel):
    """One step of a workflow, as the workflow describes itself."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="Short name of the phase.")
    detail: str = Field(
        default="", description="Longer explanation of what the phase does; may be empty."
    )


class WorkflowMeta(BaseModel):
    """The ``meta`` export of a workflow file.

    Mirrors the CC convention. The runner reads this once at
    discovery and surfaces it to the FE for the workflow
    autocomplete + describe panel. ``when_to_use`` is ``whenToUse``
    on the wire, both ways.
    """

    model_config = ConfigDict(extra="forbid", validate_by_name=True, serialize_by_alias=True)

    name: str
    description: str = ""
    when_to_use: str = Field(default="", alias="whenToUse")
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
            when_to_use=self.meta.when_to_use,
            phases=self.meta.phases,
            path=self.path,
        )


class WorkflowListItem(BaseModel):
    """A workflow script available to run in this project."""

    # ``when_to_use`` goes out as ``whenToUse`` — the model serialises by
    # alias, so a plain ``model_dump()`` emits the wire name.
    model_config = ConfigDict(extra="forbid", validate_by_name=True, serialize_by_alias=True)

    name: str = Field(description="The workflow's name; pass it to run the workflow.")
    description: str = Field(default="", description="What the workflow does; may be empty.")
    when_to_use: str = Field(
        default="",
        alias="whenToUse",
        description="When the workflow is the right choice; may be empty.",
    )
    phases: list[WorkflowPhase] = Field(description="The workflow's steps, in order.")
    path: str = Field(
        description=(
            "Where the workflow file is: relative to the project root, or absolute "
            "for a workflow outside the project (e.g. in the user's home)."
        )
    )


# ── RPC contracts (LIST / RUN / CANCEL_WORKFLOW) ───────────────────


class _WorkflowRpcArgs(BaseModel):
    """The arguments of a workflow RPC.

    Keys the model doesn't name are ignored: the FE and the extension
    host send ``session_id`` too, and routing has already used it by
    the time a handler runs. Numbers are taken as strings, as the
    handlers' ``str(...)`` reads always did.
    """

    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)


class WorkflowListRequest(_WorkflowRpcArgs):
    """``LIST_WORKFLOWS`` params: none. Whatever the caller sends
    (``session_id`` and the like) is ignored."""


class WorkflowRunRequest(_WorkflowRpcArgs):
    """Arguments for starting a workflow."""

    name: str = Field(min_length=1, description="Name of the workflow to run.")
    args: dict[str, JsonValue] = Field(
        default_factory=dict,
        description=(
            "The workflow's own input, passed to it unchanged. Each workflow defines "
            "the keys it reads; omit for none."
        ),
    )

    @field_validator("args", mode="before")
    @classmethod
    def _absent_args_are_empty(cls, value: object) -> object:
        return value or {}


class WorkflowRunResponse(BaseModel):
    """The workflow run that was started.

    Returned as soon as the run starts; its progress then arrives as
    ``workflow_event`` pushes carrying the same ``workflow_run_id``.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_run_id: str = Field(
        description="Id of the new run; use it to match events and to cancel the run."
    )
    name: str = Field(description="Name of the workflow that was started.")


class WorkflowCancelRequest(_WorkflowRpcArgs):
    """Arguments for cancelling a running workflow."""

    workflow_run_id: str = Field(min_length=1, description="Id of the workflow run to cancel.")

    def answer(self, outcome: WorkflowCancelOutcome) -> WorkflowCancelResponse:
        """The response to this request, echoing the id it named."""
        return WorkflowCancelResponse(
            cancelled=outcome.cancelled, workflow_run_id=self.workflow_run_id
        )


class WorkflowCancelResponse(BaseModel):
    """Result of cancelling a workflow run."""

    model_config = ConfigDict(extra="forbid")

    cancelled: bool = Field(
        description=(
            "`true` when the run was stopped. `false` when no such run is active: it "
            "already finished, was already cancelled, or the id is unknown."
        )
    )
    workflow_run_id: str = Field(description="Id of the run the request named.")


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


class WorkflowCancelOutcome(StrEnum):
    """What asking to cancel one run came to.

    The wire only says whether the run was cancelled
    (:attr:`cancelled`); the other two outcomes are kept apart here so
    the code that produces them says which one it meant.
    """

    NOT_LIVE = "not_live"
    ALREADY_EXITED = "already_exited"
    CANCELLED = "cancelled"

    @property
    def cancelled(self) -> bool:
        return self is WorkflowCancelOutcome.CANCELLED


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
