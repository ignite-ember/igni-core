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

    A workflow's lanes run in parallel and share one relay, so this
    one counts them too: the window keeps the questions of every lane
    that is still running, and the drain that carries them lives until
    the last of them is done. A lane that finished used to close the
    relay for its siblings, and the sibling's approval had nowhere to
    go — the window never saw the card, and the call stopped with
    "nobody can answer" (campaign R664).
    """

    def __init__(self, session: Session, push: PushNotificationBridge) -> None:
        self._session = session
        self._push = push
        self._drain: asyncio.Task[None] | None = None
        #: How many agent calls are relaying right now.
        self._holders = 0

    @property
    def can_answer(self) -> bool:
        """Whether somebody is there to answer.

        A headless run marks its session unattended: nobody is at a
        terminal, so the question must not be asked of the air — the
        call is refused instead, as it was before (campaign R664).
        """
        return not self._session.sub_agent_hitl.unattended

    async def open(self) -> None:
        """Start relaying: what the session asks reaches the window.

        One lane of a workflow. The relay stays open for as long as any
        lane is running, so a lane that starts later joins the one that
        is already open instead of opening a second window.
        """
        if not self.can_answer:
            return
        self._holders += 1
        if self._holders > 1:
            return
        self._session.sub_agent_hitl.open_relay()
        self._drain = background.daemon(self._relay(), name="workflow-hitl")

    async def close(self) -> None:
        """Stop relaying, once the last lane that was relaying is done."""
        if self._holders == 0:
            return
        self._holders -= 1
        if self._holders > 0:
            # A sibling lane is still running: its questions still have
            # to reach the window.
            return
        drain, self._drain = self._drain, None
        if drain is not None:
            drain.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drain
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
