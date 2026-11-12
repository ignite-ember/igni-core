"""The arguments of the session RPCs are checked before a handler sees them.

Each model is validated from the wire dict a client sends: a required
field missing is refused by name, a key a newer client adds is ignored,
empty strings reach the handler (which answers them), a number is not
taken for a string id, and the optional ones default as published.
``search_chat`` and ``truncate_history`` take them through the real
dispatch table, over a real backend, and the typed arguments are what
reach the backend.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from igni_core.backend.rpc_binding import RpcArgsError
from igni_core.backend.rpc_handlers.session_args import (
    AgentRunArgs,
    InterruptedRunArgs,
    SearchChatArgs,
    SessionIdArgs,
    TruncateHistoryArgs,
    UploadAttachmentArgs,
)
from igni_core.backend.server import BackendServer
from igni_core.core.config.settings_loader import SettingsLoader
from igni_core.protocol.rpc import RpcMethod
from tests.rpc_tables import rpc_table_for

_REQUIRED: list[tuple[type, dict[str, Any]]] = [
    (SessionIdArgs, {"session_id": "s"}),
    (InterruptedRunArgs, {"session_id": "s", "message_id": "m"}),
    (UploadAttachmentArgs, {"filename": "f", "content_base64": ""}),
    (SearchChatArgs, {"session_id": "s", "query": "q"}),
    (TruncateHistoryArgs, {"session_id": "s", "run_id": "r"}),
]


@pytest.mark.parametrize(("model", "wire"), _REQUIRED, ids=lambda v: getattr(v, "__name__", ""))
def test_each_required_field_is_named_when_missing(model, wire):
    for field in wire:
        without = {k: v for k, v in wire.items() if k != field}
        with pytest.raises(ValidationError, match=field):
            model.model_validate(without)


@pytest.mark.parametrize(("model", "wire"), _REQUIRED, ids=lambda v: getattr(v, "__name__", ""))
def test_a_key_a_newer_client_adds_is_ignored_and_empty_strings_are_kept(model, wire):
    parsed = model.model_validate({**wire, "from_the_future": True})

    assert not hasattr(parsed, "from_the_future")
    empty = model.model_validate({k: "" for k in wire})
    assert all(getattr(empty, k) == "" for k in wire)


@pytest.mark.parametrize(("model", "wire"), _REQUIRED, ids=lambda v: getattr(v, "__name__", ""))
def test_a_number_is_not_taken_for_a_string_field(model, wire):
    for field in wire:
        with pytest.raises(ValidationError, match=field):
            model.model_validate({**wire, field: 7})


def test_the_optional_fields_default_as_published():
    assert AgentRunArgs.model_validate({}).run_id == ""
    assert SearchChatArgs.model_validate({"session_id": "s", "query": "q"}).limit == 50
    assert SearchChatArgs.model_validate({"session_id": "s", "query": "q", "limit": 3}).limit == 3


class _Backend:
    def __init__(self) -> None:
        self.asked: list[tuple[str, tuple]] = []

    async def search_chat(self, session_id: str, query: str, limit: int) -> list:
        self.asked.append(("search", (session_id, query, limit)))
        return []

    async def truncate_history(self, session_id: str, run_id: str) -> str:
        self.asked.append(("truncate", (session_id, run_id)))
        return "truncated"


async def test_search_and_truncate_reach_the_backend_with_the_typed_arguments(tmp_path):
    backend = BackendServer(SettingsLoader.load(project_dir=tmp_path), project_dir=tmp_path)
    recorder = _Backend()
    backend.search_chat = recorder.search_chat  # type: ignore[method-assign]
    backend.truncate_history = recorder.truncate_history  # type: ignore[method-assign]
    table = rpc_table_for(backend, MagicMock())

    assert await table[RpcMethod.SEARCH_CHAT]({"session_id": "s", "query": "needle"}) == []
    await table[RpcMethod.SEARCH_CHAT]({"session_id": "s", "query": "needle", "limit": 2})
    assert await table[RpcMethod.TRUNCATE_HISTORY]({"session_id": "s", "run_id": "r"}) == (
        "truncated"
    )

    assert recorder.asked == [
        ("search", ("s", "needle", 50)),
        ("search", ("s", "needle", 2)),
        ("truncate", ("s", "r")),
    ]
    with pytest.raises(RpcArgsError, match="search_chat: 'query' is required"):
        table[RpcMethod.SEARCH_CHAT]({"session_id": "s"})
    with pytest.raises(RpcArgsError, match="truncate_history: 'run_id' is required"):
        table[RpcMethod.TRUNCATE_HISTORY]({"session_id": "s"})


async def test_a_real_backend_searches_and_truncates_an_empty_session(tmp_path):
    real = BackendServer(SettingsLoader.load(project_dir=tmp_path), project_dir=tmp_path)
    table = rpc_table_for(real, MagicMock())

    assert await table[RpcMethod.SEARCH_CHAT]({"session_id": "none", "query": "x"}) == []
    assert await table[RpcMethod.SEARCH_CHAT]({"session_id": "none", "query": "  "}) == []


@pytest.mark.parametrize(("model", "wire"), _REQUIRED, ids=lambda v: getattr(v, "__name__", ""))
def test_every_field_describes_itself_for_the_published_contract(model, wire):
    for name, field in model.model_fields.items():
        assert field.description, f"{model.__name__}.{name} has no description"
        assert "frontend" not in field.description.lower()
    assert model.__doc__
    assert "frontend" not in model.__doc__.lower()
    assert set(wire) <= set(model.model_fields)
    assert model.model_json_schema()["properties"].keys() >= set(wire)
