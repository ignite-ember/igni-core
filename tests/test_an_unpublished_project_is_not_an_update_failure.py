"""PyPI answering 404 for igni-core means no release is published yet.

That is not a failed check: no update is offered, the RPC answers
``null`` rather than failing, and nothing is logged above debug. Other
HTTP failures keep being failures.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from igni_core.backend.rpc_handlers.models import ModelsRpcHandler
from igni_core.core.config.settings import Settings
from igni_core.core.utils.update_checker import UpdateChecker, UpdateError
from igni_core.protocol.rpc import RpcMethod


def _client(status: int) -> AsyncMock:
    request = httpx.Request("GET", "https://pypi.org/pypi/igni-core/json")
    response = httpx.Response(status, request=request)
    reply = MagicMock()
    reply.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("status", request=request, response=response)
    )
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=reply)
    return client


def _checker(tmp_path: Path) -> UpdateChecker:
    return UpdateChecker(
        settings=Settings(update_check_ttl=86400, air_gapped=False),
        cache_path=tmp_path / ".update-check",
    )


async def test_a_404_means_nothing_is_published_and_says_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    with patch("igni_core.core.utils.update_checker.httpx.AsyncClient", return_value=_client(404)):
        info = await _checker(tmp_path).check()

    assert info.available is False
    assert info.error is None
    assert [r for r in caplog.records if r.levelno >= logging.INFO] == []


@pytest.mark.parametrize("status", [403, 500, 503])
async def test_any_other_status_is_still_a_failure(tmp_path: Path, status: int) -> None:
    with patch(
        "igni_core.core.utils.update_checker.httpx.AsyncClient", return_value=_client(status)
    ):
        info = await _checker(tmp_path).check()

    assert info.error is UpdateError.HTTP_STATUS


async def test_the_rpc_answers_null_for_a_404_and_logs_no_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from igni_core.backend.message_dispatcher import MessageDispatcher
    from igni_core.protocol import messages as msg

    monkeypatch.setenv("HOME", str(tmp_path))
    caplog.set_level(logging.WARNING)
    handler = ModelsRpcHandler(SimpleNamespace(backend=SimpleNamespace(settings=Settings())))  # type: ignore[arg-type]
    sent: list[msg.Message] = []

    class Transport:
        async def send(self, message: msg.Message) -> None:
            sent.append(message)

    dispatcher = MessageDispatcher(
        backend=MagicMock(),
        transport=Transport(),
        rpc_table={RpcMethod.CHECK_FOR_UPDATE.value: handler.methods()[RpcMethod.CHECK_FOR_UPDATE]},
        queue=[],
        login=None,
    )
    with patch("igni_core.core.utils.update_checker.httpx.AsyncClient", return_value=_client(404)):
        await dispatcher.dispatch(msg.RPCRequest(id="u1", method="check_for_update", args={}))

    (reply,) = sent
    assert isinstance(reply, msg.RPCResponse)
    assert reply.error is None and reply.result is None
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
