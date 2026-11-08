"""Plan / todo snapshot builder.

Read-only view over the session's :class:`PlanStore` and
:class:`TodoStore` for the plan-mode panel and the todos panel. Both
results are typed: :class:`LatestPlanResult` for the plan panel and
``list[TodoItemWire]`` for the todos panel.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from igni_core.backend.schemas_plan import LatestPlanResult

if TYPE_CHECKING:
    from igni_core.core.session import Session
    from igni_core.core.tools.todo import TodoItemWire


class PlanSnapshotBuilder:
    """Read-only view of :attr:`Session.plan_store` +
    :attr:`Session.todo_store` for the plan-mode panel and the
    todos panel."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def latest(self) -> LatestPlanResult:
        """Snapshot of the plan store + todos + display state for
        the FE panel.

        ``state`` is ``"pending"`` when a plan exists (the FE
        proves otherwise via ``approve_plan`` / ``dismiss_plan``)
        and empty when no plan has been submitted yet. Never
        inferred from permission mode — a mode flip without an
        explicit user click leaves the plan pending.
        """
        snap = self._session.plan_store.snapshot()
        latest = snap.latest or ""
        return LatestPlanResult(
            latest=latest,
            history=list(snap.history),
            tasks=self._session.todo_store.wires(),
            state="pending" if latest else "",
        )

    def todos(self) -> list[TodoItemWire]:
        """The session's todo list for the todos panel — whatever
        the last ``todo_write`` tool call published. Empty when it
        was never written."""
        return self._session.todo_store.wires()
