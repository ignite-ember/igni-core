"""In the cloud, only a connection marked as a page host speaks for an extension.

``docs/design/local-cloud-switch.md`` §15.2. Every client of a VM's igni
is the producer, through the gateway. The one that frames extension pages
— the person's desktop — asks the gateway for a page-host stream
(``/v1/connect?pages=1``), and the node agent marks it
``X-Igni-Gateway: pages``, as it marks the gateway's own stream
``control``. A message naming an extension (``caller``) is honoured only
on a page host's connection; on any other — a program, the control stream
— it is refused before any handler hears it. Past the mark,
``authorise_caller`` decides what the page may reach, as on the desktop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from igni_cloud.layer import CloudLayer

from igni_core.backend.pool_rpcs import PageTokenRpc
from igni_core.core.layer import Layer, layers
from igni_core.extensions.rpc import ExtensionRpc
from igni_core.extensions.service_token import PageTokenReply
from igni_core.protocol import messages as msg
from tests.host_model_helpers import HostRig

MARK_REFUSAL = "only a connection that hosts extension pages speaks for an extension"


class PageHostsOnly(Layer):
    """The cloud layer's answers to the extension seams, on a desktop backend."""

    def connections_speak_for_extensions(self) -> bool:
        return CloudLayer().connections_speak_for_extensions()

    def extension_calls_need_page_mark(self) -> bool:
        return CloudLayer().extension_calls_need_page_mark()


class Tokens:
    """A page's token exchange that always has a token."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def reply_for(self, extension_id: str, *, refresh: bool = False) -> PageTokenReply:
        self.asked.append(extension_id)
        return PageTokenReply(
            ok=True, access_token="t", expires_in=600, service_url="https://svc.test"
        )


def test_the_cloud_layer_asks_for_the_mark_and_the_desktop_does_not():
    assert CloudLayer().connections_speak_for_extensions() is True
    assert CloudLayer().extension_calls_need_page_mark() is True
    assert CloudLayer().serves_page_files() is True
    assert Layer().extension_calls_need_page_mark() is False
    assert Layer().serves_page_files() is False


@pytest.fixture
async def rig(tmp_path: Path, monkeypatch) -> Any:
    layers.install(PageHostsOnly())
    rig = await HostRig(tmp_path).start()
    rig.reached = []  # type: ignore[attr-defined]

    assert rig.model is not None
    answer_page = rig.model.handle_page

    async def handle_page(caller: str, args: dict[str, Any]) -> Any:
        rig.reached.append(("host_model", caller))  # type: ignore[attr-defined]
        return await answer_page(caller, args)

    async def dispatch(self: ExtensionRpc, *, caller: str | None, method: str, args: Any) -> Any:
        rig.reached.append((method, caller))  # type: ignore[attr-defined]
        return {"hits": 1}

    assert rig.model is not None and rig.orchestrator is not None
    monkeypatch.setattr(rig.model, "handle_page", handle_page)
    monkeypatch.setattr(ExtensionRpc, "dispatch", dispatch)
    rig.tokens = Tokens()  # type: ignore[attr-defined]
    rig.orchestrator.mount(PageTokenRpc(rig.tokens))  # type: ignore[arg-type,attr-defined]
    yield rig
    await rig.stop()


def _requests(page: str) -> list[msg.RPCRequest]:
    return [
        msg.RPCRequest(id="h", method="host_model", caller=page, args={"op": "get"}),
        msg.RPCRequest(id="t", method="extension_service_token", caller=page),
        msg.RPCRequest(id="x", method="ext.acme.reviewer.search", caller=page),
    ]


async def _send(rig: Any, requests: list[msg.RPCRequest], mark: str | None) -> dict[str, Any]:
    for request in requests:
        request.mark_from("ws-desktop")
        if mark == "control":
            request.mark_control()
        elif mark == "pages":
            request.mark_page_host()
        await rig.orchestrator.dispatch(request)
    return {m.id: m for m in rig.windows.of(msg.RPCResponse)}


@pytest.mark.parametrize("mark", [None, "control"], ids=["unmarked", "control stream"])
async def test_a_caller_on_a_connection_without_the_page_mark_is_refused_before_any_handler(
    rig: Any, mark: str | None
):
    page = rig.install("acme.reviewer", host=["sessions"], rpc=["ext.acme.reviewer.*"])

    answers = await _send(rig, _requests(page), mark)

    for request_id, method in (
        ("h", "host_model"),
        ("t", "extension_service_token"),
        ("x", "ext.acme.reviewer.search"),
    ):
        assert answers[request_id].error == f"{method}: {MARK_REFUSAL}", request_id
        assert answers[request_id].to_client == "ws-desktop"
    assert rig.reached == [] and rig.tokens.asked == []


async def test_a_page_host_s_caller_reaches_the_host_model_its_token_and_its_methods(rig: Any):
    page = rig.install("acme.reviewer", host=["sessions"], rpc=["ext.acme.reviewer.*"])

    answers = await _send(rig, _requests(page), "pages")

    assert answers["h"].error is None, answers["h"].error
    assert answers["t"].error is None and answers["t"].result["ok"] is True
    assert answers["x"].error is None and answers["x"].result == {"hits": 1}
    assert ("host_model", page) in rig.reached
    assert ("ext.acme.reviewer.search", page) in rig.reached
    assert rig.tokens.asked == [page]
    assert all(answers[i].to_client == "ws-desktop" for i in "htx"), "to the asker alone"


async def test_a_page_host_still_speaks_only_for_an_installed_extension(rig: Any):
    answers = await _send(rig, _requests("acme.absent"), "pages")

    for request_id in "htx":
        assert "no extension 'acme.absent' is installed" in (answers[request_id].error or "")
    assert rig.reached == []


async def test_a_page_host_s_page_never_reaches_a_pool_level_call(rig: Any):
    page = rig.install("acme.reviewer", host=["sessions"])
    attach = msg.RPCRequest(id="a", method="attach_session", caller=page, args={"session_id": "s1"})

    answers = await _send(rig, [attach], "pages")

    assert answers["a"].error and MARK_REFUSAL not in answers["a"].error
    assert "attach_session" in answers["a"].error
