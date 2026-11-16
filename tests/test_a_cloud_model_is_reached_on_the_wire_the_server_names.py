"""A discovered model is reached on the API the server says it speaks.

igni server's ``GET /v1/chat/models`` answers ``{"id": ..., "api": ...}``:
``"anthropic"`` for a model it serves on ``POST /v1/messages``. The client
read only ``id`` and built every entry as ``openai_like`` at
``{api_url}/v1``, so an Anthropic-wire model was discovered, even picked as
the default, and every call to it answered 404 "served on /v1/messages"
(campaign R076, R194).
"""

from __future__ import annotations

import httpx
import pytest

from igni_core.core.config.cloud_models import CloudModelCatalogClient
from igni_core.core.config.model_entry import CLOUD_TOKEN_SENTINEL

_REAL_CLIENT = httpx.Client
API = "http://localhost:19010"


def _fetch(monkeypatch: pytest.MonkeyPatch, models: list[dict]):
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": models})

    monkeypatch.setattr(
        "httpx.Client", lambda **_: _REAL_CLIENT(transport=httpx.MockTransport(respond))
    )
    result = CloudModelCatalogClient(API, "tok").fetch()
    assert result.ok
    return {entry.model_id: entry for entry in result.entries}


def test_an_anthropic_model_is_an_anthropic_entry_at_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _fetch(monkeypatch, [{"id": "MiniMax-M3.1-Flash", "api": "anthropic"}])[
        "MiniMax-M3.1-Flash"
    ]
    assert entry.provider == "anthropic"
    # The Anthropic SDK adds /v1/messages itself.
    assert entry.url == API
    assert entry.api_key == CLOUD_TOKEN_SENTINEL
    assert entry.same_origin(entry.url, API)


def test_an_openai_model_and_one_with_no_api_stay_on_the_chat_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = _fetch(monkeypatch, [{"id": "gpt", "api": "openai"}, {"id": "plain"}])
    for name in ("gpt", "plain"):
        assert entries[name].provider == "openai_like"
        assert entries[name].url == f"{API}/v1"
