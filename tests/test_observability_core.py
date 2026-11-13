"""The pieces of ``core/observability``, one by one.

``test_telemetry_goes_only_to_the_customers_collector.py`` holds the end
to end properties (a real turn, a live collector). These tests hold what
that turn does not reach: which attributes a span may keep, what the
processor stamps and counts, how the sinks are built, what the target and
the runtime answer for each kind of settings.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

import igni_core
from igni_core.core.config.schemas.telemetry import OtlpConfig, TelemetryConfig
from igni_core.core.config.settings import Settings
from igni_core.core.db.scope import Tenant, tenancy
from igni_core.core.observability import OTEL_INSTALLED, OtelRuntime, OtelStart, OtlpTarget
from igni_core.core.observability import runtime as runtime_module
from igni_core.core.observability.content import ContentScrubbingExporter, SpanContentPolicy
from igni_core.core.observability.pipeline import OtelPipeline, OtelSinks
from igni_core.core.observability.run_spans import RunSpanProcessor, SpanAttribute

_OTEL_VARIABLES = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_SERVICE_NAME",
    "IGNI_OTEL_CAPTURE_CONTENT",
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    for name in _OTEL_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    yield
    OtelRuntime.stop()
    tenancy.use(None)


def settings_for(endpoint: str, **fields: object) -> Settings:
    return Settings(telemetry=TelemetryConfig(otlp=OtlpConfig(endpoint=endpoint, **fields)))


def memory_sinks() -> tuple[OtelSinks, InMemorySpanExporter, InMemoryMetricReader]:
    spans = InMemorySpanExporter()
    metrics = InMemoryMetricReader()
    return OtelSinks(span_exporter=spans, metric_reader=metrics), spans, metrics


def finished_spans(build) -> list[ReadableSpan]:
    """Spans a real tracer finished; ``build(tracer)`` opens and ends them."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider.add_span_processor(SimpleSpanProcessor(exporter))
    build(provider.get_tracer("test"))
    return list(exporter.get_finished_spans())


# ── The package surface ─────────────────────────────────────────────


class TestThePackageSurface:
    def test_it_exports_the_runtime_and_the_target(self):
        import igni_core.core.observability as package

        assert sorted(package.__all__) == [
            "OTEL_INSTALLED",
            "OtelRuntime",
            "OtelStart",
            "OtlpTarget",
        ]
        assert package.OtelRuntime is OtelRuntime
        assert package.OtlpTarget is OtlpTarget
        assert package.OtelStart is OtelStart
        assert package.OTEL_INSTALLED is True

    def test_the_package_imports_and_answers_without_the_otel_extra(self):
        """An install without the extra still imports it, and refuses to start."""
        code = textwrap.dedent(
            """
            import sys

            class Block:
                def find_spec(self, name, path=None, target=None):
                    if name == "opentelemetry" or name.startswith("opentelemetry."):
                        raise ImportError(name)
                    if name.startswith("openinference"):
                        raise ImportError(name)

            sys.meta_path.insert(0, Block())
            from igni_core.core.observability import OTEL_INSTALLED, OtelRuntime
            from igni_core.core.config.schemas.telemetry import OtlpConfig, TelemetryConfig
            from igni_core.core.config.settings import Settings

            assert OTEL_INSTALLED is False
            OtelRuntime.note_permission("deny")
            started = OtelRuntime.start(
                Settings(telemetry=TelemetryConfig(otlp=OtlpConfig(endpoint="http://127.0.0.1:4318")))
            )
            assert started.ok is False
            assert "igni-core[otel]" in started.reason
            assert OtelRuntime.current() is None
            print("ok")
            """
        )
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", code], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "ok"


# ── content.py ──────────────────────────────────────────────────────


class TestSpanContentPolicy:
    @pytest.mark.parametrize("name", sorted(SpanContentPolicy.KEEP))
    def test_every_named_attribute_is_kept(self, name):
        assert SpanContentPolicy.keeps(name)

    @pytest.mark.parametrize(
        "name",
        [
            "igni.org.id",
            "igni.agent.runs",
            "llm.token_count.prompt",
            "llm.cost.total",
            "gen_ai.usage.input_tokens",
        ],
    )
    def test_the_prefixes_are_kept(self, name):
        assert SpanContentPolicy.keeps(name)

    @pytest.mark.parametrize(
        "name",
        [
            "input.value",
            "output.value",
            "tool.parameters",
            "llm.input_messages.0.message.content",
            "llm.output_messages.0.message.content",
            "url.full",
            "url.query",
            "http.url",
            "exception.message",
            "exception.stacktrace",
            "igniX",
            "igni",
            "llm.token_countless",
            "llm.token_count",
            "gen_ai.usage",
            "some.future.content.field",
            "",
        ],
    )
    def test_everything_else_is_dropped(self, name):
        assert not SpanContentPolicy.keeps(name)

    def test_attributes_filters_a_mapping(self):
        kept = SpanContentPolicy.attributes(
            {"tool.name": "read", "tool.parameters": "SECRET", "igni.outcome": "ok"}
        )
        assert kept == {"tool.name": "read", "igni.outcome": "ok"}

    def test_no_attributes_give_an_empty_dict(self):
        assert SpanContentPolicy.attributes(None) == {}
        assert SpanContentPolicy.attributes({}) == {}

    def test_a_scrubbed_span_keeps_identity_and_timing_and_loses_content(self):
        def build(tracer):
            with tracer.start_as_current_span("parent"):
                span = tracer.start_span(
                    "tool",
                    attributes={
                        "tool.name": "read",
                        "tool.parameters": "SECRET",
                        "url.full": "https://x.example/?key=SECRET",
                        "http.method": "GET",
                    },
                )
                span.add_event("log", {"message": "SECRET"})
                try:
                    raise ValueError("SECRET")
                except ValueError as exc:
                    span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, "failed reading SECRET"))
                span.end()

        spans = [s for s in finished_spans(build) if s.name == "tool"]
        original = spans[0]

        scrubbed = SpanContentPolicy.scrub(original)

        assert dict(scrubbed.attributes) == {"tool.name": "read", "http.method": "GET"}
        assert [e.name for e in scrubbed.events] == ["exception"]
        assert dict(scrubbed.events[0].attributes) == {"exception.type": "ValueError"}
        assert scrubbed.status.status_code is StatusCode.ERROR
        assert scrubbed.status.description is None
        assert scrubbed.context == original.context
        assert scrubbed.parent == original.parent
        assert scrubbed.kind == original.kind
        assert scrubbed.resource == original.resource
        assert scrubbed.start_time == original.start_time
        assert scrubbed.end_time == original.end_time
        assert scrubbed.name == "tool"
        # The original is untouched.
        assert "tool.parameters" in original.attributes

    def test_a_span_without_attributes_or_events_scrubs_to_empty(self):
        span = ReadableSpan(name="bare", attributes=None)
        scrubbed = SpanContentPolicy.scrub(span)
        assert dict(scrubbed.attributes or {}) == {}
        assert list(scrubbed.events) == []


class RecordingExporter(SpanExporter):
    def __init__(self, result: SpanExportResult = SpanExportResult.SUCCESS) -> None:
        self.result = result
        self.exported: list[list[ReadableSpan]] = []
        self.calls: list[str] = []
        self.flush_result = True

    def export(self, spans):
        self.exported.append(list(spans))
        return self.result

    def shutdown(self):
        self.calls.append("shutdown")

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        self.calls.append(f"flush:{timeout_millis}")
        return self.flush_result


class TestContentScrubbingExporter:
    def spans(self) -> list[ReadableSpan]:
        return finished_spans(
            lambda tracer: tracer.start_span("a", attributes={"input.value": "SECRET"}).end()
        )

    def test_the_wrapped_exporter_receives_scrubbed_copies(self):
        inner = RecordingExporter()
        result = ContentScrubbingExporter(inner).export(self.spans())

        assert result is SpanExportResult.SUCCESS
        assert len(inner.exported) == 1
        assert [dict(s.attributes) for s in inner.exported[0]] == [{}]

    def test_its_result_is_passed_through(self):
        inner = RecordingExporter(SpanExportResult.FAILURE)
        assert ContentScrubbingExporter(inner).export(self.spans()) is SpanExportResult.FAILURE

    def test_an_empty_batch_is_exported_empty(self):
        inner = RecordingExporter()
        ContentScrubbingExporter(inner).export([])
        assert inner.exported == [[]]

    def test_shutdown_and_flush_go_to_the_wrapped_exporter(self):
        inner = RecordingExporter()
        exporter = ContentScrubbingExporter(inner)

        exporter.shutdown()
        assert exporter.force_flush(1234) is True
        inner.flush_result = False
        assert exporter.force_flush() is False

        assert inner.calls == ["shutdown", "flush:1234", "flush:30000"]


# ── run_spans.py ────────────────────────────────────────────────────


def processor_with_metrics() -> tuple[TracerProvider, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(RunSpanProcessor(meter_provider.get_meter("test")))
    return tracer_provider, reader


def points(reader: InMemoryMetricReader, name: str) -> list:
    data = reader.get_metrics_data()
    if data is None:
        return []
    return [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


def labelled(reader, name) -> dict[tuple, float]:
    return {
        tuple(sorted(dict(p.attributes).items())): getattr(p, "value", None) or p.count
        for p in points(reader, name)
    }


class TestRunSpanProcessor:
    def test_an_agent_run_is_stamped_with_its_tenant(self):
        tenancy.use(lambda: Tenant(org="acme", producer="person:7"))
        provider, _ = processor_with_metrics()
        span = provider.get_tracer("t").start_span("run", attributes={SpanAttribute.KIND: "AGENT"})
        assert span.attributes[SpanAttribute.ORG] == "acme"
        assert span.attributes[SpanAttribute.PRODUCER] == "person:7"

    def test_without_a_tenant_the_span_is_left_alone(self):
        provider, _ = processor_with_metrics()
        span = provider.get_tracer("t").start_span("run", attributes={SpanAttribute.KIND: "AGENT"})
        assert SpanAttribute.ORG not in span.attributes
        assert SpanAttribute.PRODUCER not in span.attributes

    def test_a_tenant_lookup_that_fails_never_fails_the_run(self):
        def broken() -> Tenant:
            raise RuntimeError("no principal")

        tenancy.use(broken)
        provider, _ = processor_with_metrics()
        span = provider.get_tracer("t").start_span("run", attributes={SpanAttribute.KIND: "AGENT"})
        assert SpanAttribute.ORG not in span.attributes

    def test_only_agent_spans_are_stamped(self):
        tenancy.use(lambda: Tenant(org="acme", producer="person:7"))
        provider, _ = processor_with_metrics()
        tracer = provider.get_tracer("t")
        for attributes in ({SpanAttribute.KIND: "LLM"}, {SpanAttribute.KIND: "TOOL"}, None):
            span = tracer.start_span("s", attributes=attributes)
            assert SpanAttribute.ORG not in (span.attributes or {})

    def test_an_agent_run_counts_by_agent_and_outcome_and_records_its_duration(self):
        provider, reader = processor_with_metrics()
        tracer = provider.get_tracer("t")
        attributes = {SpanAttribute.KIND: "AGENT", SpanAttribute.AGENT_NAME: "coder"}
        tracer.start_span("a", attributes=attributes, start_time=1_000_000_000).end(
            end_time=3_500_000_000
        )
        failed = tracer.start_span("b", attributes=attributes)
        failed.set_status(Status(StatusCode.ERROR, "x"))
        failed.end()

        runs = labelled(reader, "igni.agent.runs")
        assert runs == {
            (("agent.name", "coder"), ("igni.outcome", "ok")): 1,
            (("agent.name", "coder"), ("igni.outcome", "error")): 1,
        }
        ok_duration = next(
            p
            for p in points(reader, "igni.agent.run.duration")
            if p.attributes[SpanAttribute.OUTCOME] == "ok"
        )
        assert ok_duration.sum == pytest.approx(2.5)
        assert ok_duration.count == 1

    def test_an_agent_without_a_name_is_labelled_with_an_empty_name(self):
        provider, reader = processor_with_metrics()
        provider.get_tracer("t").start_span("a", attributes={SpanAttribute.KIND: "AGENT"}).end()
        assert labelled(reader, "igni.agent.runs") == {
            (("agent.name", ""), ("igni.outcome", "ok")): 1
        }

    def test_llm_tokens_are_counted_by_model_and_direction(self):
        provider, reader = processor_with_metrics()
        provider.get_tracer("t").start_span(
            "llm",
            attributes={
                SpanAttribute.KIND: "LLM",
                SpanAttribute.MODEL: "m1",
                SpanAttribute.PROMPT_TOKENS: 11,
                SpanAttribute.COMPLETION_TOKENS: 7,
            },
        ).end()
        assert labelled(reader, "igni.llm.tokens") == {
            (("igni.token.direction", "input"), ("llm.model_name", "m1")): 11,
            (("igni.token.direction", "output"), ("llm.model_name", "m1")): 7,
        }

    @pytest.mark.parametrize("count", [0, -3, "12", 1.5, None])
    def test_a_token_count_that_is_not_a_positive_integer_is_not_counted(self, count):
        provider, reader = processor_with_metrics()
        attributes = {SpanAttribute.KIND: "LLM", SpanAttribute.MODEL: "m1"}
        if count is not None:
            attributes[SpanAttribute.PROMPT_TOKENS] = count
        provider.get_tracer("t").start_span("llm", attributes=attributes).end()
        assert labelled(reader, "igni.llm.tokens") == {}

    def test_a_model_without_a_name_is_labelled_with_an_empty_name(self):
        provider, reader = processor_with_metrics()
        provider.get_tracer("t").start_span(
            "llm", attributes={SpanAttribute.KIND: "LLM", SpanAttribute.PROMPT_TOKENS: 2}
        ).end()
        assert labelled(reader, "igni.llm.tokens") == {
            (("igni.token.direction", "input"), ("llm.model_name", "")): 2
        }

    def test_tool_calls_count_by_tool_outcome_and_decision(self):
        provider, reader = processor_with_metrics()
        tracer = provider.get_tracer("t")

        def tool(name, decision=None, error=False):
            attributes = {SpanAttribute.KIND: "TOOL"}
            if name:
                attributes[SpanAttribute.TOOL_NAME] = name
            if decision:
                attributes[SpanAttribute.PERMISSION] = decision
            span = tracer.start_span("t", attributes=attributes)
            if error:
                span.set_status(Status(StatusCode.ERROR))
            span.end()

        tool("read", "allow")
        tool("write", "deny", error=True)  # a refusal is denied, not error
        tool("shell", error=True)
        tool(None)

        def key(tool_name, outcome, decision):
            return (
                ("igni.outcome", outcome),
                ("igni.permission.decision", decision),
                ("tool.name", tool_name),
            )

        assert labelled(reader, "igni.tool.calls") == {
            key("read", "ok", "allow"): 1,
            key("write", "denied", "deny"): 1,
            key("shell", "error", ""): 1,
            key("", "ok", ""): 1,
        }

    def test_other_kinds_of_span_are_counted_nowhere(self):
        provider, reader = processor_with_metrics()
        tracer = provider.get_tracer("t")
        tracer.start_span("c", attributes={SpanAttribute.KIND: "CHAIN"}).end()
        tracer.start_span("n").end()
        for name in ("igni.agent.runs", "igni.llm.tokens", "igni.tool.calls"):
            assert points(reader, name) == []

    def test_a_span_without_times_is_counted_but_has_no_duration(self):
        reader = InMemoryMetricReader()
        processor = RunSpanProcessor(MeterProvider(metric_readers=[reader]).get_meter("t"))
        span = ReadableSpan(
            name="a",
            attributes={SpanAttribute.KIND: "AGENT"},
            status=Status(StatusCode.OK),
        )
        processor.on_end(span)
        assert len(points(reader, "igni.agent.runs")) == 1
        assert points(reader, "igni.agent.run.duration") == []

    def test_flushing_succeeds_and_shutting_down_does_nothing(self):
        processor = RunSpanProcessor(MeterProvider().get_meter("t"))
        assert processor.force_flush() is True
        assert processor.shutdown() is None


# ── target.py ───────────────────────────────────────────────────────


class TestOtlpTarget:
    def test_settings_without_a_telemetry_block_export_nothing(self):
        target = OtlpTarget.resolve(SimpleNamespace())
        assert target.ok is False
        assert target.reason == ""
        assert target.config is None

    def test_a_telemetry_block_without_otlp_exports_nothing(self):
        target = OtlpTarget.resolve(SimpleNamespace(telemetry=SimpleNamespace()))
        assert (target.ok, target.reason) == (False, "")

    def test_an_otlp_value_of_the_wrong_type_exports_nothing(self):
        stub = SimpleNamespace(telemetry=SimpleNamespace(otlp={"endpoint": "http://x:4318"}))
        assert (OtlpTarget.resolve(stub).ok, OtlpTarget.resolve(stub).reason) == (False, "")

    @pytest.mark.parametrize("endpoint", ["", "   ", "\t\n"])
    def test_a_blank_endpoint_is_simply_disabled(self, endpoint):
        target = OtlpTarget.resolve(settings_for(endpoint))
        assert (target.ok, target.reason) == (False, "")

    def test_the_endpoint_is_stripped_in_the_resolved_config(self):
        target = OtlpTarget.resolve(settings_for("  http://otel.acme.com:4318 \n"))
        assert target.ok is True
        assert target.config is not None
        assert target.config.endpoint == "http://otel.acme.com:4318"

    def test_the_scheme_may_be_in_any_case(self):
        assert OtlpTarget.resolve(settings_for("HTTPS://otel.acme.com")).ok is True
        assert OtlpTarget.resolve(settings_for("Http://otel.acme.com:4318")).ok is True

    @pytest.mark.parametrize(
        "endpoint",
        [
            "ftp://otel.acme.com",
            "grpc://otel.acme.com:4317",
            "otel.acme.com:4318",
            "http://",
            "http://:4318",
            "http:///v1/traces",
            "http://[::1",
        ],
    )
    def test_an_endpoint_that_is_not_http_with_a_host_is_refused_with_its_reason(self, endpoint):
        target = OtlpTarget.resolve(settings_for(endpoint))
        assert target.ok is False
        assert target.config is None
        assert "is not an http:// or https:// URL" in target.reason
        assert repr(endpoint) in target.reason

    def test_a_port_credentials_and_a_path_do_not_matter(self):
        target = OtlpTarget.resolve(settings_for("https://user:pw@otel.acme.com:8443/base?x=1"))
        assert target.ok is True

    def test_air_gapped_refuses_a_host_outside_the_network_with_the_policys_words(self):
        settings = Settings(
            air_gapped=True,
            telemetry=TelemetryConfig(otlp=OtlpConfig(endpoint="https://otel.vendor.example")),
        )
        target = OtlpTarget.resolve(settings)
        assert target.ok is False
        assert target.config is None
        assert "OpenTelemetry collector" in target.reason
        assert "air-gapped" in target.reason

    def test_air_gapped_allows_localhost(self):
        settings = Settings(
            air_gapped=True,
            telemetry=TelemetryConfig(otlp=OtlpConfig(endpoint="http://localhost:4318")),
        )
        assert OtlpTarget.resolve(settings).ok is True


# ── pipeline.py ─────────────────────────────────────────────────────


class TestOtelSinks:
    def test_http_exporters_go_to_the_signal_paths_under_the_endpoint(self):
        sinks = OtelSinks.otlp(OtlpConfig(endpoint="http://otel.acme.com:4318///"))
        assert sinks.span_exporter._endpoint == "http://otel.acme.com:4318/v1/traces"
        assert sinks.metric_reader._exporter._endpoint == "http://otel.acme.com:4318/v1/metrics"

    def test_metrics_are_pushed_every_sixty_seconds(self):
        sinks = OtelSinks.otlp(OtlpConfig(endpoint="http://otel.acme.com:4318"))
        assert OtelSinks.METRIC_INTERVAL_MS == 60_000
        assert sinks.metric_reader._export_interval_millis == 60_000

    def test_grpc_to_an_http_endpoint_is_insecure_and_strips_the_slash(self):
        config = OtlpConfig(
            endpoint="http://otel.acme.com:4317/", protocol="grpc", headers={"x-key": "k"}
        )
        sinks = OtelSinks.otlp(config)
        metric_exporter = sinks.metric_reader._exporter
        for exporter in (sinks.span_exporter, metric_exporter):
            assert exporter._endpoint == "otel.acme.com:4317"
            assert exporter._insecure is True
            assert ("x-key", "k") in exporter._headers

    def test_grpc_to_an_https_endpoint_is_secure(self):
        sinks = OtelSinks.otlp(OtlpConfig(endpoint="https://otel.acme.com:4317", protocol="grpc"))
        assert sinks.span_exporter._insecure is False
        assert sinks.metric_reader._exporter._insecure is False

    def test_grpc_scheme_in_upper_case_is_still_insecure(self):
        sinks = OtelSinks.otlp(OtlpConfig(endpoint="HTTP://otel.acme.com:4317", protocol="grpc"))
        assert sinks.span_exporter._insecure is True


class TestOtelPipeline:
    def pipeline(self, **fields) -> tuple[OtelPipeline, InMemorySpanExporter, InMemoryMetricReader]:
        sinks, spans, metrics = memory_sinks()
        config = OtlpConfig(endpoint="http://127.0.0.1:4318", **fields)
        return OtelPipeline(config, sinks), spans, metrics

    def test_the_resource_names_the_service_and_the_version(self):
        pipeline, _, _ = self.pipeline(service_name="igni-acme")
        attributes = pipeline.tracer_provider.resource.attributes
        assert attributes["service.name"] == "igni-acme"
        assert attributes["service.version"] == igni_core.__version__
        assert pipeline.meter_provider._sdk_config.resource.attributes["service.name"] == (
            "igni-acme"
        )

    def test_nothing_is_installed_as_the_global_default(self):
        before_tracer = otel_trace.get_tracer_provider()
        pipeline, _, _ = self.pipeline()
        pipeline.instrument()
        try:
            assert otel_trace.get_tracer_provider() is before_tracer
            assert otel_trace.get_tracer_provider() is not pipeline.tracer_provider
        finally:
            pipeline.shutdown()

    def test_without_content_every_hide_switch_is_on(self):
        pipeline, _, _ = self.pipeline()
        config = pipeline.trace_config()
        hidden = {name for name, value in vars(config).items() if value is True}
        assert hidden == {
            "hide_inputs",
            "hide_outputs",
            "hide_input_messages",
            "hide_output_messages",
            "hide_input_images",
            "hide_input_text",
            "hide_output_text",
            "hide_embedding_vectors",
            "hide_prompts",
            "hide_choices",
            "hide_llm_invocation_parameters",
        }

    def test_with_content_the_default_trace_config_is_used(self):
        pipeline, _, _ = self.pipeline(capture_content=True)
        config = pipeline.trace_config()
        assert config.hide_inputs is False
        assert config.hide_outputs is False
        assert config.hide_input_messages is False

    def _export_one_span(self, pipeline) -> None:
        pipeline.tracer_provider.get_tracer("t").start_span(
            "s", attributes={"input.value": "PROMPT", "tool.name": "read"}
        ).end()
        pipeline.force_flush()

    def test_spans_are_scrubbed_unless_content_is_captured(self):
        pipeline, spans, _ = self.pipeline()
        self._export_one_span(pipeline)
        assert dict(spans.get_finished_spans()[0].attributes) == {"tool.name": "read"}

        pipeline, spans, _ = self.pipeline(capture_content=True)
        self._export_one_span(pipeline)
        assert dict(spans.get_finished_spans()[0].attributes) == {
            "input.value": "PROMPT",
            "tool.name": "read",
        }

    def test_finished_spans_are_counted_by_the_run_processor(self):
        pipeline, _, metrics = self.pipeline()
        pipeline.tracer_provider.get_tracer("t").start_span(
            "run", attributes={SpanAttribute.KIND: "AGENT", SpanAttribute.AGENT_NAME: "a"}
        ).end()
        pipeline.force_flush()
        assert len(points(metrics, "igni.agent.runs")) == 1

    def test_instrumenting_makes_httpx_requests_carry_the_trace_context(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        import httpx

        seen: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen.update({k.lower(): v for k, v in self.headers.items()})
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                return None

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        pipeline, spans, _ = self.pipeline()
        pipeline.instrument()
        try:
            url = f"http://127.0.0.1:{server.server_port}/path?key=SECRET"
            with httpx.Client() as client:
                client.get(url)
        finally:
            pipeline.shutdown()
            server.shutdown()
            thread.join()
        assert "traceparent" in seen
        exported = spans.get_finished_spans()
        assert len(exported) == 1
        # Spans reach the collector with the URL's query already dropped.
        assert "SECRET" not in repr(dict(exported[0].attributes))
        assert exported[0].attributes["http.method"] == "GET"

    def test_shutdown_without_instrument_and_twice_is_harmless(self):
        pipeline, _, _ = self.pipeline()
        pipeline.shutdown()
        pipeline.shutdown()

    def test_an_unreachable_collector_does_not_break_flush_or_shutdown(self):
        sinks = OtelSinks.otlp(OtlpConfig(endpoint="http://127.0.0.1:9"))
        pipeline = OtelPipeline(OtlpConfig(endpoint="http://127.0.0.1:9"), sinks)
        pipeline.tracer_provider.get_tracer("t").start_span("s").end()
        pipeline.force_flush()
        pipeline.shutdown()


# ── runtime.py ──────────────────────────────────────────────────────


class TestOtelRuntime:
    def test_a_missing_otel_extra_refuses_with_the_install_hint(self, monkeypatch, caplog):
        monkeypatch.setattr(runtime_module, "OTEL_INSTALLED", False)
        sinks, _, _ = memory_sinks()

        with caplog.at_level(logging.WARNING):
            started = OtelRuntime.start(settings_for("http://127.0.0.1:4318"), sinks=sinks)

        assert started.ok is False
        assert "igni-core[otel]" in started.reason
        assert started.reason in caplog.text
        assert OtelRuntime.current() is None

    def test_the_extra_missing_is_only_reported_when_a_collector_is_named(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(runtime_module, "OTEL_INSTALLED", False)
        with caplog.at_level(logging.WARNING):
            started = OtelRuntime.start(Settings())
        assert (started.ok, started.reason) == (False, "")
        assert caplog.text == ""

    def test_no_endpoint_logs_nothing(self, caplog):
        with caplog.at_level(logging.DEBUG):
            started = OtelRuntime.start(SimpleNamespace())
        assert (started.ok, started.reason) == (False, "")
        assert caplog.records == []

    def test_a_refused_endpoint_is_warned_with_its_reason(self, caplog):
        with caplog.at_level(logging.WARNING):
            started = OtelRuntime.start(settings_for("ftp://x"))
        assert started.ok is False
        assert started.reason in caplog.text
        assert OtelRuntime.current() is None

    def test_starting_returns_the_config_and_logs_where_it_exports(self, caplog):
        sinks, _, _ = memory_sinks()
        with caplog.at_level(logging.INFO):
            started = OtelRuntime.start(
                settings_for("http://127.0.0.1:4318", protocol="grpc"), sinks=sinks
            )
        assert started.ok is True
        assert started.config is not None
        assert started.config.endpoint == "http://127.0.0.1:4318"
        assert isinstance(OtelRuntime.current(), OtelPipeline)
        assert "http://127.0.0.1:4318 (grpc)." in caplog.text
        assert "with content" not in caplog.text

    def test_the_log_says_when_content_is_captured(self, caplog):
        sinks, _, _ = memory_sinks()
        with caplog.at_level(logging.INFO):
            OtelRuntime.start(
                settings_for("http://127.0.0.1:4318", capture_content=True), sinks=sinks
            )
        assert "(http/protobuf), with content." in caplog.text

    def test_starting_twice_stops_the_first_pipeline_and_keeps_the_second(self):
        first_sinks, first_spans, _ = memory_sinks()
        second_sinks, _, _ = memory_sinks()
        OtelRuntime.start(settings_for("http://127.0.0.1:4318"), sinks=first_sinks)
        first = OtelRuntime.current()
        assert first is not None
        first.tracer_provider.get_tracer("t").start_span("held").end()

        OtelRuntime.start(settings_for("http://127.0.0.1:4318"), sinks=second_sinks)

        second = OtelRuntime.current()
        assert second is not None
        assert second is not first
        # The first one was shut down, which exported what it held.
        assert [s.name for s in first_spans.get_finished_spans()] == ["held"]

    def test_flush_exports_what_is_held(self):
        sinks, spans, _ = memory_sinks()
        OtelRuntime.start(settings_for("http://127.0.0.1:4318"), sinks=sinks)
        pipeline = OtelRuntime.current()
        pipeline.tracer_provider.get_tracer("t").start_span(
            "held", attributes={"tool.name": "x"}
        ).end()

        OtelRuntime.flush()

        assert [s.name for s in spans.get_finished_spans()] == ["held"]

    def test_flush_before_start_does_nothing(self):
        OtelRuntime.flush()
        assert OtelRuntime.current() is None

    def test_stop_exports_what_is_held_and_is_safe_to_repeat(self):
        sinks, spans, _ = memory_sinks()
        OtelRuntime.start(settings_for("http://127.0.0.1:4318"), sinks=sinks)
        pipeline = OtelRuntime.current()
        pipeline.tracer_provider.get_tracer("t").start_span("held").end()

        OtelRuntime.stop()
        OtelRuntime.stop()

        assert [s.name for s in spans.get_finished_spans()] == ["held"]
        assert OtelRuntime.current() is None

    def test_stop_before_start_is_safe(self):
        OtelRuntime.stop()
        assert OtelRuntime.current() is None

    def test_a_pipeline_that_cannot_be_built_leaves_nothing_running(self, monkeypatch):
        sinks, _, _ = memory_sinks()
        OtelRuntime.start(settings_for("http://127.0.0.1:4318"), sinks=sinks)

        def broken(*args, **kwargs):
            raise RuntimeError("no providers")

        monkeypatch.setattr(runtime_module, "OtelPipeline", broken)
        with pytest.raises(RuntimeError):
            OtelRuntime.start(settings_for("http://127.0.0.1:4318"), sinks=sinks)

        assert OtelRuntime.current() is None

    def test_a_permission_decision_lands_on_the_current_recording_span(self):
        sinks, spans, _ = memory_sinks()
        OtelRuntime.start(settings_for("http://127.0.0.1:4318"), sinks=sinks)
        pipeline = OtelRuntime.current()
        tracer = pipeline.tracer_provider.get_tracer("t")

        with tracer.start_as_current_span("tool"):
            OtelRuntime.note_permission("deny")
        pipeline.force_flush()

        assert spans.get_finished_spans()[0].attributes[SpanAttribute.PERMISSION] == "deny"

    def test_a_permission_decision_before_start_is_ignored(self):
        provider = TracerProvider()
        with provider.get_tracer("t").start_as_current_span("tool") as span:
            OtelRuntime.note_permission("deny")
            assert SpanAttribute.PERMISSION not in span.attributes

    def test_a_permission_decision_outside_a_span_is_ignored(self):
        sinks, _, _ = memory_sinks()
        OtelRuntime.start(settings_for("http://127.0.0.1:4318"), sinks=sinks)
        assert not otel_trace.get_current_span().is_recording()
        OtelRuntime.note_permission("deny")

    def test_a_permission_decision_without_the_extra_is_ignored(self, monkeypatch):
        sinks, _, _ = memory_sinks()
        OtelRuntime.start(settings_for("http://127.0.0.1:4318"), sinks=sinks)
        pipeline = OtelRuntime.current()
        monkeypatch.setattr(runtime_module, "OTEL_INSTALLED", False)
        with pipeline.tracer_provider.get_tracer("t").start_as_current_span("tool") as span:
            OtelRuntime.note_permission("allow")
            assert SpanAttribute.PERMISSION not in span.attributes

    def test_the_extra_is_reported_installed(self):
        assert OTEL_INSTALLED is True
