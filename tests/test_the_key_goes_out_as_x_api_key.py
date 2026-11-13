"""``AnthropicKeyAuth`` puts the provider's key in ``X-Api-Key``.

The SDK sends a key as ``Authorization: Bearer <key>``; gateways read
``X-Api-Key``. The key is moved on every request, ``Authorization`` is
left alone when it carries anything else, and with a cloud token the
token takes ``Authorization`` and is retried once on a 401 only if it
rotated.
"""

from __future__ import annotations

from pathlib import Path

import httpx2

from igni_core.core.auth.credentials import Credentials, CredentialsStore
from igni_core.core.auth.http_auth import CloudTokenAuth
from igni_core.core.config.anthropic_auth import AnthropicKeyAuth

Seen = list[tuple[str | None, str | None]]


def _client(auth: AnthropicKeyAuth, answers: list[int], seen: Seen) -> httpx2.AsyncClient:
    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.headers.get("authorization"), request.headers.get("x-api-key")))
        return httpx2.Response(answers[len(seen) - 1] if len(seen) <= len(answers) else 200)

    return httpx2.AsyncClient(transport=httpx2.MockTransport(answer), auth=auth)


def _creds(tmp_path: Path, token: str | None) -> tuple[CredentialsStore, CloudTokenAuth]:
    path = tmp_path / "credentials.json"
    store = CredentialsStore(str(path))
    if token:
        store.save(Credentials.new(token, "ada@acme.test"))
    return store, CloudTokenAuth(str(path))


async def test_the_bearer_key_moves_to_x_api_key() -> None:
    seen: Seen = []
    async with _client(AnthropicKeyAuth("sk-key"), [200], seen) as client:
        await client.post("http://gw.test/v1", headers={"Authorization": "Bearer sk-key"})
    assert seen == [(None, "sk-key")]


async def test_it_moves_the_key_on_the_sync_path_too() -> None:
    seen: Seen = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.headers.get("authorization"), request.headers.get("x-api-key")))
        return httpx2.Response(200)

    with httpx2.Client(transport=httpx2.MockTransport(answer), auth=AnthropicKeyAuth("k")) as c:
        c.post("http://gw.test/v1", headers={"Authorization": "Bearer k"})
    assert seen == [(None, "k")]


async def test_an_authorization_that_is_not_the_key_is_left_alone() -> None:
    seen: Seen = []
    async with _client(AnthropicKeyAuth("sk-key"), [200], seen) as client:
        await client.post("http://gw.test/v1", headers={"Authorization": "Bearer other"})
    assert seen == [("Bearer other", None)]


async def test_a_key_alone_is_never_retried() -> None:
    seen: Seen = []
    async with _client(AnthropicKeyAuth("sk-key"), [401, 200], seen) as client:
        resp = await client.post("http://gw.test/v1", headers={"Authorization": "Bearer sk-key"})
    assert resp.status_code == 401
    assert len(seen) == 1


async def test_a_401_with_the_same_token_is_not_retried(tmp_path: Path) -> None:
    _, cloud = _creds(tmp_path, "tok-1")
    seen: Seen = []
    auth = AnthropicKeyAuth("k", cloud)
    async with _client(auth, [401, 200], seen) as client:
        resp = await client.post("http://gw.test/v1", headers={"Authorization": "Bearer k"})
    assert resp.status_code == 401
    assert seen == [("Bearer tok-1", "k")]


async def test_a_non_401_failure_is_not_retried_even_if_the_token_rotated(tmp_path: Path) -> None:
    store, cloud = _creds(tmp_path, "tok-1")
    seen: Seen = []
    auth = AnthropicKeyAuth("k", cloud)
    async with _client(auth, [500, 200], seen) as client:
        store.save(Credentials.new("tok-2", "ada@acme.test"))
        resp = await client.post("http://gw.test/v1", headers={"Authorization": "Bearer k"})
    assert resp.status_code == 500
    assert len(seen) == 1


async def test_no_token_leaves_authorization_absent(tmp_path: Path) -> None:
    _, cloud = _creds(tmp_path, None)
    seen: Seen = []
    async with _client(AnthropicKeyAuth("k", cloud), [200], seen) as client:
        await client.post("http://gw.test/v1", headers={"Authorization": "Bearer k"})
    assert seen == [(None, "k")]


async def test_the_async_flow_waits_for_the_token_to_be_sendable() -> None:
    waited: list[str] = []

    class Cloud:
        async def ready(self) -> None:
            waited.append("ready")

        def token(self) -> str | None:
            waited.append("token")
            return "t"

    seen: Seen = []
    auth = AnthropicKeyAuth("k", Cloud())  # type: ignore[arg-type]
    async with _client(auth, [200], seen) as client:
        await client.post("http://gw.test/v1", headers={"Authorization": "Bearer k"})
    assert waited[0] == "ready"
    assert seen == [("Bearer t", "k")]
