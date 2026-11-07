"""Typed schemas for the workflow runner surface.

Three concerns:

1. ``WorkflowMeta`` — what a workflow advertises (read from the
   ``meta`` export of every ``.igni/workflows/*.mjs``). Mirrors
   the CC convention: ``{name, description, whenToUse, phases}``.

2. ``WorkflowEvent`` — the envelope the Node subprocess emits
   one JSON line at a time on stdout. Every event has a
   ``run_id`` (so the BE can route re-connections) and a
   monotonic ``seq`` (so the FE can re-order if a push
   arrives out of order).

3. The RPC contracts: ``WorkflowListItem`` (one ``LIST_WORKFLOWS``
   row), ``WorkflowRunRequest`` / ``WorkflowRunResponse``
   (``RUN_WORKFLOW``) and ``WorkflowCancelRequest`` /
   ``WorkflowCancelResponse`` (``CANCEL_WORKFLOW``).

Following the same pattern as
:mod:`igni_core.backend.schemas_rpc`: every wire shape is a
Pydantic model, every construction site uses ``model_dump()`` at
the boundary, shape drift fails loud at validation time.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal, Self

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


# ── Runtime event envelope (subprocess stdout) ─────────────────────


class WorkflowEvent(BaseModel):
    """One JSON line emitted by ``workflow_runtime.mjs`` on stdout.

    The BE writes one of these into ``session.append_event`` per
    line, with the event_type normalized to ``"workflow_event"``,
    and pushes it on the ``workflow_event`` push channel.

    ``payload`` is intentionally a free-form dict — each event
    ``type`` has its own shape, and the FE reducer folds by type.
    Putting every variant on this model would force both ends to
    keep a giant ``Union`` in sync; the FE is happy to do
    ``switch(ev.type)`` on the raw dict.
    """

    model_config = ConfigDict(extra="forbid")

    ts: int
    run_id: str
    seq: int = Field(ge=0)
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


# ── RPC contracts (RUN_WORKFLOW / CANCEL_WORKFLOW) ─────────────────


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
