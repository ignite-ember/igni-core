"""What igni's window would show for one session, derived from the frames it received."""

from __future__ import annotations

from typing import Literal

from tests.session_lifecycle.schemas.pending_approval import PendingApproval
from tests.session_lifecycle.schemas.wire_frame import WireFrame

RunPhase = Literal[
    "idle",
    "starting",
    "streaming",
    "finalizing",
    "awaiting_approval",
    "cancelled",
    "errored",
    "done",
]

# The phases in which the app shows its spinner and arms Stop (``ACTIVE_PHASES``).
ACTIVE_PHASES: frozenset[str] = frozenset(
    {"starting", "streaming", "finalizing", "awaiting_approval"}
)
# The phases in which the composer queues a message instead of sending it (``isProcessing``).
PROCESSING_PHASES: frozenset[str] = frozenset({"starting", "streaming"})


class ClientView:
    """The window's state for one session: phase, approvals on screen, sidebar mark."""

    def __init__(self, session_id: str = "") -> None:
        self.session_id = session_id
        self.phase: RunPhase = "idle"
        self.pending: list[PendingApproval] = []
        # The sidebar's mark for this session: ``idle``, ``running`` or ``waiting``.
        self.sidebar_state = "idle"
        self.sidebar_waiting = 0
        self.sidebar_unread = False

    @property
    def stop_armed(self) -> bool:
        """Whether the app shows Stop / the spinner for this session."""
        return self.phase in ACTIVE_PHASES

    @property
    def queues_messages(self) -> bool:
        """Whether a message typed now would be queued rather than sent."""
        return self.phase in PROCESSING_PHASES

    def sent(self) -> None:
        self.phase = "starting"

    def cancelled(self) -> None:
        self.phase = "cancelled"
        self.pending = []

    def rebind(self, session_id: str) -> None:
        """A window that moved to another session starts clean, as the app's does."""
        self.session_id = session_id
        self.phase = "idle"
        self.pending = []

    def apply(self, frame: WireFrame) -> None:
        if frame.type == "push_notification":
            self._apply_push(frame)
            return
        if frame.session_id and self.session_id and frame.session_id != self.session_id:
            return
        kind = frame.type
        if (
            kind == "run_started"
            and frame.top_level
            or kind == "content_delta"
            and self.phase == "starting"
        ):
            self.phase = "streaming"
        elif kind == "streaming_done" and frame.top_level and self.phase == "streaming":
            self.phase = "finalizing"
        elif kind == "run_paused":
            self.phase = "awaiting_approval"
            for req in frame.data.get("requirements") or []:
                rid = str(req.get("requirement_id", ""))
                if rid and all(p.requirement_id != rid for p in self.pending):
                    self.pending.append(
                        PendingApproval(
                            requirement_id=rid,
                            tool_name=str(req.get("tool_name", "")),
                            run_id=frame.run_id,
                        )
                    )
        elif kind == "requirement_resolved":
            rid = str(frame.data.get("requirement_id", ""))
            self.pending = [p for p in self.pending if p.requirement_id != rid]
        elif kind == "run_completed" and frame.top_level:
            if self.phase not in ("cancelled", "errored"):
                self.phase = "done"
        elif kind in ("run_error", "error"):
            self.phase = "errored"
        elif kind == "stream_end" and self.phase in ("starting", "streaming", "finalizing"):
            self.phase = "done"

    def _apply_push(self, frame: WireFrame) -> None:
        if frame.channel != "session_states_changed":
            return
        payload = frame.data.get("payload") or {}
        listed = {str(s.get("session_id")): s for s in payload.get("sessions") or []}
        mine = listed.get(self.session_id)
        if payload.get("cause") == "seen" and mine is None:
            return
        if mine is None:
            self.sidebar_state, self.sidebar_waiting, self.sidebar_unread = "idle", 0, False
            return
        self.sidebar_state = str(mine.get("state", "idle"))
        self.sidebar_waiting = int(mine.get("waiting_approvals") or 0)
        self.sidebar_unread = bool(mine.get("unread"))
