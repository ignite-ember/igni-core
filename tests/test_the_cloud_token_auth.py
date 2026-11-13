"""``CloudTokenAuth``: the bearer token igni server's gateway is sent, read when it is sent.

``test_the_model_carries_the_current_cloud_token.py`` drives a model
through it. This file pins the auth on its own: when it sends no
header, when a refusal is tried again and when it is not, and that an
async request waits for the layer's credential source before it reads.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from igni_core.core.auth.credentials import (
    Credentials,
    CredentialSource,
    CredentialsStore,
)
from igni_core.core.auth.http_auth import CloudTokenAuth
from igni_core.core.auth.schemas import LoadCredentialsResult
from igni_core.core.layer import Layer, layers

URL = "https://igni.test/v1/chat/completions"


class Gateway:
    """Answers 401 to every token but the ones it accepts; records what it was sent."""

    def __init__(self, *accepted: str, on_request=None) -> None:
        self.accepted = set(accepted)
        self.seen: list[str | None] = []
        self.on_request = on_request

    def handler(self, request: httpx.Request) -> httpx.Response:
        header = request.headers.get("Authorization")
        self.seen.append(header)
        if self.on_request:
            self.on_request(len(self.seen))
        bearer = (header or "").removeprefix("Bearer ")
        return httpx.Response(200 if bearer in self.accepted else 401)

    async def call(self, auth: CloudTokenAuth, headers: dict[str, str] | None = None) -> int:
        transport = httpx.MockTransport(self.handler)
        async with httpx.AsyncClient(transport=transport, auth=auth) as client:
            return (await client.post(URL, headers=headers)).status_code

    def call_sync(self, auth: CloudTokenAuth) -> int:
        with httpx.Client(transport=httpx.MockTransport(self.handler), auth=auth) as client:
            return client.post(URL).status_code


@pytest.fixture
def file(tmp_path: Path) -> Path:
    return tmp_path / "credentials.json"


def _sign_in(file: Path, token: str) -> None:
    CredentialsStore(str(file)).save(Credentials.new(token, "ada@acme.test"))


class TestWhatIsSent:
    async def test_the_stored_token_as_a_bearer(self, file):
        _sign_in(file, "tok-1")
        gateway = Gateway("tok-1")
        assert await gateway.call(CloudTokenAuth(str(file))) == 200
        assert gateway.seen == ["Bearer tok-1"]

    async def test_nobody_signed_in_sends_no_authorization_and_leaves_the_callers_alone(self, file):
        gateway = Gateway()
        auth = CloudTokenAuth(str(file))
        assert await gateway.call(auth) == 401
        assert gateway.seen == [None]
        gateway.seen.clear()
        await gateway.call(auth, headers={"Authorization": "Bearer theirs"})
        assert gateway.seen == ["Bearer theirs"]

    async def test_an_expired_sign_in_is_nobody(self, file):
        file.write_text(
            json.dumps(
                {
                    "access_token": "old",
                    "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                }
            )
        )
        gateway = Gateway("old")
        assert await gateway.call(CloudTokenAuth(str(file))) == 401
        assert gateway.seen == [None]

    async def test_the_token_is_read_again_for_each_request(self, file):
        gateway = Gateway("one", "two")
        auth = CloudTokenAuth(str(file))
        _sign_in(file, "one")
        await gateway.call(auth)
        _sign_in(file, "two")
        await gateway.call(auth)
        assert gateway.seen == ["Bearer one", "Bearer two"]

    def test_a_client_that_is_not_async_sends_it_too(self, file):
        _sign_in(file, "tok-1")
        gateway = Gateway("tok-1")
        assert gateway.call_sync(CloudTokenAuth(str(file))) == 200
        assert gateway.seen == ["Bearer tok-1"]


class TestARefusal:
    async def test_the_same_token_refused_is_not_sent_again(self, file):
        _sign_in(file, "stale")
        gateway = Gateway()
        assert await gateway.call(CloudTokenAuth(str(file))) == 401
        assert gateway.seen == ["Bearer stale"]

    async def test_a_token_rotated_meanwhile_is_tried_once_more(self, file):
        _sign_in(file, "old")
        gateway = Gateway("new", on_request=lambda n: _sign_in(file, "new") if n == 1 else None)
        assert await gateway.call(CloudTokenAuth(str(file))) == 200
        assert gateway.seen == ["Bearer old", "Bearer new"]

    async def test_the_retry_happens_once_even_when_it_is_refused_too(self, file):
        _sign_in(file, "t0")
        gateway = Gateway(on_request=lambda n: _sign_in(file, f"t{n}"))
        assert await gateway.call(CloudTokenAuth(str(file))) == 401
        assert gateway.seen == ["Bearer t0", "Bearer t1"]

    async def test_a_token_rotated_during_a_request_that_succeeded_is_not_sent_again(self, file):
        _sign_in(file, "old")
        gateway = Gateway("old", on_request=lambda n: _sign_in(file, "new"))
        assert await gateway.call(CloudTokenAuth(str(file))) == 200
        assert gateway.seen == ["Bearer old"]

    async def test_a_refusal_with_nobody_signed_in_is_not_retried(self, file):
        gateway = Gateway()
        assert await gateway.call(CloudTokenAuth(str(file))) == 401
        assert gateway.seen == [None]

    def test_a_retry_is_also_made_by_a_client_that_is_not_async(self, file):
        _sign_in(file, "old")
        gateway = Gateway("new", on_request=lambda n: _sign_in(file, "new") if n == 1 else None)
        assert gateway.call_sync(CloudTokenAuth(str(file))) == 200
        assert gateway.seen == ["Bearer old", "Bearer new"]


class _Renewing(CredentialSource):
    """A layer's source whose token only exists once ``ready`` has run."""

    def __init__(self) -> None:
        self.token: str | None = None
        self.order: list[str] = []

    def load(self) -> LoadCredentialsResult:
        if self.token is None:
            return LoadCredentialsResult(ok=False, reason="no_file")
        return LoadCredentialsResult(ok=True, creds=Credentials.new(self.token, "vm@acme.test"))

    async def ready(self) -> None:
        self.order.append("ready")
        self.token = "renewed"


class TestInAVm:
    async def test_a_request_waits_for_the_renewal_under_way_before_reading_the_token(self):
        source = _Renewing()

        class _Layer(Layer):
            name = "test"

            def credentials(self) -> CredentialSource:
                return source

        layers.install(_Layer())
        gateway = Gateway("renewed", on_request=lambda n: source.order.append("sent"))
        assert await gateway.call(CloudTokenAuth()) == 200
        assert gateway.seen == ["Bearer renewed"]
        assert source.order == ["ready", "sent"]
