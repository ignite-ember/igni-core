"""``telemetry.otlp``: defaults, the OTEL environment fallbacks and the header parser."""

from __future__ import annotations

import pytest

from igni_core.core.config.schemas import OtlpConfig, TelemetryConfig

_VARIABLES = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_SERVICE_NAME",
    "IGNI_OTEL_CAPTURE_CONTENT",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_with_nothing_set_nothing_is_exported():
    otlp = OtlpConfig()
    assert otlp.endpoint == ""
    assert otlp.enabled is False
    assert otlp.protocol == "http/protobuf"
    assert otlp.headers == {}
    assert otlp.service_name == "igni"
    assert otlp.capture_content is False
    assert TelemetryConfig().otlp == otlp


@pytest.mark.parametrize("endpoint", ["", " ", "\t\n"])
def test_a_blank_endpoint_is_disabled(endpoint: str):
    assert OtlpConfig(endpoint=endpoint).enabled is False


def test_a_named_endpoint_enables_export():
    assert OtlpConfig(endpoint="http://otel.acme.internal:4318").enabled is True


def test_the_environment_fills_every_field_left_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", " http://c:4317 ")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer%20x")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "acme-igni")
    monkeypatch.setenv("IGNI_OTEL_CAPTURE_CONTENT", "true")
    otlp = OtlpConfig()
    assert otlp.endpoint == "http://c:4317"
    assert otlp.protocol == "grpc"
    assert otlp.headers == {"authorization": "Bearer x"}
    assert otlp.service_name == "acme-igni"
    assert otlp.capture_content is True


def test_the_organisations_own_values_beat_the_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://env:4317")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "a=env")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "env-name")
    otlp = OtlpConfig(endpoint="http://cfg:4318", headers={"x": "cfg"}, service_name="cfg-name")
    assert otlp.endpoint == "http://cfg:4318"
    assert otlp.headers == {"x": "cfg"}
    assert otlp.service_name == "cfg-name"
    # A configured protocol equal to the default cannot be told from unset.
    assert otlp.protocol == "grpc"


def test_an_unknown_protocol_in_the_environment_is_ignored(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/json")
    assert OtlpConfig().protocol == "http/protobuf"


def test_blank_service_name_in_the_environment_keeps_igni(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OTEL_SERVICE_NAME", "  ")
    assert OtlpConfig().service_name == "igni"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " Yes ", "on", "On"])
def test_truthy_capture_content_values_turn_it_on(monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setenv("IGNI_OTEL_CAPTURE_CONTENT", value)
    assert OtlpConfig().capture_content is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "2", "enabled"])
def test_other_capture_content_values_leave_it_off(monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setenv("IGNI_OTEL_CAPTURE_CONTENT", value)
    assert OtlpConfig().capture_content is False


def test_the_environment_can_turn_capture_content_on_but_never_off(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("IGNI_OTEL_CAPTURE_CONTENT", "0")
    assert OtlpConfig(capture_content=True).capture_content is True


def test_fill_from_reads_the_mapping_it_is_given_not_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://process:4318")
    otlp = OtlpConfig()
    otlp.endpoint = ""
    otlp.fill_from({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://given:4318"})
    assert otlp.endpoint == "http://given:4318"


def test_headers_are_split_on_commas_keys_lowercased_and_values_url_decoded():
    assert OtlpConfig.parse_headers("Api-Key=a%2Bb, X-Org = acme%20inc ,k=v") == {
        "api-key": "a+b",
        "x-org": "acme inc",
        "k": "v",
    }


def test_a_value_may_contain_equals_signs_only_the_first_splits():
    assert OtlpConfig.parse_headers("token=abc==") == {"token": "abc=="}


def test_pairs_without_an_equals_or_with_an_empty_key_are_skipped():
    assert OtlpConfig.parse_headers("novalue, =orphan, ,ok=1") == {"ok": "1"}


def test_an_empty_header_string_gives_no_headers():
    assert OtlpConfig.parse_headers("") == {}


def test_a_repeated_header_keeps_the_last_value():
    assert OtlpConfig.parse_headers("a=1,A=2") == {"a": "2"}


def test_a_header_with_an_empty_value_is_kept_empty():
    assert OtlpConfig.parse_headers("a=") == {"a": ""}
