"""The account chip tells the truth about igni server.

The chip says whether the person is signed in to igni server. A valid
token on disk is not the same as a server that is there: with igni
server down the chip still said "Account" while nothing answered
(campaign R638).

So the chip answers two questions: is the token good, and was the
server there the last time igni really asked it? The second is
answered by the requests igni already makes — nothing is polled, and
no request is added.

What we pin:

* a request that never got an answer says the server was not there;
* a 5xx says the server was there but broken;
* a refused token (401) is a credentials problem, not a reachability
  one, and does not make the server unreachable;
* the next success says the server is there again;
* an expired token says not connected whatever the server said;
* reading the chip asks the server nothing;
* each of the requests igni makes to igni server records what it found.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from igni_core.core.auth.credentials import CloudCredentials
from igni_core.core.auth.server_reachability import (
    record_answered,
    record_unreachable,
    reset_reachability,
)
from igni_core.core.config.schemas import AuthConfig, ModelsConfig
from igni_core.core.config.settings import Settings
from igni_core.core.session.cloud_auth import SessionCloudAuth
from igni_core.core.session.cloud_catalog import CloudModelCatalog
from igni_core.extensions.service_token import IgniSession
from igni_core.notifications.server_client import (
    IgniNotificationsClient,
    NotificationsUnavailable,
    SignedInServer,
)

SERVER = "https://chip.example"
_REAL_CLIENT = httpx.Client


@pytest.fixture(autouse=True)
def _no_reachability_left_over() -> Any:
    """Each test starts with nothing recorded about the server.

    The recorders live for the life of the process; this file also
    resets them itself, so it says what it means on its own.
    """
    reset_reachability()
    yield
    reset_reachability()


def _jwt(payload: dict) -> str:
    def part(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return ".".join([part(b'{"alg":"HS256"}'), part(json.dumps(payload).encode()), part(b"sig")])


def _creds(tmp_path: Path, token: str | None = None) -> CloudCredentials:
    path = tmp_path / "credentials.json"
    token = token if token is not None else _jwt({"org": "org_42", "exp": 4_102_444_800})
    path.write_text(json.dumps({"access_token": token, "email": "maya@acme.test"}))
    return CloudCredentials(str(path))


def _auth(tmp_path: Path, token: str | None = None) -> SessionCloudAuth:
    return SessionCloudAuth(
        creds=_creds(tmp_path, token),
        server_url=SERVER,
        rebuild_team=lambda: None,
        catalog=None,  # type: ignore[arg-type]
    )


def _settings(tmp_path: Path, token: str = "tok") -> Settings:
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"access_token": token, "email": "a@b.test"}))
    return Settings(
        api_url=SERVER,
        auth=AuthConfig(credentials_file=str(creds)),
        models=ModelsConfig(),
    )


def _serve(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    """Every httpx.Client answers with ``handler`` instead of the network."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    monkeypatch.setattr(
        "httpx.Client", lambda **_: _REAL_CLIENT(transport=httpx.MockTransport(respond))
    )
    return requests


def _server() -> SignedInServer:
    return SignedInServer(
        api_url=lambda: SERVER,
        session=lambda: IgniSession(access_token="tok"),
    )


def test_a_request_that_never_answered_says_the_server_is_not_there(tmp_path: Path) -> None:
    """The server did not answer: the chip must not claim it is there."""
    auth = _auth(tmp_path)
    assert auth.connected is True

    record_unreachable(SERVER, "ConnectError")

    assert auth.connected is False


def test_a_broken_server_says_the_server_is_not_there(tmp_path: Path) -> None:
    """A 5xx is an answer, but a broken one: the chip says no."""
    auth = _auth(tmp_path)

    record_answered(SERVER, 503)

    assert auth.connected is False


def test_a_refused_token_is_not_a_reachability_problem(tmp_path: Path) -> None:
    """The server answered and refused the token: it was there."""
    auth = _auth(tmp_path)

    record_answered(SERVER, 401)

    assert auth.connected is True


def test_the_next_success_says_the_server_is_there_again(tmp_path: Path) -> None:
    """The server came back: the chip says so."""
    auth = _auth(tmp_path)
    record_unreachable(SERVER, "ConnectError")
    assert auth.connected is False

    record_answered(SERVER, 200)

    assert auth.connected is True


def test_an_expired_token_says_not_connected_whatever_the_server_said(tmp_path: Path) -> None:
    """The token is the credentials' business; the server's health does
    not make an expired token good."""
    auth = _auth(tmp_path, token=_jwt({"org": "org_42", "exp": 1}))
    record_answered(SERVER, 200)

    assert auth.connected is False


def test_reading_the_chip_asks_the_server_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is polled: the chip reads what the last request found.

    Reading the chip must not open a connection — a probe would be
    traffic nobody asked for, forever.
    """

    def _no_client(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the chip asked the server something")

    monkeypatch.setattr("httpx.Client", _no_client)
    monkeypatch.setattr("httpx.AsyncClient", _no_client)
    auth = _auth(tmp_path)

    assert auth.connected is True
    assert auth.connected is True


def test_the_catalogue_refresh_says_the_server_is_not_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refresh is a real request: a dead server is found out by it."""
    auth = _auth(tmp_path)

    def _dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _serve(monkeypatch, _dead)
    CloudModelCatalog(_settings(tmp_path)).refresh()

    assert auth.connected is False


def test_a_refresh_that_answered_says_the_server_is_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same request, against a server that answers."""
    auth = _auth(tmp_path)

    _serve(monkeypatch, lambda _r: httpx.Response(200, json={"models": []}))
    CloudModelCatalog(_settings(tmp_path)).refresh()

    assert auth.connected is True


def test_a_refresh_that_the_server_broke_says_the_server_is_not_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 5xx is a failed real request: the chip says no."""
    auth = _auth(tmp_path)

    _serve(monkeypatch, lambda _r: httpx.Response(503, json={"error": "down"}))
    CloudModelCatalog(_settings(tmp_path)).refresh()

    assert auth.connected is False


async def test_the_account_reading_says_the_server_is_not_there() -> None:
    """Reading who the person is is a real request too."""
    auth = _auth(Path("/tmp"))

    def _dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = IgniNotificationsClient(_server(), transport=httpx.MockTransport(_dead))

    with pytest.raises(NotificationsUnavailable):
        await client.organisations()

    assert auth.connected is False


async def test_an_account_reading_the_server_answered_says_the_server_is_there() -> None:
    """The same call, against a server that answers."""
    auth = _auth(Path("/tmp"))

    client = IgniNotificationsClient(
        _server(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"organizations": [{"id": "org_1", "display_name": "Acme"}]}
            )
        ),
    )

    assert await client.organisations() == {"org_1": "Acme"}
    assert auth.connected is True


async def test_the_event_pull_says_the_server_is_not_there() -> None:
    """The desktop's own pull of the person's events is a request too."""
    from igni_core.extensions.event_pull import PersonEventPull, PullFailed

    auth = _auth(Path("/tmp"))

    def _dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    pull = PersonEventPull(
        server=_server(),
        inbox=None,  # type: ignore[arg-type]
        transport=httpx.MockTransport(_dead),
    )

    with pytest.raises(PullFailed):
        await pull.once()

    assert auth.connected is False
