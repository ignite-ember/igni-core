"""Tells every window which chats are working, waiting or unread, as that moves.

Fed by ``app.activity`` (:class:`~igni_core.extensions.host.activity.ActivityWatch`):
a chat that goes from idle to running or waiting has had a message go in;
one that goes back to idle has finished a run, its reply the last message.
Both are recorded (:class:`~igni_core.core.session.session_reads.SessionReadStore`)
so the chat stays unread until the person looks at it, and the sidebar
lists the newest conversation first — after a restart, and on another
device, too.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from igni_core.backend.activity import Activity, ActivityState
from igni_core.backend.schemas_session_states import (
    SessionState,
    SessionStates,
    SessionStatesCause,
)
from igni_core.core.session.session_reads import SessionReadStore
from igni_core.protocol import messages as msg
from igni_core.protocol.schemas.enums import PushChannel

_BUSY = frozenset({ActivityState.RUNNING, ActivityState.WAITING})


class SessionStateWatch:
    """Records finished runs and looks, and pushes ``session_states_changed``."""

    def __init__(
        self,
        *,
        reads: SessionReadStore,
        activity: Callable[[], Activity],
        project_dir_of: Callable[[str], Path],
        broadcast: Callable[[msg.Message], None],
    ) -> None:
        self._reads = reads
        self._activity = activity
        self._project_dir_of = project_dir_of
        self._broadcast = broadcast
        self._last: dict[str, ActivityState] = {}
        self._told: SessionStates | None = None

    def activity_moved(self, activity: Activity) -> None:
        """``app.activity`` moved: record the runs that finished, then tell the windows."""
        now = {s.session_id: s.state for s in activity.sessions}
        for session_id, state in now.items():
            before = self._last.get(session_id)
            if state is ActivityState.IDLE and before in _BUSY:
                self._reads.finished(session_id, self._project_dir_of(session_id))
            elif state in _BUSY and before not in _BUSY:
                self._reads.message(session_id, self._project_dir_of(session_id))
        self._last = now
        states = self._states(activity, "activity")
        # A busy lease moves the activity too, with no chat changing.
        if states != self._told:
            self._push(states)

    def mark_seen(self, session_id: str) -> SessionStates:
        """The person looked at ``session_id``; every window is told."""
        self._reads.seen(session_id, self._project_dir_of(session_id))
        states = self._states(self._activity(), "seen")
        self._push(states)
        return states

    def states(self) -> SessionStates:
        """Every chat running, waiting or unread, now."""
        return self._states(self._activity(), "activity")

    def _states(self, activity: Activity, cause: SessionStatesCause) -> SessionStates:
        marks = {row.session_id: row for row in self._reads.rows()}
        rows: list[SessionState] = []
        for s in activity.sessions:
            mark = marks.pop(s.session_id, None)
            unread = mark is not None and mark.unread
            if s.state is ActivityState.IDLE and not unread:
                continue
            rows.append(
                SessionState(
                    session_id=s.session_id,
                    state=s.state,
                    waiting_approvals=s.waiting_approvals,
                    unread=unread,
                    last_message_at=mark.last_message_at if mark is not None else 0,
                )
            )
        rows += [
            SessionState(session_id=sid, unread=True, last_message_at=mark.last_message_at)
            for sid, mark in sorted(marks.items())
            if mark.unread
        ]
        return SessionStates(sessions=rows, cause=cause)

    def _push(self, states: SessionStates) -> None:
        self._told = states.model_copy(update={"cause": "activity"})
        self._broadcast(
            msg.PushNotification(
                channel=PushChannel.SESSION_STATES_CHANGED.value,
                payload=states.model_dump(mode="json"),
            )
        )


__all__ = ["SessionStateWatch"]
