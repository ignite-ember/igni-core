"""What each provider builder hands its client, and what it says when it cannot.

``OpenAILikeBuilder`` is reached through the registry elsewhere, and
``AnthropicBuilder`` by real-SDK tests; the arguments themselves (what is
sent only when set, the timeout and pool limits, Gemini's own name for the
output cap) and the refusals of an install without an SDK are pinned here.
"""

from __future__ import annotations

import logging

import pytest

from igni_core.core.auth.http_auth import CloudTokenAuth
from igni_core.core.config.llm_call_logger import LlmCallLogger
from igni_core.core.config.logging_model import LoggingModel
from igni_core.core.config.model_entry import CLOUD_TOKEN_SENTINEL, ModelRegistryEntry
from igni_core.core.config.provider_builders import (
    AnthropicBuilder,
    GeminiBuilder,
    OpenAILikeBuilder,
    ProviderClientBuilder,
)


def entry(**fields) -> ModelRegistryEntry:
    return ModelRegistryEntry(model_id="m-1", **fields)


class TestOpenAILike:
    def test_an_unset_temperature_and_max_tokens_are_not_sent(self):
        kwargs = OpenAILikeBuilder()._build_kwargs(entry(api_key="k"), cloud_token=None)

        assert "temperature" not in kwargs
        assert "max_tokens" not in kwargs
        assert kwargs["id"] == "m-1"
        assert kwargs["api_key"] == "k"

    def test_a_temperature_of_zero_is_sent(self):
        kwargs = OpenAILikeBuilder()._build_kwargs(
            entry(api_key="k", temperature=0, max_tokens=7), cloud_token=None
        )

        assert kwargs["temperature"] == 0
        assert kwargs["max_tokens"] == 7

    def test_no_url_leaves_the_base_url_to_the_sdk(self):
        absent = OpenAILikeBuilder()._build_kwargs(entry(api_key="k"), cloud_token=None)
        given = OpenAILikeBuilder()._build_kwargs(
            entry(api_key="k", url="http://llm.local/v1"), cloud_token=None
        )

        assert "base_url" not in absent
        assert given["base_url"] == "http://llm.local/v1"

    def test_the_timeout_is_on_the_sdk_and_on_the_transport(self):
        kwargs = OpenAILikeBuilder()._build_kwargs(entry(api_key="k", timeout=17), cloud_token=None)

        client = kwargs["http_client"]
        assert kwargs["timeout"] == 17
        assert client.timeout.connect == 17
        assert client.timeout.read == 17

    def test_the_pool_is_small_with_a_short_keepalive(self):
        client = OpenAILikeBuilder()._build_kwargs(entry(api_key="k"), cloud_token=None)[
            "http_client"
        ]

        pool = client._transport._pool
        assert pool._max_connections == 10
        assert pool._max_keepalive_connections == 5
        assert pool._keepalive_expiry == 30

    def test_an_explicit_key_never_gets_the_live_cloud_auth(self):
        auth = CloudTokenAuth(lambda: "live")

        kwargs = OpenAILikeBuilder()._build_kwargs(
            entry(api_key="mine"), cloud_token="t", cloud_auth=auth
        )

        assert kwargs["api_key"] == "mine"
        assert kwargs["http_client"].auth is not auth

    def test_a_cloud_key_with_live_auth_gets_it_and_logs_nothing(self, caplog):
        auth = CloudTokenAuth(lambda: "live")

        with caplog.at_level(logging.WARNING):
            kwargs = OpenAILikeBuilder()._build_kwargs(
                entry(api_key=CLOUD_TOKEN_SENTINEL), cloud_token=None, cloud_auth=auth
            )

        assert kwargs["http_client"].auth is auth
        assert caplog.records == []

    def test_a_cloud_key_with_no_live_auth_and_no_token_is_said_out_loud(self, caplog):
        with caplog.at_level(logging.WARNING):
            kwargs = OpenAILikeBuilder()._build_kwargs(
                entry(api_key=CLOUD_TOKEN_SENTINEL), cloud_token=None
            )

        assert kwargs["api_key"] == "not-set"
        assert "Requests to it will be rejected." in caplog.text

    def test_build_wraps_the_model_with_its_vision_flag_and_logger(self):
        llm_logger = LlmCallLogger()

        model = OpenAILikeBuilder().build(
            entry(api_key="k", vision=True), cloud_token=None, llm_logger=llm_logger
        )

        assert isinstance(model, LoggingModel)
        assert model.vision is True
        assert model._llm_logger is llm_logger


class TestEndpoints:
    def test_the_default_endpoint_is_the_entry_url(self):
        assert OpenAILikeBuilder().endpoint_for(entry(url="http://h/v1")) == "http://h/v1"
        assert OpenAILikeBuilder().endpoint_for(entry()) is None

    def test_gemini_is_never_sent_to_the_entry_url(self):
        assert GeminiBuilder().endpoint_for(entry(url="http://h/v1")) is None

    @pytest.mark.parametrize(
        ("url", "base"),
        [
            (None, "https://api.anthropic.com"),
            ("", "https://api.anthropic.com"),
            ("https://gw.local", "https://gw.local"),
            ("https://gw.local/", "https://gw.local"),
            ("https://gw.local/v1", "https://gw.local"),
            ("https://gw.local/v1/", "https://gw.local"),
            ("https://gw.local/proxy/v1", "https://gw.local/proxy"),
            ("https://gw.local/proxy///", "https://gw.local/proxy"),
            ("/v1", ""),
        ],
    )
    def test_anthropic_strips_the_v1_suffix_and_keeps_a_prefix(self, url, base):
        e = entry(provider="anthropic", url=url)

        assert AnthropicBuilder.base_url_for(e) == base
        assert AnthropicBuilder().endpoint_for(e) == base


class TestGemini:
    def test_the_key_is_sent_only_when_it_resolves(self):
        with_key = GeminiBuilder()._build_kwargs(entry(api_key="g"), cloud_token=None)
        without = GeminiBuilder()._build_kwargs(entry(), cloud_token=None)

        assert with_key == {"id": "m-1", "api_key": "g"}
        assert without == {"id": "m-1"}

    def test_a_missing_key_is_a_debug_line_not_a_warning(self, caplog):
        with caplog.at_level(logging.DEBUG):
            GeminiBuilder()._build_kwargs(entry(), cloud_token=None)

        assert [r.levelno for r in caplog.records] == [logging.DEBUG]

    def test_temperature_zero_is_sent_and_max_tokens_is_the_output_cap(self):
        kwargs = GeminiBuilder()._build_kwargs(
            entry(api_key="g", temperature=0, max_tokens=99), cloud_token=None
        )

        assert kwargs["temperature"] == 0
        assert kwargs["max_output_tokens"] == 99
        assert "max_tokens" not in kwargs

    def test_the_model_it_builds_takes_those_arguments(self):
        """``max_tokens`` used to fail the build with a TypeError."""
        model = GeminiBuilder().build(
            entry(provider="gemini", api_key="g", temperature=0.2, max_tokens=64),
            cloud_token=None,
            llm_logger=LlmCallLogger(),
        )

        assert model.id == "m-1"
        assert model.max_output_tokens == 64
        assert model.temperature == 0.2

    def test_without_the_sdk_it_is_unavailable_and_refuses_to_build(self, monkeypatch):
        monkeypatch.setitem(__import__("sys").modules, "igni_core.core.config.checked_gemini", None)
        builder = GeminiBuilder()

        assert builder.available is False
        with pytest.raises(RuntimeError, match="pip install google-genai"):
            builder.build(entry(provider="gemini"), cloud_token=None, llm_logger=LlmCallLogger())


class TestAnthropic:
    def test_thinking_is_copied_effort_becomes_output_config_and_max_tokens_passes(self):
        thinking = {"type": "enabled", "budget_tokens": 1024}
        e = entry(
            provider="anthropic", api_key="k", thinking=thinking, effort="high", max_tokens=300
        )

        kwargs = AnthropicBuilder()._build_kwargs(e, cloud_token=None)

        assert kwargs["thinking"] == thinking
        assert kwargs["thinking"] is not e.thinking
        assert kwargs["output_config"] == {"effort": "high"}
        assert kwargs["max_tokens"] == 300

    def test_what_is_unset_is_not_sent(self):
        kwargs = AnthropicBuilder()._build_kwargs(
            entry(provider="anthropic", api_key="k"), cloud_token=None
        )

        for name in ("thinking", "output_config", "max_tokens", "temperature"):
            assert name not in kwargs

    def test_an_explicit_key_never_gets_the_live_cloud_auth(self):
        auth = CloudTokenAuth(lambda: "live")

        kwargs = AnthropicBuilder()._build_kwargs(
            entry(provider="anthropic", api_key="mine"), cloud_token=None, cloud_auth=auth
        )

        assert kwargs["http_client"].auth._cloud is None

    def test_without_the_sdk_it_is_unavailable_and_refuses_to_build(self, monkeypatch):
        monkeypatch.setitem(__import__("sys").modules, "igni_core.core.config.logging_claude", None)
        builder = AnthropicBuilder()

        assert builder.available is False
        with pytest.raises(RuntimeError, match="igni-core\\[anthropic\\]"):
            builder.build(entry(provider="anthropic"), cloud_token=None, llm_logger=LlmCallLogger())


def test_a_builder_must_implement_build():
    with pytest.raises(TypeError):
        ProviderClientBuilder()  # type: ignore[abstract]
