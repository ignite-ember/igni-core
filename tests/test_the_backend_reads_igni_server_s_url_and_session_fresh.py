"""How the backend reaches igni server: the URL and the session, read fresh.

Signing in can change which server it is, and signing out must stop the
session being sent, so neither is kept by the clients
:class:`IgniServerAccess` hands out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.backend.igni_server_access import IgniServerAccess
from igni_core.core.auth.credentials import Credentials, CredentialsStore
from igni_core.notifications.server_client import NotSignedIn


def _sign_in(path: Path, token: str = "tok-1") -> CredentialsStore:
    store = CredentialsStore(str(path))
    store.save(Credentials.new(token, "p@example.com"))
    return store


def test_the_url_is_asked_for_on_every_call(tmp_path: Path) -> None:
    url: list[str | None] = ["https://one.example/"]
    access = IgniServerAccess(credentials_file=str(tmp_path / "c.json"), api_url=lambda: url[0])

    assert access.api_url() == "https://one.example/"
    url[0] = "https://two.example"
    assert access.api_url() == "https://two.example"
    url[0] = None
    assert access.api_url() is None


def test_a_server_client_follows_a_sign_in_that_switches_server(tmp_path: Path) -> None:
    creds = tmp_path / "c.json"
    url = ["https://one.example/"]
    access = IgniServerAccess(credentials_file=str(creds), api_url=lambda: url[0])
    server = access.signed_in_server()

    with pytest.raises(NotSignedIn, match="nobody is signed in"):
        server.current()

    store = _sign_in(creds)
    api_url, session = server.current()
    assert (api_url, session.access_token) == ("https://one.example", "tok-1")

    url[0] = "https://two.example/"
    assert server.current()[0] == "https://two.example"

    url[0] = ""
    with pytest.raises(NotSignedIn, match="no igni server"):
        server.current()

    # Signing out stops the session being sent, through a client built before.
    url[0] = "https://two.example"
    store.clear()
    with pytest.raises(NotSignedIn, match="nobody is signed in"):
        server.current()


def test_the_session_is_read_from_the_file_each_time(tmp_path: Path) -> None:
    creds = tmp_path / "c.json"
    access = IgniServerAccess(credentials_file=str(creds), api_url=lambda: None)
    assert access.session() is None

    _sign_in(creds, "tok-a")
    first = access.session()
    assert first is not None and first.access_token == "tok-a"
    _sign_in(creds, "tok-b")
    second = access.session()
    assert second is not None and second.access_token == "tok-b"


def test_with_no_credentials_file_nobody_is_signed_in() -> None:
    access = IgniServerAccess(credentials_file=None, api_url=lambda: "https://x.example")

    assert access.session() is None


def test_every_consumer_gets_its_own_client_and_exchange(tmp_path: Path) -> None:
    access = IgniServerAccess(credentials_file=str(tmp_path / "c.json"), api_url=lambda: None)

    assert access.signed_in_server() is not access.signed_in_server()
    first, second = access.token_exchange(), access.token_exchange()
    assert first is not second
    assert first._cache is not second._cache
