"""The real backend with one known bug put back in: how the matrix proves it can see bugs."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncGenerator, Callable
from typing import Any

from agno.run.base import HISTORY_SKIP_STATUSES, RunStatus

from igni_core.backend.__main__ import main as backend_main
from igni_core.backend.message_dispatcher import MessageDispatcher
from igni_core.backend.run_controller import RunController
from igni_core.protocol import messages as msg


class Mutants:
    """The bug shapes, by name."""

    @staticmethod
    def stop_ignored() -> None:
        """Escape and Stop reach the backend and do nothing."""
        RunController.cancel_run = lambda self: False  # type: ignore[method-assign]

    @staticmethod
    def parallel_follow_up() -> None:
        """6fa3e09e's shape: a follow-up runs beside the turn in flight, blind to it."""
        original = RunController.run_message

        async def unlocked(self: RunController, text: str, media: Any) -> AsyncGenerator[Any, None]:
            self._run_lock = asyncio.Lock()  # noqa: SLF001 — the bug is exactly this
            async for proto in original(self, text, media):
                yield proto

        async def run_at_once(self: MessageDispatcher, message: msg.QueueMessage) -> None:
            await self._transport.send(  # noqa: SLF001
                msg.UserMessageReceived(text=message.text, client_id=message.client_id, queued=True)
            )
            await self._run_turn(message.text, {}, "")  # noqa: SLF001

        RunController.run_message = unlocked  # type: ignore[method-assign]
        MessageDispatcher._on_queue_message = run_at_once  # type: ignore[method-assign]  # noqa: SLF001

    @staticmethod
    def history_forgets() -> None:
        """Every earlier turn is left out of what the model is shown."""
        HISTORY_SKIP_STATUSES.append(RunStatus.completed)

    @classmethod
    def by_name(cls) -> dict[str, Callable[[], None]]:
        return {
            "stop_ignored": cls.stop_ignored,
            "parallel_follow_up": cls.parallel_follow_up,
            "history_forgets": cls.history_forgets,
        }


if __name__ == "__main__":
    Mutants.by_name()[sys.argv[1]]()
    backend_main(sys.argv[2:])
