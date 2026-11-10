"""What the host model needs from igni's backend, and nothing more.

The model is implemented *against the existing backend*: a member that
reads or writes something calls the RPC handler, the UI action or the
service that igni's own UI uses, so the model never holds a second copy
of igni's logic — and a write the person could not make is refused by
the same check that refuses the person.

This protocol is the seam. :class:`~igni_core.backend.host_backend.OrchestratorHostBackend`
implements it over the session pool; the model's tests drive that same
class over a pool of stand-in sessions.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from igni_core.backend.activity import Activity
from igni_core.core.session.client_state import ClientStateStoreProtocol
from igni_core.core.session.event_log_schema import VisualizationDeltaPayload
from igni_core.core.session.schemas import PersistResult
from igni_core.extensions.host.attachments import ChatAttachments
from igni_core.extensions.host.values import SessionAttached
from igni_core.extensions.service_events import UnattendedPost
from igni_core.protocol.messages import Message
from igni_core.protocol.rpc import RpcMethod

if TYPE_CHECKING:
    # The broadcasts package reads this protocol back — its router needs
    # the services, which need this — so naming the view here at runtime
    # closes the circle: importing this module first fails. The name is
    # a type, and only the checker needs it.
    from igni_core.extensions.host.broadcasts.views import ViewShown


class HostBackend(Protocol):
    """igni's backend, as the host model reaches it."""

    #: The project this backend was started in.
    project_dir: Path

    #: Where a window's lasting state is kept — the store that answers
    #: with a result, so a failed write is told rather than swallowed.
    client_state: ClientStateStoreProtocol

    def default_session(self) -> str:
        """The boot session — what a view that binds nothing talks to."""
        ...

    def live_sessions(self) -> list[str]:
        """Sessions with a runtime now."""
        ...

    async def rpc(self, method: RpcMethod, args: dict[str, Any], *, session_id: str) -> Any:
        """Call an RPC handler in a session, as the UI would. JSON back.

        The arguments and the answer are the handler's JSON — the ones
        the window sends and gets — so this seam stays JSON on purpose:
        a member builds its arguments as a typed
        :class:`~igni_core.extensions.host.rpc_wire.base.RpcArgs` model
        and dumps it at the callsite, and reads the answer back into a
        typed model of its own.

        Raises whatever the handler raised: the model turns it into a
        refusal or a failure.
        """
        ...

    async def act(self, message: Message, *, session_id: str) -> None:
        """Send a UI action into a session, as a window would.

        Returns once it is accepted; whatever it streams goes to every
        window, as the person's own action's would.
        """
        ...

    async def ask(self, message: Message, *, session_id: str, timeout: float = 120.0) -> Message:
        """Send a UI action whose answer comes back here, not to the windows."""
        ...

    async def attach(self, session_id: str, project_dir: str | None) -> SessionAttached:
        """``attach_session``: make a session live, refused where the person's would be."""
        ...

    async def post(
        self,
        *,
        session_id: str,
        extension_id: str,
        text: str,
        media: ChatAttachments,
        queued: bool,
    ) -> None:
        """A message in the chat, labelled as ``extension_id``, for the agent.

        ``media`` is what it carries — the host schema's ``attachments``:
        path to content.
        """
        ...

    async def deliver(
        self,
        *,
        session_id: str,
        extension_id: str,
        text: str,
        media: ChatAttachments,
    ) -> UnattendedPost:
        """The same, for a session nobody may be watching: it starts a run
        there, or is queued for the run going — working or waiting on an
        approval — as a service event is (``extensions/service_events.py``)."""
        ...

    async def title(self, *, session_id: str, title: str) -> PersistResult:
        """Name a session now — before its first run, too — so no auto-name
        replaces it, as a service's new chat is titled; every window is told."""
        ...

    async def show_view(
        self, *, session_id: str, payload: VisualizationDeltaPayload, run_id: str
    ) -> None:
        """Put a json-render block in a session's transcript, and keep it.

        ``payload`` is the card itself — igni's ``visualization_delta``,
        labelled with the title and the source the extension gave it.
        """
        ...

    def views(self, session_id: str) -> list[ViewShown]:
        """Every json-render block a session's transcript holds."""
        ...

    def project_dir_of(self, session_id: str) -> Path: ...

    def activity(self) -> Activity:
        """``app.activity``: running, waiting on a person, or idle."""
        ...

    def waiting_approvals(self, session_id: str) -> list[Message]:
        """The ``RunPaused`` of every approval the session has waiting —
        what the host knows after a restart, before any window asked."""
        ...

    def broadcast(self, message: Message) -> None:
        """Send to every window, now (scheduled on the loop)."""
        ...


__all__ = ["HostBackend"]
