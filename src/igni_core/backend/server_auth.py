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

import asyncio
import contextlib
import logging
import webbrowser
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from igni_core.backend.group_policy_sync import GroupPolicySync
from igni_core.backend.schemas_auth import ServerChoice, ServerFound
from igni_core.backend.schemas_rpc import CloudPlan, LoginResult
from igni_core.core.activity import background
from igni_core.core.auth.credentials import (
    CloudCredentials,
    Credentials,
    CredentialsStore,
)
from igni_core.core.auth.discovery import discover, is_safe_url
from igni_core.core.auth.portal_client import PortalClient
from igni_core.core.auth.schemas import JwtClaims
from igni_core.core.config.endpoint import ApiUrlNotConfigured
from igni_core.core.config.user_config_store import UserConfigStore
from igni_core.protocol import messages as msg

if TYPE_CHECKING:
    from igni_core.core.config.settings import Settings
    from igni_core.core.session import Session

logger = logging.getLogger(__name__)


StatusCallback = Callable[[str], Awaitable[None] | None] | None


class _StatusForwarder:
    """Adapter around the optional login-status callback.

    Wrapping the sync-or-async callback in a named collaborator (one
    per :meth:`AuthController.login` call) flattens the login body —
    the previous nested ``def _status`` closure hid the async-schedule
    quirk (``ensure_future`` for coroutine returns) inside a
    per-line-of-progress lambda. Same semantics, one place to explain
    them.
    """

    def __init__(self, callback: StatusCallback) -> None:
        self._callback = callback

    def emit(self, text: str) -> None:
        """Forward ``text`` to the wrapped callback (sync or async);
        no-op when no callback was supplied."""
        if self._callback is None:
            return
        result = self._callback(text)
        # Coroutine callbacks are scheduled fire-and-forget so the
        # login flow doesn't have to await status echoes.
        if asyncio.iscoroutine(result):
            background.start(result, holder="auth", reason="a status callback")


class AuthController:
    """Cloud auth controller for a single session + settings pair.

    Constructed once per :class:`BackendServer` (or per test). Every
    collaborator is injected: ``portal`` is shared with ``group_policy``
    so both talk to the same server, and ``user_config_store`` is where
    a chosen server is written down.
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
        # One :class:`CredentialsStore` per controller so the login
        # write + the :class:`CloudCredentials` read below share a
        # single path source.
        self._store = CredentialsStore(self._settings.auth.credentials_file)

    async def login(self, on_status: StatusCallback = None) -> LoginResult:
        """Run the browser-callback login flow.

        Returns a :class:`LoginResult` with named ``ok`` / ``email`` /
        ``error`` fields. Status callbacks are forwarded to
        ``on_status`` so the caller can echo progress into the FE.

        The failures a sign-in expects — no server configured, a port
        that will not bind, a credentials file that cannot be written —
        come back as ``ok=False``; anything else is a bug and raises.
        """
        status = _StatusForwarder(on_status)
        try:
            status.emit("Starting local server...")
            async with self._portal.start_callback() as callback:
                login_url = self._portal.login_url(callback.port)

                # No browser is not a failure: the address is printed next.
                with contextlib.suppress(webbrowser.Error, OSError):
                    webbrowser.open(login_url)

                status.emit(
                    f"Waiting for login in browser...\nIf the browser didn't open, go to:\n{login_url}"
                )

                token = await callback.wait_for_token(timeout=300)
                if token is None:
                    return LoginResult(ok=False, error="Login timed out")

            status.emit("Fetching user info...")
            validation = await self._portal.validate_token(token)
            email = validation.user.email if validation.ok and validation.user else ""
            self._store.save(self._credentials(token, email))
        except (ApiUrlNotConfigured, OSError) as exc:
            return LoginResult(ok=False, error=str(exc))

        # Refresh the on-disk group policy pack with the freshly issued
        # token so admin-side overrides take effect immediately rather
        # than waiting for the 5-min TTL. ``force`` is what makes that
        # true: without it a login inside the window did nothing.
        # Failures are non-fatal — hydration reports them as ``False``.
        await self._group_policy.hydrate(token, force=True)

        self.reload_cloud_credentials()
        return LoginResult(ok=True, email=email)

    @staticmethod
    def _credentials(token: str, email: str) -> Credentials:
        """Credentials expiring when the JWT does — or at the
        :meth:`Credentials.new` default when it carries no ``exp``."""
        claims = JwtClaims.decode(token)
        if claims is None or not claims.exp:
            return Credentials.new(token, email)
        now = datetime.now(timezone.utc)
        exp = datetime.fromtimestamp(claims.exp, tz=timezone.utc)
        return Credentials.new(token, email, ttl=max(int((exp - now).total_seconds()), 0))

    def reload_cloud_credentials(self) -> msg.StatusUpdate:
        """Reload cloud credentials after login."""
        # Share the controller's :class:`CredentialsStore` so the
        # session sees the exact file the login flow just wrote.
        self._session.replace_cloud_credentials(CloudCredentials(store=self._store))
        return self._status_provider()

    def clear_cloud_credentials(self) -> msg.StatusUpdate:
        """Clear cloud credentials on logout."""
        self._session.clear_cloud_credentials()
        return self._status_provider()

    async def get_cloud_plan(self) -> CloudPlan | None:
        """Fetch the current user's plan tier from the cloud.

        Hits ``/portal/me`` with the stored JWT. Returns ``None``
        when logged out or the call fails.
        """
        token = self._session.cloud_access_token
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
        return ServerFound.from_discovered(await discover(email))

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
        if portal:
            self._settings.portal_url = portal
        self._portal = PortalClient(api_url=chosen, portal_url=portal or None)
        self._group_policy.use_portal(self._portal)
        logger.info("server set to %s", chosen)
        return ServerChoice(api_url=chosen, portal_url=portal)
