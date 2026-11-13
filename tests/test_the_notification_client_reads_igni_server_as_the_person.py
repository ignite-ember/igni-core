"""The notification routes, called as the signed-in person, and every way a call fails.

A refusal, a connection that cannot be made and a body that is not what
igni server promised are each a :class:`NotificationsUnavailable` that
says which, never a raw error. Who is signed in and where the server
is are read fresh on every call, after the session's token is ready.
"""

from __future__ import annotations

import httpx
import pytest

from igni_core.core.activity import ActivityRegistry
from igni_core.extensions.service_token import IgniSession, NotSignedIn
from igni_core.notifications.schemas import MarkReadRequest
from igni_core.notifications.server_client import (
    IgniNotificationsClient,
    NotificationsUnavailable,
    SignedInServer,
)
from tests.notification_helpers import API_URL, SESSION, signed_in


def answering(body: bytes | str, status: int = 200) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status, content=body))


# ── Where the server is and who is signed in ────────────────────────


def test_nobody_signed_in_and_no_server_are_told_apart():
    with pytest.raises(NotSignedIn, match="nobody is signed in"):
        signed_in(session=None).current()
    for blank in (None, "", "   "):
        with pytest.raises(NotSignedIn, match="no igni server is configured"):
            signed_in(api_url=blank).current()


def test_the_address_loses_its_trailing_slash():
    for written in (f"{API_URL}/", f"{API_URL}//"):
        url, session = signed_in(api_url=written).current()
        assert url == API_URL
        assert session.access_token == SESSION


def test_the_address_loses_stray_spaces_around_it():
    # A configured address with a space at either end is still an address.
    url, _ = signed_in(api_url=f"  {API_URL}/ ").current()

    assert url == API_URL


async def test_the_token_is_made_ready_before_the_session_is_read():
    order: list[str] = []

    async def ready() -> None:
        order.append("ready")

    def session() -> IgniSession:
        order.append("session")
        return IgniSession(access_token="renewed")

    server = SignedInServer(api_url=lambda: API_URL, session=session, ready=ready)

    _, got = await server.signed_in()

    assert order == ["ready", "session"]
    assert got.access_token == "renewed"


async def test_a_renewal_that_fails_is_not_hidden():
    async def ready() -> None:
        raise RuntimeError("renewal failed")

    server = SignedInServer(
        api_url=lambda: API_URL, session=lambda: IgniSession(access_token="t"), ready=ready
    )

    with pytest.raises(RuntimeError, match="renewal failed"):
        await server.signed_in()


async def test_a_call_reads_the_url_and_the_person_again_each_time():
    seen: list[tuple[str, str]] = []
    state = {"url": "https://one.test/", "token": "a"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers["authorization"]))
        return httpx.Response(200, json={"notifications": []})

    client = IgniNotificationsClient(
        SignedInServer(
            api_url=lambda: state["url"],
            session=lambda: IgniSession(access_token=state["token"]),
        ),
        transport=httpx.MockTransport(handler),
    )

    await client.listing(limit=7)
    state.update(url="https://two.test", token="b")
    await client.listing()

    assert seen == [
        ("https://one.test/v1/notifications?limit=7", "Bearer a"),
        ("https://two.test/v1/notifications?limit=50", "Bearer b"),
    ]


async def test_signed_out_raises_not_signed_in_not_unavailable():
    client = IgniNotificationsClient(signed_in(session=None), transport=answering("{}"))

    with pytest.raises(NotSignedIn):
        await client.counts()


# ── Every way a call fails ──────────────────────────────────────────


@pytest.mark.parametrize("status", [401, 403, 500])
async def test_any_status_but_ok_names_the_call_and_the_status(status):
    client = IgniNotificationsClient(signed_in(), transport=answering("{}", status))

    with pytest.raises(
        NotificationsUnavailable, match=rf"POST /v1/notifications/read \({status}\)"
    ):
        await client.mark_read(MarkReadRequest())
    with pytest.raises(NotificationsUnavailable, match=rf"GET /v1/portal/me \({status}\)"):
        await client.organisations()


async def test_a_connection_that_cannot_be_made_says_igni_could_not_be_reached():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = IgniNotificationsClient(signed_in(), transport=httpx.MockTransport(refuse))

    for call in (client.listing(), client.counts(), client.organisations()):
        with pytest.raises(NotificationsUnavailable, match="igni could not be reached.*no route"):
            await call


async def test_a_body_that_is_not_what_was_promised_is_unavailable_for_each_reader():
    client = IgniNotificationsClient(signed_in(), transport=answering('{"notifications": 3}'))
    with pytest.raises(NotificationsUnavailable, match="notification list is malformed"):
        await client.listing()

    client = IgniNotificationsClient(signed_in(), transport=answering('{"unread": "many"}'))
    with pytest.raises(NotificationsUnavailable, match="notification counts are malformed"):
        await client.counts()

    client = IgniNotificationsClient(signed_in(), transport=answering('{"organizations": 1}'))
    with pytest.raises(NotificationsUnavailable, match="account is malformed"):
        await client.organisations()


async def test_a_body_that_is_not_json_is_unavailable_too():
    client = IgniNotificationsClient(signed_in(), transport=answering("<html>"))

    with pytest.raises(NotificationsUnavailable, match="malformed"):
        await client.listing()


async def test_a_row_without_its_extension_is_malformed_not_skipped():
    client = IgniNotificationsClient(
        signed_in(), transport=answering('{"notifications": [{"id": "n1"}]}')
    )

    with pytest.raises(NotificationsUnavailable, match="malformed"):
        await client.listing()


# ── The call itself ─────────────────────────────────────────────────


async def test_a_call_holds_an_activity_lease_while_it_waits():
    held: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        held.append([lease.holder for lease in ActivityRegistry.shared().busy()])
        return httpx.Response(200, json={"unread": 0})

    client = IgniNotificationsClient(signed_in(), transport=httpx.MockTransport(handler))

    await client.counts()

    assert held == [["igni-server"]]
    assert ActivityRegistry.shared().busy() == []


async def test_the_wait_for_an_answer_is_bounded():
    client = IgniNotificationsClient(signed_in())

    assert client._timeout == 15.0  # type: ignore[attr-defined]
