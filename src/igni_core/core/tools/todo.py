"""TodoWrite — agent-facing planning tool, Claude Code parity.

The agent uses ``todo_write`` to maintain a per-session list of
upcoming / in-progress / completed work items. Each call REPLACES
the entire list (atomic snapshot semantics — matches CC's
``TodoWrite``), so the agent ships the full intended state on
every call instead of issuing per-item updates.

A tool at all (vs. having the agent narrate its plan): the user gets
a stable view of intent that survives intermediate tool calls, the
list is consumable by external surfaces (a webview sidebar, the TUI
status line) via the ``GET_TODOS`` RPC without parsing prose, and
the model is steered to **commit** to a plan and tick it off.

The single rule the model needs to follow: keep at most one item
in ``in_progress`` at a time, and flip a completed item before
starting the next. That property isn't enforced here (we record
whatever the model writes) — it's encouraged by the docstring and
surfaced in the tool's reply summary so the model can self-correct.

Shapes are typed end to end: the agent's loose rows become
:class:`TodoItemInput` models, the rows that survive validation
become :class:`TodoItemWire` rows, and those rows are the only
shape the store, the broadcast, the RPC and the persistence layer
carry. There is one set of row rules —
:meth:`TodoItemInput.validate_batch` — and every boundary uses it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any, Literal, cast, get_args

from agno.tools import Toolkit
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from igni_core.core.broadcast_schema import BroadcastEvent

if TYPE_CHECKING:
    from igni_core.core.session.core import Session
    from igni_core.core.session.schemas import PersistResult

logger = logging.getLogger(__name__)


TodoStatus = Literal["pending", "in_progress", "completed"]

#: The statuses a row may carry — derived from :data:`TodoStatus`
#: itself, so there is no second list of valid values to keep in
#: step with the type.
VALID_STATUSES: frozenset[str] = frozenset(get_args(TodoStatus))


# The canonical todo row: the one shape every boundary carries. The
# camelCase alias keeps Claude Code parity: every model_dump() emits
# ``activeForm``.
class TodoItemWire(BaseModel):
    """One item in the agent's todo checklist."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    content: str = Field(description="What the item is, in imperative form, e.g. `Run tests`.")
    status: TodoStatus = Field(
        description="Progress of the item: `pending` (not started), `in_progress` (being "
        "worked on now) or `completed`."
    )
    active_form: str = Field(
        default="",
        alias="activeForm",
        description="Present-tense form shown while the item is in progress, e.g. "
        "`Running tests`. Empty when the agent gave none.",
    )

    @classmethod
    def parse_snapshot(cls, raw: object) -> list[TodoItemWire]:
        """Best-effort read of a persisted snapshot.

        A stale or corrupt row on disk must NOT sink the whole read
        — the malformed rows drop and the valid ones come back.
        """
        if not isinstance(raw, list):
            return []
        return TodoItemInput.validate_batch(raw).items


class TodoItemInput(BaseModel):
    """One row as the agent sent it — the tolerant input shape.

    The agent's rows are loose JSON: camelCase or snake_case keys,
    a number where a string belongs, occasionally a bare string
    where a row was expected. This model absorbs that without
    failing the whole tool call; :meth:`validate_batch` then
    applies the one set of rules.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    content: str = Field(
        default="", description="Imperative description of the work, e.g. 'Run tests'."
    )
    status: str = Field(
        default="", description="One of: pending, in_progress, completed (default pending)."
    )
    active_form: str = Field(
        default="", alias="activeForm", description="Gerund shown while in progress."
    )

    #: Set when the row was not an object at all — not a validation
    #: error yet, but a row the batch validator has to name.
    _not_a_row: bool = PrivateAttr(default=False)

    @model_validator(mode="wrap")
    @classmethod
    def _absorb(cls, raw: Any, handler: Any) -> TodoItemInput:
        """Take the row as it came.

        A row that is not an object at all travels on as an empty,
        flagged row instead of raising inside the agent's tool call.
        Everything else is normalised to strings here (numbers
        become their string form, values are stripped).
        """
        if not isinstance(raw, dict):
            row = cls.model_construct(content="", status="", active_form="")
            row._not_a_row = True
            return row
        data = dict(raw)
        for key in ("content", "status", "activeForm", "active_form"):
            if key in data:
                value = data[key]
                data[key] = "" if value is None else str(value).strip()
        return handler(data)

    @classmethod
    def validate_batch(cls, rows: Iterable[Any] | None) -> TodoBatch:
        """Validate a batch of todo rows.

        Returns the rows that can be applied as typed
        :class:`TodoItemWire` rows plus one error message per row
        that could not be used. The caller applies the good rows
        and surfaces the errors so the agent can correct on the
        next call.

        The row index in an error message uses the ``todos[i]``
        prefix the plan pipeline uses — the same row naming in both
        replies.
        """
        if rows is None:
            return TodoBatch()
        if not isinstance(rows, (list, tuple)):
            return TodoBatch(errors=["todos must be a list"])
        items: list[TodoItemWire] = []
        errors: list[str] = []
        for index, entry in enumerate(rows):
            row = entry if isinstance(entry, cls) else cls.model_validate(entry)
            if row._not_a_row:
                errors.append(f"todos[{index}]: not a dict")
                continue
            if not row.content:
                errors.append(f"todos[{index}]: empty content")
                continue
            # An absent status means "pending"; a status the agent
            # spelled out is taken at its word (and lower-cased).
            status = (row.status if "status" in row.model_fields_set else "pending").lower()
            if status not in VALID_STATUSES:
                errors.append(f"todos[{index}]: status {status!r} not in {sorted(VALID_STATUSES)}")
                continue
            items.append(
                TodoItemWire(
                    content=row.content,
                    status=cast(TodoStatus, status),
                    active_form=row.active_form,
                )
            )
        return TodoBatch(items=items, errors=errors)


class TodoBatch(BaseModel):
    """The outcome of validating a batch of todo rows: the rows that
    can be applied, and one error message per rejected row."""

    model_config = ConfigDict(frozen=True)

    items: list[TodoItemWire] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class TodosUpdatedEvent(BroadcastEvent):
    """The agent's todo checklist changed; carries the whole new list."""

    channel: Literal["todos_updated"] = "todos_updated"
    todos: list[TodoItemWire] = Field(
        description="The full checklist, in order. Replaces any list received before; empty "
        "when the agent cleared it."
    )


class TodoStore:
    """Per-session todo list — the rows the agent last published.

    Every reader goes through :attr:`items`, so there is one place
    the rows change.
    """

    def __init__(self) -> None:
        self._items: list[TodoItemWire] = []

    @property
    def items(self) -> list[TodoItemWire]:
        """The current rows — a copy, so a reader cannot mutate the
        store behind its back."""
        return list(self._items)

    def set(self, items: Sequence[TodoItemWire]) -> None:
        """Replace the list atomically — the contract is "the list
        IS now this". No partial merge, no delta detection."""
        self._items = list(items)


class TodoTools(Toolkit):
    """Single-method toolkit: ``todo_write``."""

    def __init__(self, session: Session) -> None:
        super().__init__(name="ember_todo")
        self._session = session
        self.register(self.todo_write)

    def _broadcast_state(self) -> None:
        """Push the current rows on the ``todos_updated`` channel so
        attached clients re-render with the latest statuses. A
        session without a wired transport has no subscribers."""
        self._session.broadcast(TodosUpdatedEvent(todos=self._session.todo_store.items))

    async def _persist_state(self) -> PersistResult:
        """Write the current rows to ``session_data["todos"]`` so the
        live execution state (in_progress / completed flips) survives
        a BE restart.

        Without this, rehydration falls back to the plan's original
        ``exit_plan_mode(tasks=...)`` list — everything pending, all
        progress erased. The envelope carries a failed write instead
        of swallowing it.
        """
        return await self._session.persistence.save_todos(self._session.todo_store.items)

    async def todo_write(self, todos: list[TodoItemInput]) -> str:
        """Replace the session's todo list with ``todos``.

        Each row carries three fields:

        * ``content`` — imperative description (e.g. ``"Run
          tests"``). Required, non-empty.
        * ``status`` — ``"pending"``, ``"in_progress"``, or
          ``"completed"``. Defaults to ``pending``.
        * ``activeForm`` — verb-noun gerund to display while the
          item is in progress (e.g. ``"Running tests"``).
          Optional.

        The list is replaced atomically — pass the FULL intended
        state on every call. Mark items ``completed`` as soon as
        the work is done, not in batches at the end; keep at most
        one item ``in_progress``.

        Returns a short summary so the model gets immediate
        feedback ("3 todos: 1 completed, 1 in_progress, 1 pending")
        and surfaces any validation errors from the payload.
        """
        batch = TodoItemInput.validate_batch(todos)
        items = batch.items
        errors = batch.errors
        persist: PersistResult | None = None
        if not errors and not items:
            # Empty list IS valid (clears the plan) but we want
            # a positive confirmation so the model knows the call
            # took effect.
            self._session.todo_store.set([])
            self._broadcast_state()
            persist = await self._persist_state()
            return self._with_persist_note("Cleared todo list.", persist)
        if items:
            self._session.todo_store.set(items)
            self._broadcast_state()
            persist = await self._persist_state()
        counts = {status: 0 for status in VALID_STATUSES}
        for item in items:
            counts[item.status] += 1
        msg = (
            f"{len(items)} todos: "
            f"{counts['completed']} completed, "
            f"{counts['in_progress']} in_progress, "
            f"{counts['pending']} pending."
        )
        if errors:
            joined = "; ".join(errors)
            msg += f" Validation errors (ignored): {joined}"
        if counts["in_progress"] > 1:
            msg += (
                " Note: keep at most one item in_progress at a time —"
                " mark the previous in_progress item completed before"
                " starting another."
            )
        return self._with_persist_note(msg, persist)

    @staticmethod
    def _with_persist_note(msg: str, persist: PersistResult | None) -> str:
        """Surface a failed snapshot write instead of swallowing it.

        The in-memory state and the broadcast still reach the
        attached clients — but the agent is told the snapshot did
        not land, so it does not assume the restart keeps its
        progress.
        """
        if persist is None or persist.ok:
            return msg
        logger.error("todo persist failed: %s", persist.error)
        return f"{msg} Warning: the todo snapshot was not persisted ({persist.error})."
