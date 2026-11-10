"""How the backend reaches igni server as the person signed in here."""

from __future__ import annotations

from collections.abc import Callable

from igni_core.extensions.service_token import CredentialsSession, ServiceTokenExchange
from igni_core.notifications.server_client import SignedInServer


class IgniServerAccess:
    """igni server's URL and the signed-in session, for whoever calls it.

    Both are read fresh on every ask: signing in can change which server
    it is, and signing out must stop the session being sent.
    """

    def __init__(self, *, credentials_file: str | None, api_url: Callable[[], str | None]) -> None:
        self._api_url = api_url
        self._session = CredentialsSession(credentials_file)

    def api_url(self) -> str | None:
        """igni server's URL as it is configured now."""
        return self._api_url()

    @property
    def session(self) -> CredentialsSession:
        return self._session

    def signed_in_server(self) -> SignedInServer:
        """A client of igni server as the signed-in person; one per consumer."""
        return SignedInServer(
            api_url=self._api_url, session=self._session, ready=self._session.ready
        )

    def token_exchange(self) -> ServiceTokenExchange:
        """A new exchange, with its own token cache. Build one and share it."""
        return ServiceTokenExchange(
            api_url=self._api_url, session=self._session, ready=self._session.ready
        )
