"""On a person's desktop, igni fetches their services' events itself.

igni server's service-identity §15. In the cloud the gateway pulls a
producer's events and hands them over on its control stream; on the
desktop igni pulls the person's own, with their session, while it is
online — the same shapes and the same two-minute lease:

* ``POST /v1/me/events/pull`` → ``{"events": [...]}``, oldest first, at
  most :attr:`PersonEventPull.PAGE`;
* each goes through the :class:`~igni_core.extensions.service_events.ServiceEventInbox`
  — once, through its extension, into the agent's run;
* ``POST /v1/me/events/ack`` ``{"event_ids": receipt.acknowledge,
  "not_handled": [{event_id, reason}]}`` — delivered now or before, and
  the events no handler took, which their service reads as
  ``not_handled``. One the session could not take is left to be handed
  out again when its lease runs out.

A full page is followed at once by the next; otherwise igni asks again
after :attr:`PersonEventPull.INTERVAL_SECONDS`. Nobody signed in, or no
server: it waits and asks again. An app's token is refused (403): an
app's events reach it through the gateway, so the pull stops.

**One side owns the events.** igni server serves a person's events to
one side at a time (``docs/design/local-cloud-switch.md`` §7): this
desktop, or their igni in the cloud. While the cloud owns them the pull
answers ``409 {"error": "events_owned_elsewhere", "owner": "cloud"}``.
That is no failure: the pull idles, says so once, and asks again after
the interval, so it takes them back as soon as the person switches back.
Acknowledgements are never refused for it, so a delivery already under
way finishes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, Field, ValidationError

from igni_core.core.activity import background
from igni_core.core.activity.http import busy_call
from igni_core.core.auth.server_reachability import record_answered, record_unreachable
from igni_core.extensions.service_events import (
    DeliveryReceipt,
    NotHandled,
    ServiceEvent,
    ServiceEventInbox,
)
from igni_core.extensions.service_token import NotSignedIn
from igni_core.notifications.server_client import SignedInServer

logger = logging.getLogger(__name__)


class PulledEvents(BaseModel):
    events: list[ServiceEvent] = Field(default_factory=list)


class Acknowledgement(BaseModel):
    """``POST /v1/me/events/ack``."""

    event_ids: list[str]
    not_handled: list[NotHandled] = Field(default_factory=list)


class PullRefused(Exception):
    """igni server will not hand this caller events (an app's token: 403)."""


class PullFailed(Exception):
    """igni server could not be reached, or answered in a shape not understood."""


class EventsOwnedElsewhere(Exception):
    """igni server serves this person's events to their other side now (409).

    ``owner`` is the side it names, ``"cloud"`` today.
    """

    def __init__(self, owner: str) -> None:
        super().__init__(f"the events go to the {owner or 'other'} side")
        self.owner = owner


class OwnedElsewhere(BaseModel):
    """The body of the pull's ``409``."""

    error: str
    owner: str = ""


class PersonEventPull:
    """Pulls the signed-in person's events, delivers them, acknowledges what was taken."""

    PULL_PATH: ClassVar[str] = "/v1/me/events/pull"
    ACK_PATH: ClassVar[str] = "/v1/me/events/ack"
    #: igni server hands out at most this many at once.
    PAGE: ClassVar[int] = 100
    #: Between pulls when the last one was not a full page, or the
    #: events go to the person's other side.
    INTERVAL_SECONDS: ClassVar[float] = 60.0
    #: The ``error`` of a ``409`` that means another side owns the events.
    OWNED_ELSEWHERE: ClassVar[str] = "events_owned_elsewhere"
    TIMEOUT_SECONDS: ClassVar[float] = 15.0

    def __init__(
        self,
        *,
        server: SignedInServer,
        inbox: ServiceEventInbox,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._server = server
        self._inbox = inbox
        self._transport = transport
        self._task: asyncio.Future[None] | None = None
        #: Whether the pull is idling because the other side owns the events.
        self._idle = False

    @property
    def idle(self) -> bool:
        """The last pull found the events owned by the person's other side."""
        return self._idle

    async def once(self) -> tuple[int, DeliveryReceipt]:
        """One pull: ``(events pulled, what became of them)``.

        Raises:
            NotSignedIn: nobody is signed in, or no server is configured.
            PullRefused: this caller gets no events.
            EventsOwnedElsewhere: the person's other side gets them now.
            PullFailed: igni server could not be reached.
        """
        answer = await self._call(self.PULL_PATH, {})
        try:
            pulled = PulledEvents.model_validate_json(answer.content)
        except ValidationError as exc:
            raise PullFailed(f"the events are malformed: {exc}") from exc
        receipt = await self._inbox.deliver(pulled.events)
        if receipt.acknowledge:
            await self._call(
                self.ACK_PATH,
                Acknowledgement(
                    event_ids=receipt.acknowledge, not_handled=receipt.not_handled
                ).model_dump(),
            )
        return len(pulled.events), receipt

    def start(self) -> None:
        if self._task is None:
            self._task = background.daemon(self._run(), name="event-pull")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            try:
                pulled, _ = await self.once()
            except PullRefused as exc:
                logger.info("events: igni server hands this caller none (%s); not pulling", exc)
                return
            except EventsOwnedElsewhere as exc:
                if not self._idle:
                    logger.info("events: %s; asking again every %gs", exc, self.INTERVAL_SECONDS)
                self._idle = True
                pulled = 0
            except (NotSignedIn, PullFailed) as exc:
                logger.debug("events: not pulled now: %s", exc)
                pulled = 0
            else:
                if self._idle:
                    logger.info("events: this side gets them again")
                self._idle = False
            if pulled < self.PAGE:
                await asyncio.sleep(self.INTERVAL_SECONDS)

    async def _call(self, path: str, body: dict[str, Any]) -> httpx.Response:
        api_url, session = await self._server.signed_in()
        try:
            async with (
                busy_call("igni-server", "service events"),
                httpx.AsyncClient(
                    timeout=self.TIMEOUT_SECONDS, transport=self._transport
                ) as client,
            ):
                response = await client.post(
                    f"{api_url}{path}",
                    json=body,
                    headers={"Authorization": f"Bearer {session.access_token}"},
                )
        except httpx.HTTPError as exc:
            # The server did not answer: it was not there.
            record_unreachable(api_url, exc.__class__.__name__)
            raise PullFailed(f"igni server could not be reached: {exc}") from exc
        # The server answered. A 5xx is an answer, but a broken one; a
        # refused token is not a reachability problem at all.
        record_answered(api_url, response.status_code)
        if response.status_code == 403:
            raise PullRefused(f"{path} answered 403")
        if response.status_code == 409:
            raise self._conflict(path, response)
        if response.status_code != 200:
            raise PullFailed(f"{path} answered {response.status_code}")
        return response

    @classmethod
    def _conflict(cls, path: str, response: httpx.Response) -> Exception:
        """A ``409``: the events owned elsewhere, or a conflict not understood."""
        try:
            body = OwnedElsewhere.model_validate_json(response.content)
        except ValidationError:
            return PullFailed(f"{path} answered 409")
        if body.error != cls.OWNED_ELSEWHERE:
            return PullFailed(f"{path} answered 409 ({body.error})")
        return EventsOwnedElsewhere(body.owner)


__all__ = [
    "Acknowledgement",
    "EventsOwnedElsewhere",
    "OwnedElsewhere",
    "PersonEventPull",
    "PullFailed",
    "PullRefused",
    "PulledEvents",
]
