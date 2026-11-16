"""Relays a workflow's approval questions to the window that owns the session.

A workflow started from the composer has no chat run of its own: the
run is an RPC that returns at once, and its agent calls happen later,
with no stream to carry a pause on. Nothing was relaying the
session's questions to a person, so the workflow's agent call asked and
the approval channel answered that nobody could answer it (campaign
R664).

This opens the session's HITL relay for the length of the call and
surfaces what is asked to the window, which draws the same card the
chat draws. The answer comes back the way it always does — the person
answers with ``hitl_response``, and :meth:`HitlController.resolve_batch`
hands it to the coordinator, which is what the waiting call is on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from igni_core.backend.schemas_pause import SubagentPause
from igni_core.core.activity import background

if TYPE_CHECKING:
    from igni_core.backend.push_bridge import PushNotificationBridge
    from igni_core.core.session.core import Session

logger = logging.getLogger(__name__)


class WorkflowApprovalRelay:
    """One workflow's window onto the session's questions.

    Opened for as long as the workflow's agent call runs, closed after.
    The session's relay is a reference count, so the chat's own
    multiplexer keeps its share while this one is open.
    """

    def __init__(self, session: Session, push: PushNotificationBridge) -> None:
        self._session = session
        self._push = push
        self._drain: asyncio.Task[None] | None = None
        self._opened = False

    @property
    def can_answer(self) -> bool:
        """Whether somebody is there to answer.

        A headless run marks its session unattended: nobody is at a
        terminal, so the question must not be asked of the air — the
        call is refused instead, as it was before (campaign R664).
        """
        return not self._session.sub_agent_hitl.unattended

    async def open(self) -> None:
        """Start relaying: what the session asks reaches the window."""
        if self._opened or not self.can_answer:
            return
        self._opened = True
        self._session.sub_agent_hitl.open_relay()
        self._drain = background.daemon(self._relay(), name="workflow-hitl")

    async def close(self) -> None:
        """Stop relaying, and let go of the session's relay."""
        drain, self._drain = self._drain, None
        if drain is not None:
            drain.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drain
        if self._opened:
            self._opened = False
            self._session.sub_agent_hitl.close_relay()

    async def _relay(self) -> None:
        """Surface each new question to the window.

        The questions are taken from the coordinator, which marks them
        shown: the chat's own multiplexer may take them first, and then
        they go out on the chat stream instead — the person sees the
        same card either way.
        """
        hitl = self._session.sub_agent_hitl
        while True:
            await hitl.new_arrival.wait()
            entries = hitl.list_new_pending()
            if entries:
                self._push.run_paused(SubagentPause(entries=entries).run_paused())
            for update in hitl.take_detail_updates():
                self._push.requirement_details(update)


__all__ = ["WorkflowApprovalRelay"]
