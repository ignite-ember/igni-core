"""The per-session stores — the scratch the tools and the run loop write into.

Two groups live here:

* the **loop state** — the ``/loop`` machine and the two Sqlite-backed
  stores its progress goes to;
* the **scratch** — the todo list, the plan store, the memory mirror,
  the output-style selection, the broadcast bus, the reminder queue and
  the session's event log.

None of them knows about the others; the session holds them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from igni_core.core.loop import LoopProgressStore, LoopStore
from igni_core.core.session.broadcast import BroadcastBus
from igni_core.core.session.event_log import SessionEventLog
from igni_core.core.session.loop_ops import LoopController
from igni_core.core.session.output_style_selection import OutputStyleSelection
from igni_core.core.session.reminders import PendingReminderQueue
from igni_core.core.tools.plan import PlanStore
from igni_core.core.tools.todo import TodoStore
from igni_core.core.utils.context_memory import ProjectMemoryBank

if TYPE_CHECKING:
    from igni_core.core.session.core import Session


class StoresBuilder:
    """The stores a session holds — the loop's, and the scratch."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def build_loop_state(self) -> None:
        """The ``/loop`` machine and the stores its progress goes to.

        The controller is the sole owner of the loop's state; the two
        stores beside it are the Sqlite backing it persists to.
        """
        session = self._session
        session.loop_store = LoopStore(project_dir=session.project_dir)
        session.loop_progress_store = LoopProgressStore(project_dir=session.project_dir)
        session.loop = LoopController(session.loop_store)

    def build_scratch(self) -> None:
        """The per-session stores populated by tools and the run loop."""
        session = self._session
        session.todo_store = TodoStore()
        session.plan_store = PlanStore()
        memory_bank = ProjectMemoryBank(session.project_dir)
        memory_bank.ensure()
        # In the cloud the memory directory is a write-through mirror of
        # the database's rows; ``None`` on the desktop, whose files are the store.
        session.memory_mirror = memory_bank.mirror()
        session.output_style = OutputStyleSelection()
        session.broadcast_bus = BroadcastBus()
        session.reminders = PendingReminderQueue()
        # The event log persists through the session's own store, which is
        # only there once the session is past its construction — hence the
        # late lookup rather than a captured value.
        session.event_log_store = SessionEventLog(
            persist_ref=lambda: getattr(session, "persistence", None)
        )


__all__ = ["StoresBuilder"]
