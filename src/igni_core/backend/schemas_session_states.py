"""What each chat is doing, and whether it has news the person has not seen.

The sidebar's marks: a chat working, a chat waiting on the person's
answer, a chat that finished while they were elsewhere. Answered by
``list_session_states`` and ``mark_session_seen``, and pushed to every
window on ``session_states_changed`` whenever one of them moves.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from igni_core.backend.activity import ActivityState
from igni_core.backend.rpc_binding import RpcArgs

SessionStatesCause = Literal["activity", "seen"]


class SessionState(BaseModel):
    """One chat's state, and whether it is unread."""

    session_id: str = Field(description="The chat's session id.")
    state: ActivityState = Field(
        default=ActivityState.IDLE,
        description=(
            "What the chat is doing. `running`: a run is working. `waiting`: a run waits on "
            "the person's answer to an approval. `idle`: nothing — also every chat that is "
            "not loaded."
        ),
    )
    waiting_approvals: int = Field(
        default=0, description="How many approvals in the chat wait on the person."
    )
    unread: bool = Field(
        default=False,
        description=(
            "True when a run in the chat finished after the person last looked at it "
            "(`mark_session_seen`). Read state is the person's, across windows and devices."
        ),
    )
    last_message_at: int = Field(
        default=0,
        description=(
            "When a message last went into the chat or a reply came out, in Unix "
            "milliseconds; `0` when igni has not recorded one. The sidebar lists chats by it, "
            "newest first."
        ),
    )


class SessionStates(BaseModel):
    """Every chat that is doing something or is unread; any other chat is idle and read."""

    sessions: list[SessionState] = Field(
        default_factory=list,
        description="The chats that are running, waiting or unread, each once.",
    )
    cause: SessionStatesCause = Field(
        default="activity",
        description=(
            "Why this was sent. `activity`: a run started, finished, paused or resumed, or "
            "the states were asked for. `seen`: a `mark_session_seen` call."
        ),
    )


class MarkSessionSeenArgs(RpcArgs):
    """Arguments for marking a chat as seen."""

    session_id: str = Field(min_length=1, description="The chat the person looked at.")


__all__ = ["MarkSessionSeenArgs", "SessionState", "SessionStates", "SessionStatesCause"]
