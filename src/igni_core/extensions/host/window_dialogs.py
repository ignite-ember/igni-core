"""The dialogs on screen, and the answers waiting for them.

One dialog at a time: the one already open closes unanswered (``None``),
as if dismissed. Deterministic, and nobody waits on a dialog that can no
longer be answered.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from igni_core.extensions.host.window_values import Dialog


class DialogBook:
    """The dialog each window has open, and the answer waiting for it."""

    def __init__(self) -> None:
        self._open: dict[str, Dialog] = {}
        self._answers: dict[str, asyncio.Future[Any]] = {}

    def open(
        self, window_id: str, spec: Any, *, source: str | None = None
    ) -> tuple[Dialog, asyncio.Future[Any]]:
        """The dialog to show, and the future its answer comes on."""
        dialog = Dialog(id=f"d-{uuid.uuid4().hex[:12]}", spec=spec, source=source)
        answer = asyncio.get_running_loop().create_future()
        self._open[window_id] = dialog
        self._answers[dialog.id] = answer
        return dialog, answer

    def close(self, window_id: str, dialog_id: str, answer: Any) -> Dialog | None:
        """Answer the open dialog: the dialog that closed, or ``None`` if
        that one is not the one open."""
        dialog = self._open.get(window_id)
        if dialog is None or dialog.id != dialog_id:
            return None
        del self._open[window_id]
        future = self._answers.pop(dialog_id, None)
        if future is not None and not future.done():
            future.set_result(answer)
        return dialog

    def cancel(self) -> None:
        """Shutting down: nobody will answer."""
        for future in self._answers.values():
            if not future.done():
                future.set_result(None)
        self._answers.clear()


__all__ = ["DialogBook"]
