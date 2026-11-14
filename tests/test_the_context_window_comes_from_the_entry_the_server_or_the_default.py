"""``ContextWindowResolver``: an explicit hint, else what the server says,
else 128k. The synchronous ``resolve`` never touches the network.

(Where the cloud token may go is pinned by
test_the_cloud_token_goes_only_to_igni_server.py.)
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from igni_core.core.config.context_window import (
    DEFAULT_CONTEXT_WINDOW,
    ContextWindowResolver,
)
from igni_core.core.config.model_entry import ModelRegistryEntry

_REAL = httpx.AsyncClient


class Server:
    """Stands in for ``httpx.AsyncClient``'s transport and records requests."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, respond: Any) -> None:
        self.requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            result = respond(request)
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(
            "httpx.AsyncClient", lambda **kw: _REAL(transport=httpx.MockTransport(handler))
        )


def _entry(**kw: Any) -> ModelRegistryEntry:
    return ModelRegistryEntry(model_id="m", url="https://llm.test/v1", **kw)


async def test_an_explicit_hint_wins_without_a_request(monkeypatch) -> None:
    server = Server(monkeypatch, lambda r: httpx.Response(200, json={"context_window": 1}))
    resolver = ContextWindowResolver()
    assert await resolver.aresolve("m", _entry(context_window=32_000)) == 32_000
    assert resolver.resolve("m", _entry(context_window=32_000)) == 32_000
    assert server.requests == []


def test_the_sync_resolve_never_asks_and_defaults() -> None:
    assert ContextWindowResolver().resolve("m", _entry()) == DEFAULT_CONTEXT_WINDOW
    assert ContextWindowResolver().resolve("m") == DEFAULT_CONTEXT_WINDOW


async def test_no_entry_or_no_url_defaults_without_a_request(monkeypatch) -> None:
    server = Server(monkeypatch, lambda r: httpx.Response(200, json={"context_window": 1}))
    resolver = ContextWindowResolver()
    assert await resolver.aresolve("m") == DEFAULT_CONTEXT_WINDOW
    assert await resolver.aresolve("m", ModelRegistryEntry(model_id="m")) == DEFAULT_CONTEXT_WINDOW
    assert server.requests == []


@pytest.mark.parametrize("key", ["context_window", "context_length", "max_model_len"])
async def test_each_known_key_is_read(monkeypatch, key: str) -> None:
    Server(monkeypatch, lambda r: httpx.Response(200, json={key: 8192}))
    assert await ContextWindowResolver().aresolve("m", _entry()) == 8192


async def test_the_url_is_the_entry_s_base_plus_models_and_the_id(monkeypatch) -> None:
    server = Server(monkeypatch, lambda r: httpx.Response(200, json={"context_window": 5}))
    entry = ModelRegistryEntry(model_id="m", url="https://llm.test/v1/")
    await ContextWindowResolver().aresolve("m", entry)
    assert str(server.requests[0].url) == "https://llm.test/v1/models/m"


async def test_a_success_is_cached_per_model(monkeypatch) -> None:
    server = Server(monkeypatch, lambda r: httpx.Response(200, json={"context_window": 5000}))
    resolver = ContextWindowResolver()
    assert await resolver.aresolve("m", _entry()) == 5000
    assert await resolver.aresolve("m", _entry()) == 5000
    assert resolver.resolve("m") == 5000
    assert len(server.requests) == 1
    await resolver.aresolve("other", _entry())
    assert len(server.requests) == 2


@pytest.mark.parametrize(
    "respond",
    [
        lambda r: httpx.Response(404),
        lambda r: httpx.Response(500, json={"context_window": 5}),
        lambda r: httpx.Response(200, json={"nothing": 1}),
        lambda r: httpx.Response(200, json=[1, 2]),
        lambda r: httpx.Response(200, json=7),
        lambda r: httpx.Response(200, json={"context_window": "lots"}),
        lambda r: httpx.Response(200, json={"context_window": None}),
        lambda r: httpx.Response(200, json={"context_window": 0}),
        lambda r: httpx.Response(200, json={"context_window": -1}),
        lambda r: httpx.Response(200, content=b"not json"),
        lambda r: httpx.ConnectError("down"),
        lambda r: httpx.ReadTimeout("slow"),
    ],
    ids=[
        "404",
        "500",
        "no-key",
        "list",
        "scalar",
        "non-numeric",
        "null",
        "zero",
        "negative",
        "not-json",
        "connect-error",
        "timeout",
    ],
)
async def test_every_failure_falls_back_to_the_default_and_caches_nothing(
    monkeypatch, respond
) -> None:
    Server(monkeypatch, respond)
    resolver = ContextWindowResolver()
    assert await resolver.aresolve("m", _entry()) == DEFAULT_CONTEXT_WINDOW
    assert resolver.resolve("m") == DEFAULT_CONTEXT_WINDOW
    assert resolver._cache == {}


async def test_an_entry_without_a_key_sends_no_authorization(monkeypatch) -> None:
    server = Server(monkeypatch, lambda r: httpx.Response(200, json={"context_window": 5}))
    await ContextWindowResolver().aresolve("m", _entry())
    assert "authorization" not in server.requests[0].headers


async def test_the_entry_s_own_key_is_sent_as_a_bearer(monkeypatch) -> None:
    server = Server(monkeypatch, lambda r: httpx.Response(200, json={"context_window": 5}))
    await ContextWindowResolver().aresolve("m", _entry(api_key="sk-own"))
    assert server.requests[0].headers["authorization"] == "Bearer sk-own"
