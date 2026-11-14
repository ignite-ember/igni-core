"""``switch_model`` to a model the registry lacks answers, naming the model.

The contract says it "Fails when the model is not in the registry". A
caller of an RPC waits for an ``rpc_response`` carrying its id, so a
failure that sent nothing would leave it to its timeout. Driven through
the real dispatcher, RPC table and backend, not the handler alone.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from igni_core.backend.message_dispatcher import MessageDispatcher
from igni_core.backend.rpc_router import RpcRouter
from igni_core.backend.server import BackendServer
from igni_core.core.config.model_entry import ModelRegistryEntry
from igni_core.core.config.settings_loader import SettingsLoader
from igni_core.protocol import messages as msg
from igni_core.protocol.schemas.rpc import RpcErrorCode


class _Transport:
    def __init__(self) -> None:
        self.sent: list[msg.Message] = []

    async def send(self, message: msg.Message) -> None:
        self.sent.append(message)


@pytest.fixture
def wire(tmp_path: Path) -> tuple[MessageDispatcher, _Transport, BackendServer]:
    settings = SettingsLoader.load(project_dir=tmp_path)
    settings.models.registry["fast"] = ModelRegistryEntry(
        model_id="m", provider="openai_like", url="http://127.0.0.1:9/v1", api_key="k"
    )
    settings.models.default = "fast"
    backend = BackendServer(settings, project_dir=tmp_path)
    transport = _Transport()
    router = RpcRouter(backend=backend, login=MagicMock(), push=MagicMock())
    dispatcher = MessageDispatcher(
        backend=backend, transport=transport, rpc_table=router.build_table(), queue=[], login=None
    )
    return dispatcher, transport, backend


async def test_an_unknown_model_is_answered_with_an_error_naming_it(wire) -> None:
    dispatcher, transport, backend = wire

    await dispatcher.dispatch(
        msg.RPCRequest(id="r1", method="switch_model", args={"model_name": "no-such-model"})
    )

    (reply,) = transport.sent
    assert isinstance(reply, msg.RPCResponse)
    assert reply.id == "r1"
    assert reply.error and "no-such-model" in reply.error
    assert reply.code is RpcErrorCode.REFUSED
    assert backend.settings.models.default == "fast"
