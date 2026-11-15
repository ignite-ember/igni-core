"""``PermissionRequest`` / ``PermissionDenied`` for the calls the stage pipeline never sees.

The tool-event hook's stages fire both events, but they run only once a
call is about to execute. Most approvals are settled before that, at
Agno's ``requires_confirmation`` pause: the backend's pause handler and
the headless pause policy answer what the evaluator decided, and put the
rest to the person. Those calls fired neither event — not when a person
was asked, not when a person declined, not when ``--read-only`` or a
deny rule refused at the pause (HOOKS.md, "Permission events").
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from igni_core.core.activity import background
from igni_core.core.hooks.events import HookEvent
from igni_core.core.hooks.executor import HookExecutor
from igni_core.core.hooks.hook_firer import HookFirer
from igni_core.core.hooks.tool_events import (
    PermissionDeniedPayload,
    PermissionRequestPayload,
)

logger = logging.getLogger(__name__)


class PermissionNotices:
    """Fires the two permission events for a paused call."""

    #: ``PermissionDenied.reason`` when the evaluator refused at the pause.
    BY_EVALUATOR = "permission_evaluator"
    #: ``PermissionDenied.reason`` when the person declined in the dialog.
    BY_PERSON = "person"

    def __init__(
        self,
        executor_ref: Callable[[], HookExecutor | None],
        session_id_ref: Callable[[], str],
    ) -> None:
        self._executor_ref = executor_ref
        self._session_id_ref = session_id_ref

    async def requested(self, tool_name: str, tool_args: dict[str, Any]) -> None:
        """The call is about to be put to the person."""
        firer = self._firer()
        if firer is not None:
            await firer.fire(
                HookEvent.PERMISSION_REQUEST,
                tool_name,
                PermissionRequestPayload.from_call(tool_name, tool_args),
            )

    async def denied(self, tool_name: str, tool_args: dict[str, Any], reason: str) -> None:
        """The call was refused, and will not run."""
        firer = self._firer()
        if firer is not None:
            await firer.fire(
                HookEvent.PERMISSION_DENIED,
                tool_name,
                PermissionDeniedPayload.from_call(tool_name, tool_args, reason),
            )

    def requested_later(self, tool_name: str, tool_args: dict[str, Any]) -> None:
        """:meth:`requested`, from code that cannot wait for the hooks."""
        self._later(self.requested(tool_name, tool_args), HookEvent.PERMISSION_REQUEST)

    def denied_later(self, tool_name: str, tool_args: dict[str, Any], reason: str) -> None:
        """:meth:`denied`, from code that cannot wait for the hooks."""
        self._later(self.denied(tool_name, tool_args, reason), HookEvent.PERMISSION_DENIED)

    def _later(self, work: Any, event: HookEvent) -> None:
        try:
            background.start(work, holder="hook", reason=event.value)
        except RuntimeError:
            # No running loop — a synchronous caller in a test; nothing to fire on.
            work.close()
            logger.debug("no event loop to fire %s on", event.value)

    def _firer(self) -> HookFirer | None:
        executor = self._executor_ref()
        if executor is None:
            return None
        return HookFirer(executor, self._session_id_ref())


__all__ = ["PermissionNotices"]
