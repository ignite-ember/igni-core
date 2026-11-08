"""The platform's service events, landed (``extensions/service_events.py``).

An event goes to its extension, which posts if it wants the agent to act;
anything else into its thread's chat, or the extension's own
(``docs/CLOUD.md`` §6.1). Its files are fetched as the producer.
"""

from __future__ import annotations

from igni_core.backend.pool_rpcs import ServiceEventRpc
from igni_core.backend.service_event_delivery import ExtensionEventHandler
from igni_core.backend.service_item_desk import HeldPostFlusher, PoolChatDesk
from igni_core.backend.session_orchestrator import SessionOrchestrator
from igni_core.core.db.database import Database
from igni_core.extensions.event_pull import PersonEventPull
from igni_core.extensions.service_attachments import ServerAttachments
from igni_core.extensions.service_events import DeliveredEvents, ServiceEventInbox
from igni_core.extensions.service_landing import Lander
from igni_core.extensions.service_threads import HeldPosts, ServiceThreads
from igni_core.notifications.server_client import SignedInServer


class ServiceEventIngress:
    """The inbox ``deliver_service_events`` fills, the held posts, and the pull."""

    def __init__(
        self,
        *,
        inbox: ServiceEventInbox,
        held_posts: HeldPostFlusher,
        event_pull: PersonEventPull | None,
    ) -> None:
        self._inbox = inbox
        self._held_posts = held_posts
        self._event_pull = event_pull

    @classmethod
    def build(
        cls,
        *,
        producer_db: Database,
        signed_in: SignedInServer,
        orchestrator: SessionOrchestrator,
        fetches_own_events: bool,
    ) -> ServiceEventIngress:
        """``fetches_own_events``: igni pulls the person's events itself while
        online (the desktop); a layer may leave that to its platform (the
        cloud's gateway delivers them)."""
        desk = PoolChatDesk(orchestrator)
        held = HeldPosts(producer_db)
        inbox = ServiceEventInbox(
            delivered=DeliveredEvents(producer_db),
            lander=Lander(
                desk=desk,
                threads=ServiceThreads(producer_db),
                held=held,
                files=ServerAttachments(signed_in),
            ),
            hand_event=ExtensionEventHandler(),
        )
        return cls(
            inbox=inbox,
            held_posts=HeldPostFlusher(held=held, desk=desk),
            event_pull=(
                PersonEventPull(server=signed_in, inbox=inbox) if fetches_own_events else None
            ),
        )

    def rpc(self) -> ServiceEventRpc:
        return ServiceEventRpc(self._inbox)

    def start(self) -> None:
        if self._event_pull is not None:
            self._event_pull.start()
        self._held_posts.start()

    async def stop(self) -> None:
        if self._event_pull is not None:
            await self._event_pull.stop()
        await self._held_posts.stop()
