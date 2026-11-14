"""``CloudModelCatalog.refresh``: the session's best-effort pull of the key pool's models.

The catalogue is fetched with the signed-in token from the credentials
file and merged into the registry. Every failure is a quiet zero. The
first registry entry becomes the default only when nothing set one.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from igni_core.core.config.model_entry import ModelRegistryEntry
from igni_core.core.config.schemas import AuthConfig, ModelsConfig
from igni_core.core.config.settings import Settings
from igni_core.core.session.cloud_catalog import CloudModelCatalog

_REAL_CLIENT = httpx.Client
API = "https://igni.example"


def _settings(
    tmp_path: Path, *, token: str | None = "tok", api_url: str = API, **models
) -> Settings:
    creds = tmp_path / "credentials.json"
    if token is not None:
        creds.write_text(json.dumps({"access_token": token, "email": "a@b.test"}))
    return Settings(
        api_url=api_url,
        auth=AuthConfig(credentials_file=str(creds)),
        models=ModelsConfig(**models),
    )


def _serve(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    monkeypatch.setattr(
        "httpx.Client", lambda **_: _REAL_CLIENT(transport=httpx.MockTransport(respond))
    )
    return requests


def _models(*ids: str):
    return lambda _req: httpx.Response(200, json={"models": [{"id": i} for i in ids]})


def test_a_refresh_merges_the_servers_models_and_counts_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    requests = _serve(monkeypatch, _models("alpha", "beta"))

    added = CloudModelCatalog(settings).refresh()

    assert added == 2
    assert set(settings.models.registry) == {"alpha", "beta"}
    # The token came from the credentials file named in the settings.
    assert requests[0].headers["Authorization"] == "Bearer tok"
    assert str(requests[0].url) == f"{API}/v1/chat/models"


def test_a_second_refresh_adds_nothing_and_a_pinned_entry_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    pinned = ModelRegistryEntry.from_cloud_discovery(model_id="alpha", proxy_url="https://mine/v1")
    settings.models.registry["alpha"] = pinned
    _serve(monkeypatch, _models("alpha", "beta"))
    catalog = CloudModelCatalog(settings)

    assert catalog.refresh() == 1
    assert catalog.refresh() == 0

    assert settings.models.registry["alpha"] is pinned


def test_the_first_entry_becomes_the_default_when_nothing_chose_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    _serve(monkeypatch, _models("first", "second"))

    CloudModelCatalog(settings).refresh()

    assert settings.models.default == "first"


def test_a_default_somebody_chose_is_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, default="mine")
    _serve(monkeypatch, _models("first"))

    CloudModelCatalog(settings).refresh()

    assert settings.models.default == "mine"


def test_a_registry_that_was_already_filled_still_gets_its_default_after_a_failed_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    settings.models.registry["local"] = ModelRegistryEntry.from_cloud_discovery(
        model_id="local", proxy_url="https://x/v1"
    )
    _serve(monkeypatch, lambda _req: httpx.Response(503))

    assert CloudModelCatalog(settings).refresh() == 0

    assert settings.models.default == "local"


def test_an_empty_registry_after_a_failed_fetch_leaves_no_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    _serve(monkeypatch, lambda _req: httpx.Response(500))

    assert CloudModelCatalog(settings).refresh() == 0

    assert settings.models.default == ""
    assert settings.models.registry == {}


def test_signed_out_asks_nobody(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, token=None)
    requests = _serve(monkeypatch, _models("alpha"))

    assert CloudModelCatalog(settings).refresh() == 0

    assert requests == []
    assert settings.models.registry == {}


@pytest.mark.parametrize(
    "handler",
    [
        lambda _req: httpx.Response(401, json={"detail": "no"}),
        lambda _req: httpx.Response(200, json={"models": None}),
        lambda _req: httpx.Response(200, text="not json"),
        lambda _req: httpx.Response(200, json={"models": []}),
    ],
    ids=["non-200", "bad-shape", "not-json", "empty-list"],
)
def test_every_failed_fetch_is_a_quiet_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handler
) -> None:
    settings = _settings(tmp_path)
    _serve(monkeypatch, handler)

    assert CloudModelCatalog(settings).refresh() == 0
    assert settings.models.registry == {}


def test_an_unreachable_server_is_a_quiet_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    settings = _settings(tmp_path)
    _serve(monkeypatch, down)

    assert CloudModelCatalog(settings).refresh() == 0


def test_no_server_named_asks_nobody(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, api_url="")
    requests = _serve(monkeypatch, _models("alpha"))

    assert CloudModelCatalog(settings).refresh() == 0
    assert requests == []


def test_a_failed_fetch_says_why_in_the_debug_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(tmp_path)
    _serve(monkeypatch, lambda _req: httpx.Response(401))

    with caplog.at_level(logging.DEBUG, logger="igni_core.core.session.cloud_catalog"):
        CloudModelCatalog(settings).refresh()

    skipped = [r.getMessage() for r in caplog.records if "refresh skipped" in r.getMessage()]
    assert skipped == ["cloud_models: refresh skipped (reason=http_error, detail=HTTP 401)"]


def test_a_signed_out_refresh_logs_the_reason_not_a_generic_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(tmp_path, token=None)
    _serve(monkeypatch, _models("alpha"))

    with caplog.at_level(logging.DEBUG, logger="igni_core.core.session.cloud_catalog"):
        CloudModelCatalog(settings).refresh()

    skipped = [r.getMessage() for r in caplog.records if "refresh skipped" in r.getMessage()]
    assert skipped == ["cloud_models: refresh skipped (reason=no_token, detail=None)"]
