"""``list_session_states`` and ``mark_session_seen``: the sidebar's marks.

Pool-level: the sidebar lists every chat, whichever session a window is
attached to, and what the person has seen is theirs, not a session's.
"""

from __future__ import annotations

from igni_core.backend.pool_rpcs.base import PoolRpc, UnavailableRpc
from igni_core.backend.rpc_binding import Correlation, NoArgs, rpc
from igni_core.backend.schemas_session_states import MarkSessionSeenArgs, SessionStates
from igni_core.backend.session_state_watch import SessionStateWatch
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcDuration, RpcMethod, RpcScope
from igni_core.protocol.schemas.enums import PushChannel


class SessionStatesRpc(PoolRpc):
    """Answers which chats are running, waiting or unread, and marks a chat seen."""

    NOT_WATCHED = "session states: nothing follows the chats' activity in this igni"

    def __init__(self, watch: SessionStateWatch) -> None:
        self._watch = watch

    @classmethod
    def unmounted(cls) -> list[UnavailableRpc]:
        return [
            UnavailableRpc(RpcMethod.LIST_SESSION_STATES, cls.NOT_WATCHED),
            UnavailableRpc(RpcMethod.MARK_SESSION_SEEN, cls.NOT_WATCHED),
        ]

    @rpc(
        RpcMethod.LIST_SESSION_STATES,
        duration=RpcDuration.IO,
        args=NoArgs,
        scope=RpcScope.IGNI,
    )
    def list_states(self, args: NoArgs, request: msg.RPCRequest) -> SessionStates:
        """List the chats that are running, waiting on the person, or unread.

        Takes no arguments. Returns ``sessions``: each chat that is doing
        something or has a finished run the person has not seen, with its
        ``state`` (``running``, ``waiting`` or ``idle``), how many approvals
        wait in it, and ``unread``. A chat not listed is idle and read.
        ``session_states_changed`` pushes the same shape whenever it moves.
        """
        return self._watch.states()

    @rpc(
        RpcMethod.MARK_SESSION_SEEN,
        correlate=Correlation(by="payload.cause", equals="seen"),
        duration=RpcDuration.IO,
        args=MarkSessionSeenArgs,
        scope=RpcScope.IGNI,
        streams=(PushChannel.SESSION_STATES_CHANGED,),
    )
    def mark_seen(self, args: MarkSessionSeenArgs, request: msg.RPCRequest) -> SessionStates:
        """Mark a chat as seen, so it is no longer unread.

        ``session_id`` is the chat the person looked at. Its runs that
        finished until now are read, wherever the person looks next; a run
        that finishes later makes it unread again. Returns the chats'
        states afterwards, also pushed to every window as
        ``session_states_changed``. An unknown id is recorded all the same.
        """
        return self._watch.mark_seen(args.session_id)


__all__ = ["SessionStatesRpc"]
