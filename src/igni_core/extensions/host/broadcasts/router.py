"""Routes igni's traffic to the translator that owns each kind of it."""

from __future__ import annotations

import logging

from igni_core.core.approvals.events import ApprovalChange
from igni_core.extensions.host.broadcasts.session_values import DisplayRead
from igni_core.extensions.host.broadcasts.translators import Translators
from igni_core.extensions.host.rpc_write import RpcWrite
from igni_core.extensions.host.services import HostServices
from igni_core.protocol import messages as msg
from igni_core.protocol.schemas.enums import PushChannel

logger = logging.getLogger(__name__)


class BroadcastSignals:
    """Routes igni's traffic to the translator that owns each kind of it."""

    def __init__(self, host: HostServices, *, display: DisplayRead) -> None:
        """
        Args:
            display: Reads ``app.display`` for a session, as
                :meth:`~igni_core.extensions.host.members.app.AppMembers.display_value`
                does, as a :class:`~igni_core.extensions.host.members.answers.Display`.
        """
        self._translators = Translators.of(host, display=display)
        self._activity = host.activity_watch

    def outbound(self, message: msg.Message) -> None:
        """Every message igni sends its windows; a translator's failure is logged, not raised."""
        try:
            self.observe(message)
        except Exception:
            logger.exception("host model: observing %s failed", type(message).__name__)

    def observe(self, m: msg.Message) -> None:
        """One message igni sends its windows."""
        handler = self._translators.handlers.get(type(m))
        if handler is not None:
            handler(m)
        self._activity.after(m)

    def after_rpc(self, write: RpcWrite) -> None:
        """A write succeeded — whoever made it."""
        self._translators.rpc.handle(write)

    def approval_changed(self, change: ApprovalChange) -> None:
        """An approval appeared or went away (``core/approvals/events.py``).

        Told first, then the activity: a listener hears the approvals
        before what igni is doing about them.
        """
        self._translators.approvals.changed(change)
        self._activity.moved()

    def handled(self) -> frozenset[type[msg.Message]]:
        """Every message class translated, for the coverage test."""
        return frozenset(self._translators.handlers)

    def push_channels(self) -> frozenset[PushChannel]:
        """Every push channel translated, for the coverage test."""
        return self._translators.push.channels()


__all__ = ["BroadcastSignals"]
