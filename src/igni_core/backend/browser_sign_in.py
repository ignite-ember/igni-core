"""The browser-callback sign-in, with no session attached.

:class:`BrowserSignIn` starts a callback on this machine, opens the
portal's sign-in page, waits for the token, keeps it and refreshes the
group pack with it. The app's ``/login``
(:meth:`~igni_core.backend.server_auth.AuthController.login`) runs it and
then hands the new credentials to its session; ``igni login`` in a
terminal runs it on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import webbrowser
from collections.abc import Awaitable, Callable
from typing import Protocol

from igni_core.backend.schemas_rpc import LoginResult
from igni_core.core.activity import background
from igni_core.core.auth.credentials import Credentials, CredentialsStore
from igni_core.core.auth.portal_client import PortalClient
from igni_core.core.config.endpoint import ApiUrlNotConfigured

StatusCallback = Callable[[str], Awaitable[None] | None] | None


class GroupPackRefresh(Protocol):
    """What a sign-in asks to refresh the group pack with its new token."""

    async def hydrate(self, token: str, *, force: bool = False) -> bool: ...


class _StatusForwarder:
    """Adapter around the optional login-status callback.

    Wrapping the sync-or-async callback in a named collaborator (one
    per :meth:`BrowserSignIn.login` call) keeps the async-schedule
    quirk (``ensure_future`` for coroutine returns) in one place
    instead of in every line of progress.
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


class BrowserSignIn:
    """One browser sign-in against ``portal``, kept in ``store``.

    ``group_pack`` is refreshed with the new token so admin-side
    overrides take effect at once rather than at the next poll.
    """

    #: Seconds to wait for the browser to call back.
    CALLBACK_TIMEOUT = 300

    def __init__(
        self, portal: PortalClient, store: CredentialsStore, group_pack: GroupPackRefresh
    ) -> None:
        self._portal = portal
        self._store = store
        self._group_pack = group_pack

    async def login(self, on_status: StatusCallback = None) -> LoginResult:
        """Run the browser-callback login flow.

        Status lines go to ``on_status`` so the caller can echo progress.
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

                token = await callback.wait_for_token(timeout=self.CALLBACK_TIMEOUT)
                if token is None:
                    return LoginResult(ok=False, error="Login timed out")

            status.emit("Fetching user info...")
            validation = await self._portal.validate_token(token)
            email = validation.user.email if validation.ok and validation.user else ""
            self._store.save(Credentials.for_token(token, email))
        except (ApiUrlNotConfigured, OSError) as exc:
            return LoginResult(ok=False, error=str(exc))

        # ``force`` is what makes a login inside the pack's 5-minute
        # window pick up an admin's change. Failures are non-fatal —
        # hydration reports them as ``False``.
        await self._group_pack.hydrate(token, force=True)
        return LoginResult(ok=True, email=email)


__all__ = ["BrowserSignIn", "GroupPackRefresh", "StatusCallback"]
