"""One bounded sync, for a process that is not the app's backend.

``igni ext sync`` and every ``igni -m`` / ``igni -p`` run. The app's
backend schedules its sync after a window attaches; without this, a
developer working from a terminal — and every headless run — never
received an extension their organisation switched on.

Nothing is started or stopped here: the process that runs extensions
starts what is stored at its own boot. A headless run syncs *before*
it starts them, so the run gets what was just installed.

Never in the way, as :class:`~igni_core.extensions.distribution.sync.ExtensionDistribution`
is not: no server, nobody signed in, an air gap that excludes the
server, igni unreachable, a slow answer — each comes back as
``skipped`` with the reason, and what is installed stays as it is.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import httpx

from igni_core.core.auth.credentials import CredentialsStore
from igni_core.core.config.endpoint import is_configured
from igni_core.core.config.settings import Settings
from igni_core.core.network_policy import NetworkPolicy
from igni_core.extensions.distribution.schemas import SyncReport
from igni_core.extensions.distribution.sync import ExtensionDistribution
from igni_core.extensions.service_token import CredentialsSession, IgniSession
from igni_core.extensions.store import ExtensionStore

logger = logging.getLogger(__name__)


class OneShotSync:
    """One bounded sync for a process that is not the app's backend."""

    def __init__(
        self,
        store: ExtensionStore,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        session: Callable[[], IgniSession | None] | None = None,
    ) -> None:
        """
        Args:
            transport: The transport every request goes through. Set
                by the tests, which answer for igni server themselves.
            session: Where the signed-in session comes from. The
                credentials on disk, unless something else says.
        """
        self._store = store
        self._settings = settings
        self._credentials_file = settings.auth.credentials_file
        self._session = session or CredentialsSession(self._credentials_file)
        self._transport = transport

    @property
    def api_url(self) -> str | None:
        """The igni server, when one is configured."""
        value = self._settings.api_url
        return value if isinstance(value, str) else None

    def why_not(self) -> str | None:
        """Why a sync would not be attempted; ``None`` when it would."""
        api_url = self.api_url
        if not is_configured(api_url):
            return "no igni server is configured (set api_url in your igni settings)"
        assert api_url is not None
        policy = NetworkPolicy.from_settings(self._settings)
        if not policy.allows(api_url):
            return policy.refusal(api_url, what="igni server")
        if self._session() is None:
            loaded = CredentialsStore(self._credentials_file).load()
            if loaded.ok and loaded.creds is not None and loaded.creds.is_expired():
                return f"your sign-in to {api_url} has expired: run 'igni login'"
            return f"nobody is signed in to {api_url}: run 'igni login'"
        return None

    async def run(self, *, timeout: float = 30.0) -> SyncReport:
        """One sync, bounded; a failure is reported, never raised."""
        reason = self.why_not()
        if reason is not None:
            return SyncReport(skipped=reason)
        distribution = ExtensionDistribution(
            self._store,
            api_url=lambda: self.api_url,
            session=self._session,
            transport=self._transport,
        )
        try:
            return await asyncio.wait_for(distribution.sync(), timeout)
        except TimeoutError:
            return SyncReport(skipped=f"igni server did not finish answering within {timeout:g}s")
        except Exception as exc:  # noqa: BLE001 — a sync must not take a run down
            logger.warning("extension sync failed", exc_info=True)
            return SyncReport(skipped=f"the sync failed: {exc}")


__all__ = ["OneShotSync"]
