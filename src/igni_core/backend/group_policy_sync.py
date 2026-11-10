"""Keeps the on-disk group-policy pack in step with igni server.

:class:`GroupPolicySync` owns the three moments a session asks the
server about its group: once at start (:meth:`GroupPolicySync.start`),
on a timer (:meth:`start_polling`), and when a new dialogue opens
(:meth:`revalidate_for_new_dialogue`). Sign-in asks too, through
:meth:`hydrate`, which every one of those funnels into.

It used to live inside ``AuthController``, which is why the comments in
``core/config/group_policy/`` still talk about the fetch happening at
sign-in: it does, but signing in is one caller among four.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from igni_core.backend.group_pack_hydrator import GroupPackHydrator
from igni_core.core.activity import background
from igni_core.core.auth.portal_client import PortalClient

if TYPE_CHECKING:
    from igni_core.core.config.settings import Settings
    from igni_core.core.session import Session

logger = logging.getLogger(__name__)


class GroupPolicySync:
    """Keeps one session's group pack fresh.

    Holds the poll task — the only state — and reads everything else off
    the injected collaborators: the stored token off ``session``, the
    poll interval off ``settings``. The fetch itself is a
    :class:`GroupPackHydrator` over ``portal``, the client the auth
    controller signs in through; when the user picks another server,
    :meth:`use_portal` points it at the new one.
    """

    #: How long a new dialogue waits for the server before giving up and
    #: using the cached pack. Short on purpose: the point of the on-disk
    #: cache is that a session starts without the portal, and blocking
    #: startup on a slow network would undo it.
    NEW_DIALOGUE_TIMEOUT = 3.0

    def __init__(self, session: Session, settings: Settings, portal: PortalClient) -> None:
        self._session = session
        self._settings = settings
        self._hydrator = GroupPackHydrator(settings, portal)
        #: The periodic re-check, if one is running.
        self._poll_task: asyncio.Task | None = None

    def use_portal(self, portal: PortalClient) -> None:
        """Ask ``portal`` from now on — the user chose another server."""
        self._hydrator.use_portal(portal)

    def start(self) -> None:
        """Refresh from a stored token and start the timer, if there is a loop.

        Fire-and-forget — start-up does not block on the fetch; a failed
        cold start just means the next one retries. Without a running
        loop (a bare CLI utility) there is nothing to schedule on, so
        this does nothing.
        """
        token = self._session.cloud_auth.access_token
        if not token:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        background.start(
            self.hydrate(token, force=True),
            holder="group-policy",
            reason="reading the group's policy",
            loop=loop,
        )
        self.start_polling()

    def start_polling(self) -> None:
        """Re-check the group on a timer, if the deployment wants one.

        A session used to learn its group once, at start: an admin who
        moved somebody at nine reached a session opened at eight only
        when it was restarted. Now it asks again every
        ``settings.group_policy.poll_seconds``, and the ask is cheap —
        the server answers 304 with no body when nothing has changed,
        so the usual cost of a poll is a round trip and a hash
        comparison.

        Zero turns it off, for a deployment that would rather its
        machines only check at startup.
        """
        interval = self._settings.group_policy.poll_seconds
        if interval <= 0 or self._poll_task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._poll_task = background.daemon(
            self._poll(interval), name="group-policy-poll", loop=loop
        )

    def stop_polling(self) -> None:
        """Cancel the timer. Idempotent; safe on a loop that has gone."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None

    @property
    def polling(self) -> bool:
        """Whether the timer is running."""
        return self._poll_task is not None

    async def _poll(self, interval: int) -> None:
        """Ask again, forever, until cancelled.

        Every failure mode here is a reason to keep going rather than
        stop: a server restart, a laptop asleep, a token that expired
        and will be replaced by the next login. A polling loop that
        dies on the first error is worse than none, because nothing
        says it stopped.
        """
        while True:
            try:
                await asyncio.sleep(interval)
                token = self._session.cloud_auth.access_token
                if token:
                    await self.hydrate(token)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the loop outlives its errors
                logger.debug("group-policy poll failed: %s", exc)

    async def revalidate_for_new_dialogue(self) -> bool:
        """Ask the server whether the group changed, bounded by a timeout.

        A new dialogue is the moment somebody should pick up an admin's
        change, and it is rare enough to afford a round trip — unlike a
        CLI invocation, which is what the 5-minute age check exists to
        keep quiet. The ETag makes an unchanged pack a 304, so the usual
        cost is one small request.

        **Bounded on purpose.** A session that will not start because
        the portal is slow is a worse failure than one running a
        five-minute-old pack: the on-disk cache exists so sessions work
        offline, and this must not undo that. On timeout or failure the
        cached pack stands and the background poller catches up.
        """
        token = self._session.cloud_auth.access_token
        if not token:
            return False
        try:
            return await asyncio.wait_for(
                self.hydrate(token, force=True), timeout=self.NEW_DIALOGUE_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.info(
                "group-policy revalidation timed out after %ss; using the cached pack",
                self.NEW_DIALOGUE_TIMEOUT,
            )
            return False
        except Exception as exc:  # noqa: BLE001 — a session that starts outranks the update
            logger.warning("group-policy revalidation failed: %s", exc)
            return False

    async def hydrate(self, token: str, *, force: bool = False) -> bool:
        """Refresh the cached group pack; ``True`` when a new one landed.

        The fetch, check and write are :class:`GroupPackHydrator`'s; what
        this adds is the session's half — an admin moving somebody between
        groups should change what they have, not tell them to restart.
        """
        if not await self._hydrator.hydrate(token, force=force):
            return False
        if self._session.reload_group_agents():
            logger.info("group-policy: group agents reloaded")
        return True
