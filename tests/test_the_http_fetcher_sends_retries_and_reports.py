"""``HttpFetcher`` against a real ``httpx`` client wired to an in-process server.

Only the transport is replaced (``httpx.MockTransport``): the request the
fetcher builds, the status handling, the retry loop and the events it
broadcasts are the fetcher's own. What the agent reads is pinned too: HTML
is reduced to visible text *before* it is cut, JSON and anything else is cut
raw, and a failed request is the one-line ``Error fetching <url>: ...``.
"""

from __future__ import annotations

import httpx
import pytest

from igni_core.core.broadcast_schema import BroadcastEvent
from igni_core.core.tools.http_fetcher import HttpFetcher
from igni_core.core.tools.web_schemas import (
    HttpFetcherConfig,
    HttpRetryAttemptEvent,
    HttpRetrySucceededEvent,
)

URL = "https://example.test/page"
_REAL_CLIENT = httpx.AsyncClient  # patched out of ``httpx`` while a test runs


@pytest.fixture
def serve(monkeypatch):
    """Serve ``responses`` in order (the last repeats); record every request."""
    requests: list[httpx.Request] = []

    def install(*responses):
        queue = list(responses)

        def handler(request: httpx.Request) -> httpx.Response:
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
    async def instant(_delay):  # backoff sleeps are not what is under test
        return None

    monkeypatch.setattr("igni_core.core.utils.http_retry.asyncio.sleep", instant)


def _text(body: str, content_type: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=body, headers={"content-type": content_type})


async def test_the_request_carries_the_configured_user_agent(serve):
    requests = serve(_text("hi", "text/plain"))

    await HttpFetcher(HttpFetcherConfig(user_agent="igni-test/9")).fetch_text(URL)

    [request] = requests
    assert request.headers["user-agent"] == "igni-test/9"
    assert request.method == "GET"


async def test_html_is_reduced_to_visible_text_before_it_is_cut(serve):
    html = (
        "<html><style>p{}</style><script>x()</script><body><p>Hello</p> <b>world</b></body></html>"
    )
    serve(_text(html, "text/html; charset=utf-8"))

    result = await HttpFetcher().fetch_text(URL, max_length=8)

    assert result.ok is True
    assert result.text == "Hello wo"  # 8 characters of text, not of markup


async def test_json_and_other_types_are_returned_raw_and_cut(serve):
    body = '{"a": "<b>x</b>", "b": 12345}'
    serve(_text(body, "application/ld+json"))
    assert (await HttpFetcher().fetch_text(URL, max_length=10)).text == body[:10]

    serve(_text("<b>raw</b> text", "text/plain"))
    assert (await HttpFetcher().fetch_text(URL, max_length=9)).text == "<b>raw</b>"[:9]

    serve(_text("<b>no type</b>", ""))
    assert (await HttpFetcher().fetch_text(URL)).text == "<b>no type</b>"


async def test_a_zero_limit_returns_empty_text(serve):
    serve(_text("hello", "text/plain"))

    result = await HttpFetcher().fetch_text(URL, max_length=0)

    assert (result.ok, result.text) == (True, "")


async def test_json_text_asks_for_json_and_cuts_at_the_json_limit(serve):
    requests = serve(_text("x" * 100, "application/json"))

    result = await HttpFetcher(
        HttpFetcherConfig(max_json_chars=30, max_html_chars=5, user_agent="ua/1")
    ).fetch_json_text(URL)

    assert result.text == "x" * 30
    [request] = requests
    assert request.headers["accept"] == "application/json"
    assert request.headers["user-agent"] == "ua/1"


@pytest.mark.parametrize("status", [400, 404, 403])
async def test_a_client_error_status_is_a_failure_and_is_not_retried(serve, status):
    requests = serve(_text("nope", "text/plain", status))

    result = await HttpFetcher().fetch_text(URL)

    assert result.ok is False
    assert result.text_or_error().startswith(f"Error fetching {URL}: Client error '{status} ")
    assert len(requests) == 1


async def test_json_text_failure_is_the_same_one_line_error(serve):
    serve(_text("nope", "text/plain", 404))

    result = await HttpFetcher().fetch_json_text(URL)

    assert result.ok is False
    assert result.error.startswith(f"Error fetching {URL}: ")


async def test_a_transient_failure_is_retried_and_the_recovery_is_broadcast(serve):
    requests = serve(
        _text("busy", "text/plain", 503),
        _text("busy", "text/plain", 429),
        _text("fine", "text/plain"),
    )
    events: list[BroadcastEvent] = []

    result = await HttpFetcher(broadcast=events.append).fetch_text(URL)

    assert result.text == "fine"
    assert len(requests) == 3
    attempts = [e for e in events if isinstance(e, HttpRetryAttemptEvent)]
    assert [e.attempt for e in attempts] == [2, 3]
    assert [e.delay_seconds for e in attempts] == [0.1, 0.2]
    [recovered] = [e for e in events if isinstance(e, HttpRetrySucceededEvent)]
    assert (recovered.url, recovered.attempt_count) == (URL, 3)
    assert recovered.delay_seconds == pytest.approx(0.3)
    assert events[-1] is recovered


async def test_a_timeout_is_retried_like_a_server_error(serve):
    requests = serve(httpx.ReadTimeout("slow"), _text("ok", "text/plain"))

    result = await HttpFetcher().fetch_text(URL)

    assert result.text == "ok"
    assert len(requests) == 2


async def test_with_no_sink_a_retry_is_silent_and_still_succeeds(serve):
    serve(_text("busy", "text/plain", 502), _text("fine", "text/plain"))

    assert (await HttpFetcher().fetch_text(URL)).text == "fine"


async def test_a_first_try_success_broadcasts_nothing(serve):
    serve(_text("fine", "text/plain"))
    events: list[BroadcastEvent] = []

    await HttpFetcher(broadcast=events.append).fetch_text(URL)

    assert events == []


async def test_a_server_that_never_recovers_fails_after_the_last_retry(serve):
    requests = serve(_text("down", "text/plain", 500))
    events: list[BroadcastEvent] = []

    result = await HttpFetcher(broadcast=events.append).fetch_text(URL)

    assert result.ok is False
    assert result.error.startswith(f"Error fetching {URL}: Server error '500 ")
    assert len(requests) == 11  # the first try and ten retries
    assert len(events) == 10
    assert all(isinstance(e, HttpRetryAttemptEvent) for e in events)


async def test_an_error_that_is_not_http_is_not_swallowed(serve):
    serve(ValueError("programming bug"))

    with pytest.raises(ValueError, match="programming bug"):
        await HttpFetcher().fetch_text(URL)
