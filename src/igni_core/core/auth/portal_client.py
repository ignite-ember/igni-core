"""Portal endpoint client — login coordination + token validation.

Home for :class:`PortalClient`, the coordinator that owns portal
endpoint state (``portal_url`` / ``api_url`` / ``http_timeout``)
and orchestrates the browser-callback login flow. Replaces the
free-function-with-defaults pattern in the old
``core/auth/client.py`` (``get_login_url`` / ``start_callback_server``
/ ``wait_for_token`` / ``wait_for_callback`` / ``validate_token``),
each of which threaded module-level defaults through five different
signatures.

Public surface:

* :meth:`PortalClient.login_url` — build the CLI-auth URL for a
  given local callback port.
* :meth:`PortalClient.start_callback` — construct and return a
  fresh :class:`~igni_core.core.auth.callback_server.CallbackServer`
  (exposed for callers that need to interleave status callbacks
  between server start and token wait, e.g. the ``AuthController``).
* :meth:`PortalClient.validate_token` — hit ``/v1/portal/me`` and
  return a :class:`~igni_core.core.auth.schemas.ValidateResult`
  carrying either the :class:`~igni_core.core.auth.schemas.UserInfo`
  or a machine-readable failure ``reason`` (no ambiguous ``None``).
* :meth:`PortalClient.run_login` — full one-shot coordinator:
  builds a :class:`CallbackServer`, opens the browser, awaits the
  token, and returns a :class:`~igni_core.core.auth.schemas.LoginResult`.
  Does *not* raise on timeout — callers get
  ``LoginResult(ok=False, reason='timeout')``.
"""

from __future__ import annotations

import logging
import webbrowser

import httpx
from pydantic import ValidationError

from igni_core.core.activity.http import busy_call
from igni_core.core.auth.callback_server import CallbackServer
from igni_core.core.auth.schemas import (
    LoginResult,
    UserInfo,
    ValidateResult,
)
from igni_core.core.config.endpoint import (
    is_configured,
    portal_url_for,
    require_api_url,
)
from igni_core.core.config.group_policy.answers import (
    PACK_UNCHANGED,
    PackAnswer,
)
from igni_core.core.config.group_policy.issuer import IgniServerUrl
from igni_core.core.config.group_policy.jwks import Jwks
from igni_core.core.config.group_policy.response import PackResponse

logger = logging.getLogger(__name__)


class PortalClient:
    """Coordinator for portal endpoints — login + token validation.

    Constructed once per caller (typically once per
    :class:`~igni_core.backend.server_auth.AuthController`); no
    mutable state beyond the injected endpoint strings.

    **DEC-14.** This had two module-level defaults,
    the vendor's API host and the vendor's website, and the second was
    the worse of the two: ``api_url`` at least came from
    a setting a customer could change, while ``portal_url`` had no
    setting anywhere. So a customer who correctly pointed the CLI at
    their own deployment still had it open the vendor's website to sign
    in — and being unconfigurable, that was not something they could
    discover and fix.

    ``api_url`` is now required, and ``portal_url`` is derived from it by
    :func:`~igni_core.core.config.endpoint.portal_url_for` unless a
    caller passes one. One value to set, and the pair cannot disagree.

    **Constructing this does not refuse.** The first version checked
    ``api_url`` in ``__init__``, and it broke the backend server: an
    ``AuthController`` is built at startup whether or not anybody signs
    in, so an unconfigured install could not boot at all. Refusing to
    start because a *future* operation might need a setting is the wrong
    trade — the refusal belongs at the operation, which is where the
    message can say what the user was trying to do.
    """

    def __init__(
        self,
        api_url: str,
        portal_url: str | None = None,
        http_timeout: float = 15.0,
    ) -> None:
        # Stored raw. `_base` checks it, per call.
        self._api_url = api_url.rstrip("/") if api_url else ""
        self._portal_url = portal_url.rstrip("/") if portal_url else None
        self._http_timeout = http_timeout

    def _base(self, operation: str) -> str:
        """The API base, or refuse naming ``operation``."""
        return require_api_url(self._api_url, operation=operation)

    @property
    def configured(self) -> bool:
        """Whether a caller can use this without being refused.

        For the callers that offer sign-in as an option in a menu: a
        greyed-out item with a reason beats one that raises when
        clicked.
        """
        return is_configured(self._api_url)

    @property
    def portal_url(self) -> str:
        """The human-facing portal, derived unless one was passed."""
        if self._portal_url:
            return self._portal_url
        return portal_url_for(self._base("Opening the portal"))

    @property
    def api_url(self) -> str:
        return self._api_url

    def login_url(self, port: int) -> str:
        """Build the portal CLI-auth URL for ``port``.

        e.g. ``https://acme.example/cli-auth?port=53842``.
        """
        return f"{self.portal_url.rstrip('/')}/cli-auth?port={port}"

    def start_callback(self) -> CallbackServer:
        """Return a fresh :class:`CallbackServer` bound to a free port.

        Exposed so callers can interleave status callbacks between
        server start (fast) and token wait (blocks on the user).
        """
        # The server needs the API base to redeem the one-time code the
        # portal now sends in place of the token. F124.
        return CallbackServer(api_url=self._base("Signing in"), http_timeout=self._http_timeout)

    async def _get(self, url: str, headers: dict[str, str] | None = None) -> httpx.Response:
        """One GET to igni server, shown as busy while it runs."""
        async with (
            busy_call("igni-server"),
            httpx.AsyncClient(timeout=self._http_timeout) as client,
        ):
            return await client.get(url, headers=headers)

    @staticmethod
    def _bearer(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def validate_token(self, token: str) -> ValidateResult:
        """Validate ``token`` against ``/v1/portal/me``.

        Returns a :class:`ValidateResult` whose ``ok`` flag is the
        single source of truth. On failure the ``reason`` field
        distinguishes network errors, non-200 responses, and JSON /
        schema problems so callers can log or branch precisely.
        """
        url = f"{self._base('Validating your session')}/v1/portal/me"
        try:
            resp = await self._get(url, headers=self._bearer(token))
        except (httpx.HTTPError, OSError) as exc:
            logger.debug("Token validation network error: %s", exc)
            return ValidateResult(ok=False, reason="network_error", error=str(exc))

        if resp.status_code != 200:
            return ValidateResult(ok=False, reason="http_error", status_code=resp.status_code)

        try:
            payload = resp.json()
        except ValueError as exc:
            logger.debug("Token validation decode error: %s", exc)
            return ValidateResult(
                ok=False, reason="decode_error", status_code=resp.status_code, error=str(exc)
            )

        try:
            user = UserInfo.model_validate(payload)
        except ValidationError as exc:
            logger.debug("Token validation schema mismatch: %s", exc)
            return ValidateResult(
                ok=False, reason="schema_mismatch", status_code=resp.status_code, error=str(exc)
            )

        return ValidateResult(ok=True, user=user, status_code=resp.status_code)

    async def run_login(self, timeout: float = 300.0) -> LoginResult:
        """Run the full browser-callback login flow.

        Opens a :class:`CallbackServer`, launches the user's browser
        at :meth:`login_url`, and awaits the token — up to
        ``timeout`` seconds. Never raises on timeout; returns
        ``LoginResult(ok=False, reason='timeout')`` instead so the
        caller doesn't have to string-match exception messages.
        """
        try:
            async with CallbackServer(
                api_url=self._base("Signing in"), http_timeout=self._http_timeout
            ) as server:
                login_url = self.login_url(server.port)
                try:
                    webbrowser.open(login_url)
                except (webbrowser.Error, OSError) as exc:
                    # The login can still finish: the user opens the URL by hand.
                    logger.warning(
                        "Could not open a browser; open %s to sign in: %s", login_url, exc
                    )

                token = await server.wait_for_token(timeout=timeout)
                if token is None:
                    return LoginResult(
                        ok=False,
                        callback_url=server.callback_url,
                        reason="timeout",
                        error="Login timed out — no callback received",
                    )
                return LoginResult(ok=True, token=token, callback_url=server.callback_url)
        except OSError as exc:
            return LoginResult(ok=False, reason="port_bind_failed", error=str(exc))
        except Exception as exc:
            logger.warning("Login failed: %s", exc)
            return LoginResult(ok=False, reason="handler_error", error=str(exc))

    async def fetch_jwks(self) -> Jwks | None:
        """igni server's published signing keys, or ``None``.

        What a group pack's signature is checked against when no key is
        pinned (``group_policy.trusted_keys``). Fetched from the
        configured ``api_url`` — over TLS, which is what makes the answer
        worth trusting; ``PackVerifier`` refuses an unpinned plain-HTTP one.
        """
        url = IgniServerUrl(url=self._base("Fetching igni server's signing keys")).jwks_url()
        try:
            resp = await self._get(url)
        except (httpx.HTTPError, OSError) as exc:
            logger.debug("fetch_jwks network error: %s", exc)
            return None
        if resp.status_code != 200:
            logger.debug("fetch_jwks got status %s", resp.status_code)
            return None
        try:
            payload = resp.json()
        except ValueError as exc:
            logger.debug("fetch_jwks decode error: %s", exc)
            return None
        return Jwks.parse(payload)

    async def fetch_group_pack(
        self,
        token: str,
        etag: str | None = None,
    ) -> PackAnswer:
        """Fetch the group pack, or learn that it has not changed.

        Four answers, and the caller has to tell them apart:

        * a :class:`GroupPolicyPack` — here is the group's configuration
        * ``None`` — no group, or the request failed; keep what you have
        * :data:`PACK_UNCHANGED` — the server says your copy is current
        * a :class:`SignedAbsence` — the server says, signed, that you have
          no pack; once verified, the cached one is cleared

        Passing ``etag`` is what makes the third possible. Sessions poll
        this every few minutes, and the answer is nearly always the
        third; without it every poll would ship a full set of agent
        prompts to say nothing had happened.
        """
        url = f"{self._base('Fetching your group policy')}/v1/portal/me/group/pack"
        headers = self._bearer(token)
        if etag:
            headers["If-None-Match"] = etag
        try:
            resp = await self._get(url, headers=headers)
        except (httpx.HTTPError, OSError) as exc:
            logger.debug("fetch_group_pack network error: %s", exc)
            return None

        if resp.status_code == 304:
            return PACK_UNCHANGED
        if resp.status_code in (204, 404):
            return None
        if resp.status_code != 200:
            logger.debug("fetch_group_pack got status %s", resp.status_code)
            return None

        try:
            return PackResponse.model_validate(resp.json()).answer(resp.headers.get("etag"))
        except ValueError as exc:  # a body that is not JSON, or not a pack
            logger.debug("fetch_group_pack unreadable response: %s", exc)
            return None
