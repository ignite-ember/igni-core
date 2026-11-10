"""``/memory`` slash command implementation.

Following the ``cmd_*`` family's pattern:

* All work happens on :class:`MemoryCommand`, a Session-injected
  coordinator with one method per verb. The public
  :func:`cmd_memory` entry point is a two-line shim so
  :mod:`igni_core.backend.command_handler`'s dispatch table stays
  wire-compatible.
* Payload shaping (``arecall`` stores → per-store Pydantic
  section models) lives in :mod:`schemas_memory`. This module
  fetches the recall and decides what reaches the user.

Sub-commands handled here:

* ``/memory`` (no args) — show Learning Machine data (user
  profile / memory / entity memory / session context).
* ``/memory optimize`` — trigger a compaction pass over stored
  memories via :class:`SessionMemoryManager`.

Sibling command files (:mod:`cmd_session`, :mod:`cmd_modes`,
:mod:`cmd_plugin`) remain procedural at the time of this
refactor — that shape is documented tech debt and will be
migrated in follow-up passes. :mod:`cmd_schedule` has since
adopted the coordinator + view-model pattern (see
:class:`~igni_core.backend.cmd_schedule.ScheduleCommand`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from igni_core.backend.command_result import CommandResult
from igni_core.backend.schemas_memory import LearningRecall

if TYPE_CHECKING:
    from igni_core.backend.command_handler import CommandHandler
    from igni_core.core.session import Session

logger = logging.getLogger(__name__)


class MemoryCommand:
    """Coordinator for the ``/memory`` slash-command family.

    Holds a :class:`Session` reference and exposes each verb as
    a bound method. Constructed per invocation so the coordinator
    stays stateless between calls (nothing outlives one
    :meth:`dispatch` call).

    The class accepts a :class:`Session` directly rather than the
    :class:`CommandHandler` state object, so we don't reach into
    ``handler._session`` from inside the coordinator (Rule 6: no
    private-attribute reach-in), as the other ``cmd_*``
    coordinators do.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ── Dispatch ─────────────────────────────────────────────────

    async def dispatch(self, args: str) -> CommandResult:
        """Route a raw arg string to the matching verb method.

        Recognised verbs:

        * ``optimize`` → :meth:`optimize`
        * anything else (including empty args) → :meth:`show`
        """
        subcommand = args.strip().lower()
        if subcommand == "optimize":
            return await self.optimize()
        return await self.show()

    # ── Verb methods ─────────────────────────────────────────────

    async def optimize(self) -> CommandResult:
        """Trigger a compaction pass over stored memories via
        :class:`SessionMemoryManager`."""
        result = await self._session.memory_mgr.optimize()
        if not result.success:
            return CommandResult.error(f"Memory optimization failed: {result.error}")
        return CommandResult.info(result.message)

    async def show(self) -> CommandResult:
        """Render the Learning Machine's cross-session recall
        (user profile / memory / entity memory / session context).

        Recalls with no ``session_id`` so only cross-session stores
        answer. A failed recall is reported as an error, never as
        "no learnings"."""
        learning = self._session.learning_mgr.effective_machine
        if learning is None:
            return CommandResult.info(
                "Learning is not enabled. Set learning.enabled=true in config."
            )
        try:
            stores = await learning.arecall(user_id=self._session.user_id)
        except Exception as exc:
            logger.warning("Learning Machine recall failed: %s", exc)
            return CommandResult.error(f"Could not read learnings: {exc}")
        return LearningRecall.from_stores(stores).to_command_result()


async def cmd_memory(handler: CommandHandler, args: str) -> CommandResult:
    """Handle ``/memory`` commands.

    Two-line shim preserved verbatim so
    :mod:`igni_core.backend.command_handler` keeps importing
    ``cmd_memory`` by name and calling it with ``(self, args)``.
    All real work lives on :class:`MemoryCommand`.
    """
    return await MemoryCommand(handler.session).dispatch(args)
