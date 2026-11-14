"""The service-token listener answers whatever arrives, and says why it refused.

One listener serves every extension process on the machine, so whatever
a client sends — nothing, half a request, a huge one, a request that is
not HTTP, a person's name in a language igni's header rules do not
cover — it gets an answer or a closed connection, and the listener
keeps serving the next one. The exchange behind it says in a sentence
why igni did not issue a token, and the session it reads is the one on
disk now, not the one that was there when the host started.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from igni_core.core.auth.credentials import Credentials, CredentialsStore
from igni_core.extensions import service_token
from igni_core.extensions.principal import Principal, PrincipalKind
from igni_core.extensions.service_token import (
    ENV_TOKEN_SECRET,
    ENV_TOKEN_URL,
    CachedToken,
    CredentialsSession,
    IgniSession,
    NotSignedIn,
    PageServiceTokens,
    ServiceTokenEndpoint,
    ServiceTokenExchange,
    TokenExchangeFailed,
)
from tests.extension_distribution_helpers import API_URL, SESSION, FakeIgni

NOTES = "https://notes.example.com"


def exchange_over(handler: Any, *, session: IgniSession | None = None) -> ServiceTokenExchange:
    return ServiceTokenExchange(
        api_url=lambda: API_URL,
        session=lambda: session or IgniSession(access_token=SESSION, org_id="org-1"),
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture
def igni() -> FakeIgni:
    return FakeIgni()


@pytest.fixture
async def endpoint(igni):
    exchange = ServiceTokenExchange(
        api_url=lambda: API_URL,
        session=lambda: IgniSession(access_token=SESSION, org_id="org-1"),
        transport=igni.transport(),
    )
    served = ServiceTokenEndpoint(exchange)
    yield served
    await served.stop()


async def raw(endpoint: ServiceTokenEndpoint, request: bytes | None) -> bytes:
    """Send bytes to the listener as they are and read everything it answers."""
    port = int(endpoint.url.split(":")[2].split("/")[0])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    if request is not None:
        writer.write(request)
        await writer.drain()
    try:
        return await asyncio.wait_for(reader.read(), timeout=5)
    finally:
        writer.close()


def request_for(endpoint: ServiceTokenEndpoint, secret: str, *headers: str) -> bytes:
    host = endpoint.url.split("/")[2]
    lines = [
        "GET /v1/service-token HTTP/1.1",
        f"Host: {host}",
        f"Authorization: Bearer {secret}",
        *headers,
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


# ── What a client can send that is not a request ───────────────────


async def test_a_request_that_never_completes_is_answered_408(endpoint, monkeypatch):
    monkeypatch.setattr(service_token, "HEAD_TIMEOUT", 0.2)
    await endpoint.start()
    answer = await raw(endpoint, b"GET /v1/service-token HTTP/1.1\r\nHost: x")
    assert answer.startswith(b"HTTP/1.1 408")


async def test_a_client_that_sends_nothing_and_hangs_up_is_answered_408(endpoint):
    await endpoint.start()
    port = int(endpoint.url.split(":")[2].split("/")[0])
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write_eof()
    answer = await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()
    assert answer.startswith(b"HTTP/1.1 408")


async def test_a_head_larger_than_the_limit_is_answered_431(endpoint):
    await endpoint.start()
    huge = b"GET /v1/service-token HTTP/1.1\r\nX-Pad: " + b"a" * 40_000 + b"\r\n\r\n"
    answer = await raw(endpoint, huge)
    assert answer.startswith(b"HTTP/1.1 431")


@pytest.mark.parametrize(
    "line",
    [
        b"GET /v1/service-token",
        b"GET /v1/service-token HTTP/2",
        b"GET /v1/service-token  HTTP/1.1",
        b"",
    ],
)
async def test_a_request_line_that_is_not_http_1_is_answered_400(endpoint, line):
    await endpoint.start()
    answer = await raw(endpoint, line + b"\r\nHost: x\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 400")
    assert b"not an HTTP/1.1 request" in answer


async def test_the_listener_keeps_serving_after_a_bad_client(endpoint):
    env = await endpoint.grant("acme.notes", NOTES)
    await raw(endpoint, b"nonsense\r\n\r\n")
    async with httpx.AsyncClient(trust_env=False) as client:
        ok = await client.get(
            env[ENV_TOKEN_URL], headers={"Authorization": f"Bearer {env[ENV_TOKEN_SECRET]}"}
        )
    assert ok.status_code == 200


async def test_a_client_that_hangs_up_before_the_answer_does_not_stop_the_listener(endpoint):
    env = await endpoint.grant("acme.notes", NOTES)
    port = int(endpoint.url.split(":")[2].split("/")[0])
    for _ in range(3):
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request_for(endpoint, env[ENV_TOKEN_SECRET]))
        await writer.drain()
        writer.transport.abort()
    await asyncio.sleep(0.05)
    answer = await raw(endpoint, request_for(endpoint, env[ENV_TOKEN_SECRET]))
    assert answer.startswith(b"HTTP/1.1 200")


# ── Who may ask ────────────────────────────────────────────────────


async def test_a_request_naming_a_person_is_refused_when_no_one_is_configured(endpoint):
    """With nobody to compare against, naming a principal is never allowed."""
    env = await endpoint.grant("acme.notes", NOTES)
    answer = await raw(
        endpoint,
        request_for(endpoint, env[ENV_TOKEN_SECRET], f"{Principal.TOKEN_HEADER}: person:u-ada"),
    )
    assert answer.startswith(b"HTTP/1.1 403")


async def test_a_name_with_characters_outside_ascii_is_refused_not_dropped(igni):
    ada = Principal(kind=PrincipalKind.PERSON, id="u-ada")
    exchange = ServiceTokenExchange(
        api_url=lambda: API_URL,
        session=lambda: IgniSession(access_token=SESSION),
        transport=igni.transport(),
    )
    served = ServiceTokenEndpoint(exchange, principal=lambda: ada)
    try:
        env = await served.grant("acme.notes", NOTES)
        answer = await raw(
            served,
            request_for(served, env[ENV_TOKEN_SECRET], f"{Principal.TOKEN_HEADER}: person:u-\xe9"),
        )
    finally:
        await served.stop()
    assert answer.startswith(b"HTTP/1.1 403")


@pytest.mark.parametrize("authorization", ["Bearer", "Bearer    ", "", "bearer"])
async def test_a_bearer_with_nothing_after_it_is_401(endpoint, authorization):
    await endpoint.grant("acme.notes", NOTES)
    host = endpoint.url.split("/")[2]
    answer = await raw(
        endpoint,
        f"GET /v1/service-token HTTP/1.1\r\nHost: {host}\r\nAuthorization: {authorization}\r\n\r\n".encode(),
    )
    assert answer.startswith(b"HTTP/1.1 401")
    assert b'WWW-Authenticate: Bearer realm="igni-service-token"' in answer


async def test_the_scheme_is_not_case_sensitive_and_a_query_string_is_ignored(endpoint):
    env = await endpoint.grant("acme.notes", NOTES)
    host = endpoint.url.split("/")[2]
    answer = await raw(
        endpoint,
        f"GET /v1/service-token?x=1 HTTP/1.1\r\nHost: {host}\r\n"
        f"Authorization: BEARER {env[ENV_TOKEN_SECRET]}\r\n\r\n".encode(),
    )
    assert answer.startswith(b"HTTP/1.1 200")


async def test_the_host_may_be_written_with_its_brackets(endpoint):
    env = await endpoint.grant("acme.notes", NOTES)
    port = endpoint.url.split(":")[2].split("/")[0]
    answer = await raw(
        endpoint,
        f"GET /v1/service-token HTTP/1.1\r\nHost: [127.0.0.1]:{port}\r\n"
        f"Authorization: Bearer {env[ENV_TOKEN_SECRET]}\r\n\r\n".encode(),
    )
    assert answer.startswith(b"HTTP/1.1 200")


async def test_the_wrong_port_in_the_host_is_421(endpoint):
    env = await endpoint.grant("acme.notes", NOTES)
    answer = await raw(
        endpoint,
        f"GET /v1/service-token HTTP/1.1\r\nHost: 127.0.0.1:1\r\n"
        f"Authorization: Bearer {env[ENV_TOKEN_SECRET]}\r\n\r\n".encode(),
    )
    assert answer.startswith(b"HTTP/1.1 421")


# ── When igni cannot issue the token ───────────────────────────────


async def test_igni_failing_to_exchange_is_502_with_the_sentence(igni):
    igni.token_status = 400
    exchange = ServiceTokenExchange(
        api_url=lambda: API_URL,
        session=lambda: IgniSession(access_token=SESSION),
        transport=igni.transport(),
    )
    served = ServiceTokenEndpoint(exchange)
    try:
        env = await served.grant("acme.notes", NOTES)
        answer = await raw(served, request_for(served, env[ENV_TOKEN_SECRET]))
    finally:
        await served.stop()
    assert answer.startswith(b"HTTP/1.1 502")
    assert b"igni refused the exchange (400)" in answer


async def test_the_exchange_says_when_igni_cannot_be_reached():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(TokenExchangeFailed, match="igni could not be reached: connection refused"):
        await exchange_over(down).token_for(NOTES)


async def test_the_exchange_says_when_igni_answers_something_that_is_not_a_token():
    def odd(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "t"})

    with pytest.raises(TokenExchangeFailed, match="something that is not a token"):
        await exchange_over(odd).token_for(NOTES)


async def test_the_exchange_says_when_igni_answers_200_with_no_json():
    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>")

    with pytest.raises(TokenExchangeFailed, match="something that is not a token"):
        await exchange_over(html).token_for(NOTES)


async def test_a_refusal_quotes_at_most_300_characters_of_igni_s_answer():
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="x" * 300 + "TAIL")

    with pytest.raises(TokenExchangeFailed) as refused:
        await exchange_over(refuse).token_for(NOTES)
    assert str(refused.value) == f"igni refused the exchange (500): {'x' * 300}"


async def test_the_organisation_is_sent_only_when_the_session_has_one(igni):
    forms: list[dict[str, list[str]]] = []
    igni.on_token = forms.append
    pinned = exchange_over(igni._handle, session=IgniSession(access_token=SESSION, org_id="org-9"))
    bare = exchange_over(igni._handle, session=IgniSession(access_token=SESSION))
    await pinned.token_for(NOTES)
    await bare.token_for(NOTES)
    assert forms[0]["organisation"] == ["org-9"]
    assert "organisation" not in forms[1]


async def test_a_trailing_slash_on_igni_server_does_not_double_the_path(igni):
    exchange = ServiceTokenExchange(
        api_url=lambda: API_URL + "/",
        session=lambda: IgniSession(access_token=SESSION),
        transport=igni.transport(),
    )
    await exchange.token_for(NOTES)
    assert [r.url.path for r in igni.requests] == ["/v1/oauth/token"]


async def test_a_token_that_lives_less_than_the_renewal_window_is_fetched_every_time():
    issued = 0

    def short(request: httpx.Request) -> httpx.Response:
        nonlocal issued
        issued += 1
        return httpx.Response(200, json={"access_token": f"t{issued}", "expires_in": 30})

    exchange = exchange_over(short)
    assert (await exchange.token_for(NOTES)).access_token == "t1"
    assert (await exchange.token_for(NOTES)).access_token == "t2"


async def test_the_time_left_is_whole_seconds_and_never_negative(igni):
    now = [100.0]
    exchange = ServiceTokenExchange(
        api_url=lambda: API_URL,
        session=lambda: IgniSession(access_token=SESSION),
        clock=lambda: now[0],
        transport=igni.transport(),
    )
    token = await exchange.token_for(NOTES)
    now[0] += 0.5
    assert exchange.remaining(token) == 599
    now[0] += 10_000
    assert exchange.remaining(token) == 0
    assert exchange.remaining(CachedToken(access_token="t", service_url=NOTES, expires_at=-5)) == 0


async def test_forgetting_makes_the_next_ask_exchange_again(igni):
    exchange = ServiceTokenExchange(
        api_url=lambda: API_URL,
        session=lambda: IgniSession(access_token=SESSION),
        transport=igni.transport(),
    )
    await exchange.token_for(NOTES)
    exchange.forget()
    await exchange.token_for(NOTES)
    assert igni.tokens_issued == 2


async def test_asks_wait_for_a_session_renewal_under_way_before_reading_it(igni):
    order: list[str] = []

    async def ready() -> None:
        order.append("ready")

    def session() -> IgniSession:
        order.append("session")
        return IgniSession(access_token=SESSION)

    exchange = ServiceTokenExchange(
        api_url=lambda: API_URL, session=session, ready=ready, transport=igni.transport()
    )
    await exchange.token_for(NOTES)
    assert order[:2] == ["ready", "session"]


async def test_an_unconfigured_igni_server_is_not_signed_in():
    exchange = ServiceTokenExchange(
        api_url=lambda: None, session=lambda: IgniSession(access_token=SESSION)
    )
    with pytest.raises(NotSignedIn, match="no igni server is configured"):
        await exchange.token_for(NOTES)


# ── What a page is told ────────────────────────────────────────────


class NoOrigins:
    def service_url_for(self, extension_id: str) -> str | None:
        return None


class OneOrigin:
    def service_url_for(self, extension_id: str) -> str | None:
        return NOTES if extension_id == "acme.notes" else None


async def test_a_page_with_no_service_is_told_ok_false_with_the_sentence(igni):
    exchange = ServiceTokenExchange(
        api_url=lambda: API_URL,
        session=lambda: IgniSession(access_token=SESSION),
        transport=igni.transport(),
    )
    reply = await PageServiceTokens(origins=NoOrigins(), exchange=exchange).reply_for("acme.x")  # type: ignore[arg-type]
    assert reply.ok is False
    assert reply.error == "acme.x has no service its page may reach"
    assert igni.tokens_issued == 0


async def test_a_page_asking_while_signed_out_is_told_ok_false():
    exchange = ServiceTokenExchange(api_url=lambda: API_URL, session=lambda: None)
    reply = await PageServiceTokens(origins=OneOrigin(), exchange=exchange).reply_for("acme.notes")  # type: ignore[arg-type]
    assert (reply.ok, reply.error) == (False, "nobody is signed in to igni")


async def test_a_page_whose_exchange_fails_is_told_ok_false_with_igni_s_reason(igni):
    igni.token_status = 400
    exchange = ServiceTokenExchange(
        api_url=lambda: API_URL,
        session=lambda: IgniSession(access_token=SESSION),
        transport=igni.transport(),
    )
    reply = await PageServiceTokens(origins=OneOrigin(), exchange=exchange).reply_for("acme.notes")  # type: ignore[arg-type]
    assert reply.ok is False
    assert reply.error is not None and "igni refused the exchange (400)" in reply.error


async def test_a_page_with_a_token_is_told_how_long_it_lasts(igni):
    exchange = ServiceTokenExchange(
        api_url=lambda: API_URL,
        session=lambda: IgniSession(access_token=SESSION),
        clock=lambda: 50.0,
        transport=igni.transport(),
    )
    reply = await PageServiceTokens(origins=OneOrigin(), exchange=exchange).reply_for("acme.notes")  # type: ignore[arg-type]
    assert (reply.ok, reply.service_url, reply.expires_in) == (True, NOTES, 600)
    assert reply.access_token == f"token-1-for-{NOTES}"


# ── The listener's own life ────────────────────────────────────────


async def test_the_url_is_not_known_before_the_listener_starts(endpoint):
    with pytest.raises(RuntimeError, match="not listening"):
        _ = endpoint.url


async def test_starting_twice_keeps_one_listener_even_when_asked_together(endpoint):
    await asyncio.gather(endpoint.start(), endpoint.start(), endpoint.start())
    first = endpoint.url
    await endpoint.start()
    assert endpoint.url == first


async def test_stopping_closes_the_port_drops_every_grant_and_may_repeat(endpoint):
    env = await endpoint.grant("acme.notes", NOTES)
    port = int(env[ENV_TOKEN_URL].split(":")[2].split("/")[0])
    await endpoint.stop()
    await endpoint.stop()
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)
    with pytest.raises(RuntimeError):
        _ = endpoint.url
    # A restart starts with no grant: the old secret is nothing now.
    env2 = await endpoint.grant("acme.ledger", "https://ledger.example.com")
    answer = await raw(endpoint, request_for(endpoint, env[ENV_TOKEN_SECRET]))
    assert answer.startswith(b"HTTP/1.1 401")
    assert env2[ENV_TOKEN_SECRET] != env[ENV_TOKEN_SECRET]


async def test_stopping_a_listener_that_never_started_is_harmless(endpoint):
    await endpoint.stop()


async def test_a_secret_the_installer_made_is_the_one_that_works(endpoint):
    env = await endpoint.grant("acme.notes", NOTES, secret="shared-with-the-host-channel")
    assert env[ENV_TOKEN_SECRET] == "shared-with-the-host-channel"
    answer = await raw(endpoint, request_for(endpoint, "shared-with-the-host-channel"))
    assert answer.startswith(b"HTTP/1.1 200")


async def test_every_grant_gets_a_secret_of_its_own(endpoint):
    first = await endpoint.grant("acme.a", NOTES)
    second = await endpoint.grant("acme.b", NOTES)
    assert first[ENV_TOKEN_SECRET] != second[ENV_TOKEN_SECRET]


async def test_the_other_grants_still_work_after_one_is_revoked(endpoint):
    a = await endpoint.grant("acme.a", NOTES)
    b = await endpoint.grant("acme.b", "https://ledger.example.com")
    endpoint.revoke("acme.a")
    endpoint.revoke("acme.never-granted")
    assert (await raw(endpoint, request_for(endpoint, a[ENV_TOKEN_SECRET]))).startswith(
        b"HTTP/1.1 401"
    )
    assert (await raw(endpoint, request_for(endpoint, b[ENV_TOKEN_SECRET]))).startswith(
        b"HTTP/1.1 200"
    )


# ── The session on disk ────────────────────────────────────────────


def jwt_with(**claims: Any) -> str:
    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


async def test_the_session_is_read_from_disk_at_every_ask(tmp_path: Path):
    path = tmp_path / "credentials.json"
    session = CredentialsSession(str(path))
    assert session() is None

    CredentialsStore(str(path)).save(Credentials.new(jwt_with(org="org-7"), "ada@example.com"))
    signed = session()
    assert signed is not None
    assert signed.org_id == "org-7"
    assert signed.access_token == jwt_with(org="org-7")

    CredentialsStore(str(path)).save(Credentials.new(jwt_with(), "grace@example.com"))
    other = session()
    assert other is not None and other.org_id is None

    CredentialsStore(str(path)).clear()
    assert session() is None


async def test_a_session_file_is_always_ready(tmp_path: Path):
    await CredentialsSession(str(tmp_path / "credentials.json")).ready()
