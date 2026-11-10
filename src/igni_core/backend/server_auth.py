"""Cloud auth RPCs — login, credential reload/clear, server choice.

Exposes a single class, :class:`AuthController`:

* :meth:`AuthController.login` — browser-callback OAuth flow.
* :meth:`AuthController.reload_cloud_credentials` — refresh
  :class:`CloudCredentials` on the session (post-login) and rebuild
  the main team.
* :meth:`AuthController.clear_cloud_credentials` — logout inverse.
* :meth:`AuthController.discover_server` / :meth:`AuthController.set_server`
  — which server this install talks to.

Keeping the group-policy pack current is
:class:`~igni_core.backend.group_policy_sync.GroupPolicySync`'s job; this
controller only asks it to refresh after a sign-in, and points it at a
newly chosen server.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from igni_core.backend.browser_sign_in import BrowserSignIn, StatusCallback
from igni_core.backend.cloud_sign_out import switch_off_cloud
from igni_core.backend.group_policy_sync import GroupPolicySync
from igni_core.backend.schemas_auth import ServerChoice, ServerFound
from igni_core.backend.schemas_rpc import CloudPlan, LoginResult
from igni_core.core.auth.credentials import CloudCredentials, CredentialsStore
from igni_core.core.auth.discovery import ServerDiscovery
from igni_core.core.auth.portal_client import PortalClient
from igni_core.core.auth.server_url import is_safe_url
from igni_core.core.config.user_config_store import UserConfigStore
from igni_core.protocol import messages as msg

if TYPE_CHECKING:
    from igni_core.core.config.settings import Settings
    from igni_core.core.session import Session

logger = logging.getLogger(__name__)


class AuthController:
    """Cloud auth controller for a single session + settings pair.

    Constructed once per :class:`BackendServer` (or per test). Every
    collaborator is injected: ``portal`` is shared with ``group_policy``
    so both talk to the same server, ``user_config_store`` is where
    a chosen server is written down, and ``server_discovery`` reads
    where an address's organisation runs igni.
    """

    def __init__(
        self,
        session: Session,
        settings: Settings,
        status_provider: Callable[[], msg.StatusUpdate],
        *,
        portal: PortalClient,
        group_policy: GroupPolicySync,
        user_config_store: UserConfigStore,
        server_discovery: ServerDiscovery,
    ) -> None:
        self._session = session
        self._settings = settings
        # ``status_provider`` is injected as a callable so
        # ``reload_cloud_credentials`` / ``clear_cloud_credentials``
        # can return the fresh :class:`msg.StatusUpdate` shape
        # without this class needing to know the ``ContextController``
        # exists.
        self._status_provider = status_provider
        self._portal = portal
        self._group_policy = group_policy
        self._user_config_store = user_config_store
        self._server_discovery = server_discovery
        # One :class:`CredentialsStore` per controller so the login
        # write + the :class:`CloudCredentials` read below share a
        # single path source.
        self._store = CredentialsStore(self._settings.auth.credentials_file)

    async def login(self, on_status: StatusCallback = None) -> LoginResult:
        """Run the browser-callback login flow and hand the session its credentials.

        The sign-in itself is :class:`BrowserSignIn`'s — status lines go
        to ``on_status``, expected failures come back as ``ok=False`` —
        and the group pack it refreshes is this session's, so a new
        group's agents load at once.
        """
        result = await BrowserSignIn(self._portal, self._store, self._group_policy).login(on_status)
        if result.ok:
            self.reload_cloud_credentials()
        return result

    def reload_cloud_credentials(self) -> msg.StatusUpdate:
        """Reload cloud credentials after login."""
        # Share the controller's :class:`CredentialsStore` so the
        # session sees the exact file the login flow just wrote.
        self._session.cloud_auth.replace(CloudCredentials(store=self._store))
        return self._status_provider()

    def clear_cloud_credentials(self) -> msg.StatusUpdate:
        """Sign out of igni server, as ``/logout`` does: the saved
        credentials are deleted — so no session, and no later launch,
        signs in from them — the session forgets its own, and a model
        that routes through igni server gives way to one that does not."""
        self._store.clear()
        self._session.cloud_auth.clear()
        switch_off_cloud(self._session)
        return self._status_provider()

    async def get_cloud_plan(self) -> CloudPlan | None:
        """Fetch the current user's plan tier from the cloud.

        Hits ``/portal/me`` with the stored JWT. Returns ``None``
        when logged out or the call fails.
        """
        token = self._session.cloud_auth.access_token
        if not token:
            return None
        result = await self._portal.validate_token(token)
        if not result.ok or result.user is None:
            return None
        return CloudPlan.from_user_info(result.user)

    # ── Which server this install talks to ───────────────────────────

    async def discover_server(self, email: str) -> ServerFound:
        """Where this address's organisation runs igni.

        Read from a record the organisation publishes on its own
        domain — see
        :mod:`igni_core.core.auth.discovery` for why nothing
        vendor-run is asked.

        Never raises, and "nothing published" is not a failure: most
        domains have no record, and the screen offers an address field
        instead. ``reason`` is there for when somebody asks why.
        """
        return ServerFound.from_discovered(await self._server_discovery.discover(email))

    def set_server(self, api_url: str, portal_url: str = "") -> ServerChoice:
        """Write the server down, so the next launch knows it.

        Refused unless the URL is one discovery would have accepted.
        This is reachable with a hand-typed address — the fallback for
        a deployment that publishes no record — and "the user typed it"
        is not a reason to write a plaintext host into a config file
        that decides where sign-in codes are sent.

        ``portal_url`` is optional and empty means "derive it", which
        is right wherever the derivation was right before.
        """
        chosen = (api_url or "").strip().rstrip("/")
        portal = (portal_url or "").strip().rstrip("/")
        if not chosen:
            return ServerChoice.refused("Name the server to connect to.")
        if not is_safe_url(chosen):
            return ServerChoice.refused(f"{chosen} is not an https address.")
        if portal and not is_safe_url(portal):
            return ServerChoice.refused(f"{portal} is not an https address.")

        self._user_config_store.set_server(chosen, portal)
        # The settings object this controller was built with is the
        # one every later call reads, so it has to learn too — a
        # config file the running process disagrees with is the
        # confusing half of this operation.
        self._settings.api_url = chosen
        # Empty: derived from the new server, never the old server's.
        self._settings.portal_url = portal
        self._session.cloud_auth.use_server(chosen)
        self._portal = PortalClient(api_url=chosen, portal_url=portal or None)
        self._group_policy.use_portal(self._portal)
        logger.info("server set to %s", chosen)
        return ServerChoice(api_url=chosen, portal_url=portal)
