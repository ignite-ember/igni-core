"""``WebTools`` as the agent's toolkit, and the models ``web_schemas`` hands across.

The transport is the only thing replaced (``httpx.MockTransport``); the
toolkit, the fetcher and the models are real.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from igni_core import __version__
from igni_core.core.broadcast_schema import BroadcastEvent
from igni_core.core.tools.web import WebTools
from igni_core.core.tools.web_schemas import (
    FetchResult,
    HttpFetcherConfig,
    HttpRetryAttemptEvent,
    HttpRetrySucceededEvent,
)

URL = "https://example.test/x"
_REAL_CLIENT = httpx.AsyncClient


@pytest.fixture
def serve(monkeypatch):
    requests: list[httpx.Request] = []

    def install(*responses):
        queue = list(responses)

        def handler(request):
            requests.append(request)
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, Exception):
                raise item
            return item

        monkeypatch.setattr(
            "igni_core.core.tools.http_fetcher.httpx.AsyncClient",
            lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw),
        )
        return requests

    return install


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    async def instant(_delay):
        return None

    monkeypatch.setattr("igni_core.core.utils.http_retry.asyncio.sleep", instant)


def _resp(body, ctype="text/plain", status=200):
    return httpx.Response(status, text=body, headers={"content-type": ctype})


def _flags(tools: WebTools) -> dict[str, bool]:
    return {
        name: bool(getattr(f, "requires_confirmation", False))
        for registry in (tools.functions, tools.async_functions)
        for name, f in registry.items()
    }


class TestConfirmation:
    def test_named_tools_are_flagged_and_the_rest_are_not(self):
        tools = WebTools(requires_confirmation_tools=["fetch_url"])
        assert _flags(tools) == {"fetch_url": True, "fetch_json": False}

    def test_nothing_is_flagged_by_default_or_when_empty(self):
        assert set(_flags(WebTools()).values()) == {False}
        assert set(_flags(WebTools(requires_confirmation_tools=[])).values()) == {False}

    def test_both_can_be_flagged(self):
        tools = WebTools(requires_confirmation_tools=["fetch_url", "fetch_json"])
        assert set(_flags(tools).values()) == {True}

    def test_unknown_names_flag_nothing_and_print_nothing(self, capfd, caplog):
        tools = WebTools(requires_confirmation_tools=["nope"])
        assert set(_flags(tools).values()) == {False}
        out = capfd.readouterr()
        assert out.out == "" and "not present" not in out.err
        assert "not present" not in caplog.text

    def test_a_flagged_toolkit_prints_nothing_at_construction(self, capfd, caplog):
        WebTools(requires_confirmation_tools=["fetch_url", "fetch_json"])
        out = capfd.readouterr()
        assert out.out == ""
        assert "not present" not in caplog.text + out.err


class TestFetching:
    async def test_a_failure_is_the_exact_one_line_error(self, serve):
        serve(httpx.ConnectError("connection refused"))
        result = await WebTools().fetch_url(URL)
        assert result == f"Error fetching {URL}: connection refused"

    async def test_fetch_json_failure_is_the_same_error(self, serve):
        serve(_resp("nope", status=404))
        result = await WebTools().fetch_json(URL)
        assert result.startswith(f"Error fetching {URL}: Client error '404 ")

    async def test_fetch_json_is_cut_at_the_json_limit_not_the_html_one(self, serve):
        serve(_resp("y" * 100, "application/json"))
        tools = WebTools(config=HttpFetcherConfig(max_json_chars=40, max_html_chars=5))
        assert await tools.fetch_json(URL) == "y" * 40

    async def test_fetch_json_default_limit_is_20000(self, serve):
        serve(_resp("y" * 30_000, "application/json"))
        assert len(await WebTools().fetch_json(URL)) == 20_000

    async def test_a_zero_max_length_returns_nothing_not_the_default(self, serve):
        serve(_resp("hello"))
        assert await WebTools().fetch_url(URL, max_length=0) == ""

    async def test_the_request_goes_out_as_igni_with_redirects_followed(self, serve):
        requests = serve(
            httpx.Response(302, headers={"location": "https://example.test/final"}),
            _resp("landed"),
        )
        assert await WebTools().fetch_url(URL) == "landed"
        assert [str(r.url) for r in requests] == [URL, "https://example.test/final"]
        assert requests[0].headers["user-agent"] == f"igni/{__version__}"

    async def test_the_broadcast_sink_hears_the_retries(self, serve):
        serve(_resp("busy", status=503), _resp("fine"))
        events: list[BroadcastEvent] = []
        tools = WebTools(broadcast=events.append)
        assert await tools.fetch_url(URL) == "fine"
        assert [type(e) for e in events] == [HttpRetryAttemptEvent, HttpRetrySucceededEvent]

    async def test_a_bug_that_is_not_http_propagates(self, serve):
        serve(ValueError("bug"))
        with pytest.raises(ValueError, match="bug"):
            await WebTools().fetch_url(URL)


class TestTheConfig:
    def test_the_defaults(self):
        config = HttpFetcherConfig()
        assert (config.timeout, config.follow_redirects) == (30, True)
        assert (config.max_html_chars, config.max_json_chars) == (10_000, 20_000)
        assert config.user_agent == f"igni/{__version__}"
        assert "mber" not in config.user_agent

    def test_it_is_frozen(self):
        with pytest.raises(ValidationError):
            HttpFetcherConfig().timeout = 1

    def test_the_httpx_kwargs_are_timeout_and_redirects_only(self):
        config = HttpFetcherConfig(timeout=5, follow_redirects=False)
        assert config.httpx_kwargs() == {"timeout": 5, "follow_redirects": False}

    def test_wrong_types_are_refused(self):
        with pytest.raises(ValidationError):
            HttpFetcherConfig(max_html_chars="lots")


class TestFetchResult:
    def test_success_carries_the_text(self):
        result = FetchResult.success("body")
        assert (result.ok, result.text, result.error) == (True, "body", "")
        assert result.text_or_error() == "body"

    def test_an_empty_success_is_still_a_success(self):
        result = FetchResult.success("")
        assert result.ok is True and result.text_or_error() == ""

    def test_failure_formats_the_error_once(self):
        result = FetchResult.failure("https://é.test/ü", RuntimeError("bäd"))
        assert (result.ok, result.text) == (False, "")
        assert result.error == "Error fetching https://é.test/ü: bäd"
        assert result.text_or_error() == result.error

    def test_failure_with_a_silent_exception_keeps_the_prefix(self):
        assert FetchResult.failure(URL, RuntimeError()).error == f"Error fetching {URL}: "


class TestRetryEvents:
    def test_the_attempt_event_publishes_on_its_channel(self):
        event = HttpRetryAttemptEvent(attempt=2, delay_seconds=0.1)
        assert event.channel == "http_retry_attempt"
        assert event.to_payload() == {"attempt": 2, "delay_seconds": 0.1}

    def test_the_succeeded_event_publishes_on_its_channel(self):
        event = HttpRetrySucceededEvent(url=URL, attempt_count=3, delay_seconds=0.3)
        assert event.channel == "http_retry_succeeded"
        assert event.to_payload() == {"url": URL, "attempt_count": 3, "delay_seconds": 0.3}

    def test_the_channel_cannot_be_overridden_and_fields_are_required(self):
        with pytest.raises(ValidationError):
            HttpRetryAttemptEvent(channel="other", attempt=2, delay_seconds=0.1)
        with pytest.raises(ValidationError):
            HttpRetryAttemptEvent(attempt=2)
        with pytest.raises(ValidationError):
            HttpRetrySucceededEvent(url=URL)
