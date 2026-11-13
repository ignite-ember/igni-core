"""``ModelRegistry``: which model a name or its absence means, and where igni server's token goes.

The cases the neighbouring tests do not reach: the error text for a provider
nobody knows, a name carrying several colons, an empty name meaning the
default, the per-registry provider table, the bootstrap context window, and
the warning the person gets when their entry asked for igni server's token
and the registry would not send it.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from igni_core.core.auth.credentials import Credentials, CredentialsStore
from igni_core.core.config.llm_call_logger import LlmCallLogger
from igni_core.core.config.model_entry import ModelRegistryEntry
from igni_core.core.config.models import ModelRegistry
from igni_core.core.config.null_model import NoModelConfigured
from igni_core.core.config.provider_builders import ProviderClientBuilder
from igni_core.core.config.schemas import ModelsConfig
from igni_core.core.config.settings import Settings

TOKEN = "header.eyJzdWIiOiJ1In0.sig"  # noqa: S105 — a fixture value


def _settings(default: str = "", registry: dict | None = None, **rest) -> Settings:
    return Settings(models=ModelsConfig(default=default, registry=registry or {}), **rest)


class RecordingBuilder(ProviderClientBuilder):
    """A provider that records what the registry hands a build."""

    def __init__(self, endpoint: str | None) -> None:
        self.endpoint = endpoint
        self.built: list[dict] = []

    def build(
        self,
        entry: ModelRegistryEntry,
        *,
        cloud_token: str | None,
        llm_logger: LlmCallLogger,
        cloud_auth=None,
    ):
        self.built.append({"entry": entry, "token": cloud_token, "auth": cloud_auth})
        return NoModelConfigured.for_login_required()

    def endpoint_for(self, entry: ModelRegistryEntry) -> str | None:
        return self.endpoint


def _signed_in(tmp_path: Path, api_url: str, registry: dict) -> Settings:
    path = tmp_path / "credentials.json"
    CredentialsStore(str(path)).save(Credentials.new(TOKEN, "ada@acme.test"))
    settings = _settings("m", registry, api_url=api_url)
    settings.auth.credentials_file = str(path)
    return settings


class TestUnknownProvider:
    def test_the_error_lists_the_providers_there_are(self):
        settings = _settings("m", {"m": {"provider": "nonesuch", "model_id": "x"}})

        with pytest.raises(ValueError) as caught:
            ModelRegistry(settings).get_model()

        message = str(caught.value)
        assert message.startswith("Unknown provider 'nonesuch'. Available: ")
        assert "openai_like" in message
        assert message.endswith(".")

    def test_a_provider_registered_afterwards_is_found(self):
        settings = _settings("m", {"m": {"provider": "mine", "model_id": "x"}})
        registry = ModelRegistry(settings)
        registry.register_provider("mine", RecordingBuilder(None))

        assert isinstance(registry.get_model(), NoModelConfigured)


def test_a_provider_registered_on_one_registry_is_not_on_another():
    settings = _settings("m", {"m": {"provider": "mine", "model_id": "x"}})
    ModelRegistry(settings).register_provider("mine", RecordingBuilder(None))

    with pytest.raises(ValueError, match="Unknown provider 'mine'"):
        ModelRegistry(settings).get_model()


class TestNames:
    def test_a_name_splits_on_its_first_colon_only(self):
        registry = ModelRegistry(_settings())
        builder = RecordingBuilder(None)
        registry.register_provider("fake", builder)

        registry.get_model("fake:org/model:with:colons")

        assert builder.built[0]["entry"].provider == "fake"
        assert builder.built[0]["entry"].model_id == "org/model:with:colons"

    def test_an_empty_name_means_the_default(self):
        registry = ModelRegistry(_settings("m", {"m": {"provider": "fake", "model_id": "real"}}))
        builder = RecordingBuilder(None)
        registry.register_provider("fake", builder)

        registry.get_model("")

        assert builder.built[0]["entry"].model_id == "real"

    def test_without_a_default_the_first_registered_model_is_used(self):
        registry = ModelRegistry(
            _settings(
                "",
                {
                    "first": {"provider": "fake", "model_id": "one"},
                    "second": {"provider": "fake", "model_id": "two"},
                },
            )
        )
        builder = RecordingBuilder(None)
        registry.register_provider("fake", builder)

        registry.get_model()

        assert builder.built[0]["entry"].model_id == "one"

    def test_a_stored_default_is_matched_by_provider_colon_form_too(self):
        registry = ModelRegistry(_settings("fake:from-default"))
        builder = RecordingBuilder(None)
        registry.register_provider("fake", builder)

        registry.get_model()

        assert builder.built[0]["entry"].model_id == "from-default"

    def test_nothing_resolvable_is_the_login_placeholder_and_a_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="igni_core.core.config.models"):
            model = ModelRegistry(_settings()).get_model()

        assert isinstance(model, NoModelConfigured)
        assert "No model configured" in model.ERROR_MESSAGE
        assert "gone" not in model.ERROR_MESSAGE
        assert "No model configured — returning placeholder" in caplog.text

    def test_a_stale_default_warns_with_its_name(self, caplog):
        with caplog.at_level(logging.WARNING, logger="igni_core.core.config.models"):
            ModelRegistry(_settings("gone-model")).get_model()

        assert "'gone-model' is not in models.registry" in caplog.text


class TestTheContextWindowWithoutAModel:
    def test_it_is_the_configured_maximum(self):
        settings = _settings()
        settings.models.max_context_window = 123_456

        assert ModelRegistry(settings).get_context_window() == 123_456

    async def test_the_async_lookup_gives_the_same_maximum(self):
        settings = _settings()
        settings.models.max_context_window = 123_456

        assert await ModelRegistry(settings).aget_context_window() == 123_456

    async def test_the_async_lookup_prefers_the_entry_override(self):
        settings = _settings("m", {"m": {"model_id": "x", "context_window": 32_000}})

        assert await ModelRegistry(settings).aget_context_window("m") == 32_000


class TestTheCloudTokenIsWithheldFromOtherHosts:
    GATEWAY = "https://igni.example"

    def _registry(
        self, tmp_path: Path, endpoint: str | None
    ) -> tuple[ModelRegistry, RecordingBuilder]:
        entry = {"provider": "fake", "model_id": "x", "api_key": "cloud_token"}
        registry = ModelRegistry(_signed_in(tmp_path, self.GATEWAY, {"m": entry}))
        builder = RecordingBuilder(endpoint)
        registry.register_provider("fake", builder)
        return registry, builder

    def test_the_token_and_the_live_auth_go_to_the_servers_own_origin(self, tmp_path):
        registry, builder = self._registry(tmp_path, f"{self.GATEWAY}/v1")

        registry.get_model()

        assert builder.built[0]["token"] == TOKEN
        assert builder.built[0]["auth"] is not None

    def test_neither_goes_to_another_host_and_the_person_is_told(self, tmp_path, caplog):
        registry, builder = self._registry(tmp_path, "https://elsewhere.example/v1")

        with caplog.at_level(logging.WARNING, logger="igni_core.core.config.models"):
            registry.get_model()

        assert builder.built[0]["token"] is None
        assert builder.built[0]["auth"] is None
        assert "asks for the igni server token" in caplog.text
        assert "https://elsewhere.example/v1" in caplog.text
        assert self.GATEWAY in caplog.text
        assert TOKEN not in caplog.text

    def test_a_provider_with_no_endpoint_is_named_by_its_default(self, tmp_path, caplog):
        registry, builder = self._registry(tmp_path, None)

        with caplog.at_level(logging.WARNING, logger="igni_core.core.config.models"):
            registry.get_model()

        assert builder.built[0]["token"] is None
        assert "fake's default endpoint" in caplog.text

    def test_an_entry_that_never_wanted_the_token_gets_no_warning(self, tmp_path, caplog):
        settings = _signed_in(
            tmp_path,
            self.GATEWAY,
            {"m": {"provider": "fake", "model_id": "x", "api_key": "sk-own"}},
        )
        registry = ModelRegistry(settings)
        registry.register_provider("fake", RecordingBuilder("https://elsewhere.example/v1"))

        with caplog.at_level(logging.WARNING, logger="igni_core.core.config.models"):
            registry.get_model()

        assert "asks for the igni server token" not in caplog.text

    def test_not_signed_in_means_no_token_to_send_anywhere(self, tmp_path):
        settings = _settings(
            "m", {"m": {"provider": "fake", "model_id": "x"}}, api_url=self.GATEWAY
        )
        settings.auth.credentials_file = str(tmp_path / "missing.json")
        registry = ModelRegistry(settings)
        builder = RecordingBuilder(f"{self.GATEWAY}/v1")
        registry.register_provider("fake", builder)

        registry.get_model()

        assert builder.built[0]["token"] is None
