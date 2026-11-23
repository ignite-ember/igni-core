"""In a person's VM, a page host's page gets a token for its own service.

``docs/design/local-cloud-switch.md`` §15.5. ``bridge.service`` asks for
``extension_service_token``; the desktop's window forwards it with the
frame's extension as ``caller``, on a stream the node agent marked as a
page host. The VM's igni exchanges its own token — the one it keeps from
its credential (``IGNI_ACCESS_TOKEN_FILE``) — for a token whose audience is
the service recorded on that extension's origin. Driven through the
admission gate and the pool's own router with the cloud layer installed,
as the orchestrator drives them.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from igni_core.backend.message_admission import MessageAdmission
from igni_core.backend.pool_replier import PoolReplier
from igni_core.backend.pool_rpcs import PageTokenRpc, PoolRpcRouter
from igni_core.extensions.origin import ExtensionOrigins
from igni_core.extensions.registry import ExtensionRegistry, ExtensionState
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.extensions.service_token import (
    CredentialsSession,
    PageServiceTokens,
    ServiceTokenExchange,
)
from igni_core.protocol import messages as msg
from tests.host_model_helpers import CapturingTransport, manifest

SERVICE = "https://reviews.acme.test"
EXT = "acme.reviewer"


def _token(**claims: Any) -> str:
    def part(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    return f"{part(b'{}')}.{part(json.dumps(claims).encode())}.sig"


class Igni:
    """igni server's token endpoint: every exchange it was asked for."""

    def __init__(self) -> None:
        self.forms: list[dict[str, str]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.forms.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(
            200,
            json={
                "access_token": f"person-for-the-service-{len(self.forms)}",
                "expires_in": 600,
                "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
            },
        )


class Rig:
    def __init__(self, origins: ExtensionOrigins, igni: Igni) -> None:
        registry = ExtensionRegistry()
        registry.install(manifest(EXT), state=ExtensionState.READY)
        self.admission = MessageAdmission(ExtensionRpc(registry))
        self.windows = CapturingTransport()
        self.router = PoolRpcRouter(PoolReplier(self.windows))
        exchange = ServiceTokenExchange(
            api_url=lambda: "https://igni.test",
            session=CredentialsSession(),
            transport=httpx.MockTransport(igni.handle),
        )
        self.router.mount(PageTokenRpc(PageServiceTokens(origins=origins, exchange=exchange)))

    async def ask(self, request_id: str, *, refresh: bool = False) -> msg.RPCResponse:
        request = msg.RPCRequest(
            id=request_id,
            method="extension_service_token",
            caller=EXT,
            args={"refresh": True} if refresh else {},
        )
        request.mark_from("ws-desktop")
        request.mark_page_host()
        refusal = self.admission.refusal(request)
        assert refusal is None, refusal
        assert self.router.handles(request)
        await self.router.handle(request)
        (answer,) = [m for m in self.windows.of(msg.RPCResponse) if m.id == request_id]
        assert answer.to_client == "ws-desktop"
        return answer


@pytest.fixture
def vm_token(cloud_layer: Any, tmp_path: Path, monkeypatch) -> str:
    path = tmp_path / "token"
    monkeypatch.setenv("IGNI_ACCESS_TOKEN_FILE", str(path))
    own = _token(sub="person-7", kind="person", org="org-acme", exp=int(time.time()) + 900)
    path.write_text(own)
    return own


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "ui"
    root.mkdir()
    (root / "index.html").write_text("page")
    return root


@pytest.fixture
async def origins():
    served = ExtensionOrigins()
    yield served
    await served.stop_all()


async def test_a_page_host_s_token_is_exchanged_from_the_vm_s_own(vm_token, origins, bundle):
    await origins.serve(EXT, bundle, service_url=SERVICE)
    igni = Igni()
    rig = Rig(origins, igni)

    first = await rig.ask("t1")
    again = await rig.ask("t2")

    assert first.error is None and first.result["ok"] is True
    assert first.result["service_url"] == SERVICE
    assert first.result["access_token"] == again.result["access_token"]
    assert igni.forms == [
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": vm_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": SERVICE,
            "organisation": "org-acme",
        }
    ], "one exchange, the VM's own token as the subject, the service as the audience"


async def test_refresh_exchanges_again(vm_token, origins, bundle):
    await origins.serve(EXT, bundle, service_url=SERVICE)
    igni = Igni()
    rig = Rig(origins, igni)

    first = await rig.ask("t1")
    fresh = await rig.ask("t2", refresh=True)

    assert len(igni.forms) == 2
    assert fresh.result["access_token"] != first.result["access_token"]


async def test_an_extension_with_no_service_gets_no_token(vm_token, origins, bundle):
    await origins.serve(EXT, bundle)
    igni = Igni()

    answer = await Rig(origins, igni).ask("t1")

    assert answer.error is None and answer.result["ok"] is False
    assert answer.result["error"]
    assert igni.forms == []
