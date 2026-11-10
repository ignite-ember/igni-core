"""Typed schemas for the chat-history rebuild pipeline.

Extracted out of :mod:`igni_core.backend.server_history` — the old
free-function module used raw dict literals for every turn shape, ~30
loose ``getattr(msg, ..., default)`` calls to read Agno's per-message
struct, and ``payload.get("json")`` to peek into
``visualization_delta`` event payloads. Every dict / loose-attr shape
at a boundary now lives here as a Pydantic model so mypy + Ruff +
Pydantic validation give schema coverage at every seam. Same
rationale as the sibling :mod:`schemas_run` module.

Consumers:

* :class:`ChatTurn` — the tagged union of every wire-shape a
  rebuilt chat can emit. Each subclass has a ``role: Literal[...]``
  discriminator so :meth:`BaseModel.model_dump` round-trips through
  the exact same ``{"role": "...", ...}`` dict the FE and search
  helpers expect.
* :class:`AgnoRunMessageView` — the one-shot typed wrapper the
  walker builds from an Agno persisted-message record at the top
  of each iteration. Replaces the ~30 ``getattr`` reads. Uses
  ``extra='ignore'`` (not ``allow``) so unknown Agno fields drop
  silently without ballooning the model — a schema rename upstream
  is caught by the per-field defaults kicking in.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# :class:`SessionListEntry` is defined in :mod:`igni_core.protocol.messages`
# (the leaf layer — protocol never depends on backend) and re-exported
# here so backend callers can pick it up alongside the other chat-history
# schemas from one place. Kept as an explicit re-export in ``__all__``
# so ``from schemas_history import SessionListEntry`` keeps working.
from igni_core.backend.schemas_plan import PlanState
from igni_core.protocol.json_value import WireJson
from igni_core.protocol.messages import SessionListEntry


# Every rebuilt-history turn carries ``run_id`` so the client can map
# back to the owning Agno run for edit/delete truncation. ``created_at``
# is Agno-issued epoch seconds and is set only on conversational turns
# (user/assistant/thinking/tool/plan) — the per-run ``stats`` badge and
# spliced ``visualization`` turns intentionally omit ``created_at`` on
# the wire to match the pre-refactor free-function output exactly.
class _RunOwned(BaseModel):
    """Base for chat-history turns that belong to an agent run."""

    run_id: str = Field(
        default="",
        description=(
            "Id of the agent run this turn belongs to. Pass it to "
            "`truncate_history` to edit or delete from this point."
        ),
    )


class _TimedTurn(_RunOwned):
    """Conversational turn with a per-message wall-clock stamp."""

    created_at: int = Field(
        default=0,
        description="When the message was created, in Unix epoch seconds; `0` if unknown.",
    )


class UserTurn(_TimedTurn):
    """A message the user sent."""

    role: Literal["user"] = Field(default="user", description="Turn kind; always `user`.")
    content: str = Field(default="", description="The text the user sent.")


class AssistantTurn(_TimedTurn):
    """A visible reply from the assistant.

    One run can produce several of these when the reply was
    interleaved with thinking blocks."""

    role: Literal["assistant"] = Field(
        default="assistant", description="Turn kind; always `assistant`."
    )
    content: str = Field(default="", description="The reply text (Markdown).")


class ThinkingTurn(_TimedTurn):
    """The model's reasoning ("thinking") for part of a run.

    Comes either from the model's separate reasoning output or from an
    inline ``<think>`` block in the reply."""

    role: Literal["thinking"] = Field(
        default="thinking", description="Turn kind; always `thinking`."
    )
    content: str = Field(default="", description="The reasoning text.")


class ToolTurn(_TimedTurn):
    """One tool call the agent made, with its result.

    The history equivalent of the live ``tool_started`` and
    ``tool_completed`` events."""

    role: Literal["tool"] = Field(default="tool", description="Turn kind; always `tool`.")
    tool_name: str = Field(default="", description="Internal name of the tool that was called.")
    friendly_name: str = Field(default="", description="Human-readable tool name for display.")
    args: str = Field(default="", description="The call's arguments, as display text.")
    content: str = Field(default="", description="The tool's result text.")
    is_error: bool = Field(default=False, description="`true` when the tool call failed.")
    # Kept from running — see ``ToolCompleted.is_rejected``.
    is_rejected: bool = Field(
        default=False, description="`true` when the user rejected the call, so it never ran."
    )


class PlanTurn(_TimedTurn):
    """A plan the agent proposed in plan mode, shown in place of the
    ``exit_plan_mode`` tool call."""

    # ``state`` uses the empty-string sentinel during the walk — the
    # post-walk pass rewrites it to pending/approved/dismissed. The
    # empty-string default is preserved (rather than ``None``) so the
    # wire dump keeps the same shape clients read today.
    role: Literal["plan"] = Field(default="plan", description="Turn kind; always `plan`.")
    plan: str = Field(default="", description="The plan text (Markdown).")
    tasks: list[WireJson] = Field(
        default_factory=list, description="The task list submitted with the plan."
    )
    state: PlanState = Field(
        default="",
        description=(
            "Whether the user acted on the plan: `pending` (not yet), "
            '`approved`, `dismissed`, or `""` if unknown.'
        ),
    )


class StatsTurn(_RunOwned):
    """Token usage and duration for one agent run, shown at the end of
    the run. Has no ``created_at``."""

    role: Literal["stats"] = Field(default="stats", description="Turn kind; always `stats`.")
    input_tokens: int = Field(default=0, description="Input (prompt) tokens used by the run.")
    output_tokens: int = Field(default=0, description="Output tokens generated by the run.")
    reasoning_tokens: int = Field(
        default=0,
        description="Reasoning tokens generated by the run; `0` if the model reports none.",
    )
    duration: float = Field(default=0.0, description="How long the run took, in seconds.")


class VisualizationTurn(_RunOwned):
    """A visualization card (chart, diagram, extension view) shown in
    the chat during a run.

    Has no ``created_at``; use the neighbouring turns for display
    time."""

    # ``spec`` stays a raw dict — the visualizer subagent owns the JSON
    # schema and we don't validate it here.
    role: Literal["visualization"] = Field(
        default="visualization", description="Turn kind; always `visualization`."
    )
    spec_id: str = Field(default="", description="Unique id of this visualization.")
    spec: dict[str, WireJson] = Field(
        default_factory=dict,
        description="The visualization specification to render, as JSON. Not validated by igni.",
    )
    source_agent: str = Field(
        default="visualizer", description="Name of the agent that produced the visualization."
    )
    title: str = Field(
        default="", description="The card's title, as given by the extension that appended it."
    )
    extension_id: str = Field(
        default="",
        description=(
            "Id of the extension that appended the card; empty when the "
            "built-in visualizer made it. Show the extension's name."
        ),
    )
    seq: int = Field(
        default=0,
        description="Position of the card in the session's event log, for ordering within a run.",
    )


#: Discriminated union of every turn kind the rebuilder can emit.
#: Serialization uses ``role`` as the discriminator so every
#: ``model_dump()`` at the wire boundary lands on the same key-set
#: the FE reads today.
ChatTurn = Annotated[
    UserTurn | AssistantTurn | ThinkingTurn | ToolTurn | PlanTurn | StatsTurn | VisualizationTurn,
    Field(discriminator="role"),
]


class AgnoRunMessageView(BaseModel):
    """Typed view over one persisted Agno run message.

    Built once at the top of the walk via
    ``AgnoRunMessageView.model_validate(msg, from_attributes=True)``
    so the per-message body reads as attribute access instead of
    thirty ``getattr(msg, ...)`` calls.

    Field types mirror Agno's ``Message`` class
    (``agno/models/message.py``): every attribute except ``role``
    and ``content`` is ``Optional`` and defaults to ``None`` on
    non-tool messages. The ``_coerce_none`` validator collapses
    those ``None`` values back to a usable non-None default (``""``
    / ``False`` / ``[]``) so downstream handlers can treat every
    field as populated — matching the tolerance the pre-refactor
    ``getattr(msg, name, default)`` reads gave us.

    ``extra='ignore'`` (not ``'allow'``) — Agno's message struct
    isn't ours to lock down, but we don't need unknown fields to
    ride along on the model either.
    """

    model_config = ConfigDict(extra="ignore")

    role: str = ""
    content: Any = ""
    created_at: int = 0
    from_history: bool = False
    tool_name: str = ""
    tool_call_id: str = ""
    tool_args: Any = None
    tool_call_error: bool = False
    reasoning_content: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator(
        "role",
        "tool_name",
        "tool_call_id",
        "reasoning_content",
        mode="before",
    )
    @classmethod
    def _coerce_str_none(cls, v: Any) -> str:
        return "" if v is None else v

    @field_validator("tool_call_error", "from_history", mode="before")
    @classmethod
    def _coerce_bool_none(cls, v: Any) -> bool:
        return False if v is None else v

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_int_none(cls, v: Any) -> int:
        return 0 if v is None else v

    @field_validator("tool_calls", mode="before")
    @classmethod
    def _coerce_list_none(cls, v: Any) -> list[dict[str, Any]]:
        return [] if v is None else v

    @property
    def content_str(self) -> str:
        """Coerce ``content`` to string with the same guard the old
        code used (``isinstance(content, str)`` else ``str(content or
        "")``). Some Agno providers put a list-of-parts here."""
        c = self.content
        if isinstance(c, str):
            return c
        return str(c or "")


class AgnoRunMetricsView(BaseModel):
    """Typed view over an Agno run's ``metrics`` struct.

    Same tolerance shape as :class:`AgnoRunMessageView` — every
    field defaults to a usable zero so a missing metrics object
    (older Agno versions / test fixtures) can't crash the stats
    line."""

    model_config = ConfigDict(extra="ignore")

    reasoning_tokens: int = 0
    duration: float = 0.0

    @field_validator("reasoning_tokens", mode="before")
    @classmethod
    def _coerce_int_none(cls, v: Any) -> int:
        return 0 if v is None else v

    @field_validator("duration", mode="before")
    @classmethod
    def _coerce_float_none(cls, v: Any) -> float:
        return 0.0 if v is None else v


class AgnoRunView(BaseModel):
    """Typed view over one persisted Agno run.

    Wraps ``run_id`` + ``parent_run_id`` + ``messages`` +
    ``metrics`` access with the same ``getattr(run, name,
    default)`` tolerance the pre-refactor code used. ``messages``
    stays ``list[Any]`` because we validate each entry through
    :class:`AgnoRunMessageView` inside the walker (per-message
    validation gives us a fresh error site if a specific message
    is malformed instead of aborting the whole run at load time).
    """

    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    run_id: str = ""
    parent_run_id: str = ""
    messages: list[Any] = Field(default_factory=list)
    metrics: AgnoRunMetricsView | None = None

    @field_validator("run_id", "parent_run_id", mode="before")
    @classmethod
    def _coerce_str_none(cls, v: Any) -> str:
        return "" if v is None else str(v)

    @field_validator("messages", mode="before")
    @classmethod
    def _coerce_list_none(cls, v: Any) -> list[Any]:
        return [] if v is None else v

    @field_validator("metrics", mode="before")
    @classmethod
    def _coerce_metrics(cls, v: Any) -> Any:
        if v is None:
            return None
        # Accept an Agno metrics struct (attribute access) or a
        # dict — both flow through Pydantic validation.
        if isinstance(v, dict):
            return v
        return AgnoRunMetricsView.model_validate(v, from_attributes=True)


class ChatHistoryEntry(BaseModel):
    """Wire-shape entry for one chat-history turn.

    The pipeline is: :meth:`ChatHistoryRebuilder.rebuild` produces a
    list of :class:`ChatTurn` (a discriminated union); each is
    ``model_dump(mode="json")``ed at the RPC boundary in
    :meth:`BackendServer.get_chat_history`; the resulting dicts are
    the input to :meth:`SessionsController.search_chat` (which
    re-validates them into typed :class:`ChatHistoryEntry` instances
    at the seam and forwards to
    :meth:`ChatHistorySearcher.search`).

    This model is that dict shape typed. It is intentionally
    permissive (every field defaults, ``extra='allow'``) so a
    ``ChatTurn.model_dump()`` from ANY of the seven turn subclasses
    validates cleanly — the discriminator ``role`` is preserved and
    downstream code can still branch on it, while turn-specific
    fields (``tool_name``, ``spec``, …) survive via ``extra='allow'``.

    Using this model as the type of the wire boundary gets us schema
    documentation without narrowing the actual payload — the RPC
    contract stays byte-identical.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    role: str = ""
    run_id: str = ""
    content: str = ""
    created_at: int = 0


# ``history_index`` MUST align with the emission order of
# ChatHistoryRebuilder.rebuild — clients keep a parallel
# ``historyIndex → itemIndex`` map built at session load, so any drift
# breaks the "click result → jump to chat item" mapping.
class ChatSearchHit(BaseModel):
    """One match from searching a session's chat history."""

    history_index: int = Field(
        default=0,
        description=(
            "Index of the matching turn in the list returned by "
            "`get_chat_history`; use it to jump to that turn."
        ),
    )
    role: str = Field(
        default="", description="Kind of the matching turn, e.g. `user`, `assistant` or `tool`."
    )
    run_id: str = Field(default="", description="Id of the agent run the matching turn belongs to.")
    snippet: str = Field(default="", description="Excerpt of the turn's text around the match.")
    match_start: int = Field(
        default=0, description="Start offset of the match within `snippet` (not the full text)."
    )
    match_end: int = Field(
        default=0, description="End offset (exclusive) of the match within `snippet`."
    )
    created_at: int = Field(
        default=0, description="When the turn was created, in Unix epoch seconds; `0` if unknown."
    )


__all__ = [
    "UserTurn",
    "AssistantTurn",
    "ThinkingTurn",
    "ToolTurn",
    "PlanTurn",
    "StatsTurn",
    "VisualizationTurn",
    "ChatTurn",
    "AgnoRunMessageView",
    "AgnoRunView",
    "AgnoRunMetricsView",
    "ChatHistoryEntry",
    "ChatSearchHit",
    "SessionListEntry",
]
