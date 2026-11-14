"""The retry helper's edges: what it hands the callback, how long it waits,
which statuses it retries, and what it does with a failure chain that loops."""

from __future__ import annotations

import socket

import httpx
import pytest

from igni_core.core.utils import http_retry
from igni_core.core.utils.http_retry import _no_such_host, retry_with_backoff


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://example.test")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(code, request=request)
    )


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(http_retry.asyncio, "sleep", fake_sleep)
    return slept


def test_a_failure_chain_that_loops_does_not_hang_the_lookup():
    first, second = ValueError("a"), ValueError("b")
    first.__cause__ = second
    second.__cause__ = first

    assert _no_such_host(first) is False


def test_a_name_that_does_not_exist_is_found_through_the_context_too():
    try:
        try:
            raise socket.gaierror(socket.EAI_NONAME, "no such host")
        except socket.gaierror:
            raise httpx.ConnectError("connect failed")  # noqa: B904 - the implicit context is the case
    except httpx.ConnectError as exc:
        assert _no_such_host(exc) is True


def test_an_error_with_no_name_failure_behind_it_is_not_one():
    assert _no_such_host(httpx.ConnectError("refused")) is False


@pytest.mark.asyncio
async def test_the_callback_gets_the_next_attempt_number_and_the_delay(sleeps):
    calls: list[tuple[int, float]] = []
    attempts = 0

    async def flaky():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.TimeoutException("slow")
        return "ok"

    await retry_with_backoff(
        flaky,
        initial_delay=0.5,
        retry_callback=lambda attempt, delay: calls.append((attempt, delay)),
    )

    assert calls == [(2, 0.5), (3, 1.0)]
    assert sleeps == [0.5, 1.0]


@pytest.mark.asyncio
async def test_the_delay_stops_growing_at_the_cap(sleeps):
    async def always_slow():
        raise httpx.TimeoutException("slow")

    with pytest.raises(httpx.TimeoutException):
        await retry_with_backoff(always_slow, max_retries=5, initial_delay=1.0, max_delay=3.0)

    assert sleeps == [1.0, 2.0, 3.0, 3.0, 3.0]


@pytest.mark.asyncio
async def test_with_no_retries_the_first_failure_is_raised(sleeps):
    calls = 0

    async def slow():
        nonlocal calls
        calls += 1
        raise httpx.TimeoutException("slow")

    with pytest.raises(httpx.TimeoutException):
        await retry_with_backoff(slow, max_retries=0)

    assert calls == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_too_many_requests_is_retried(sleeps):
    calls = 0

    async def limited():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _status_error(429)
        return "ok"

    result, metadata = await retry_with_backoff(limited)

    assert result == "ok"
    assert metadata.retry_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [400, 404, 501])
async def test_other_statuses_are_not_retried(sleeps, code):
    calls = 0

    async def refused():
        nonlocal calls
        calls += 1
        raise _status_error(code)

    with pytest.raises(httpx.HTTPStatusError):
        await retry_with_backoff(refused)

    assert calls == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_an_error_that_is_not_httpx_comes_out_unwrapped_after_one_call(sleeps):
    calls = 0

    async def broken():
        nonlocal calls
        calls += 1
        raise KeyError("bug")

    with pytest.raises(KeyError):
        await retry_with_backoff(broken)

    assert calls == 1


@pytest.mark.asyncio
async def test_the_arguments_reach_the_function_and_the_retry_options_do_not(sleeps):
    seen: list[tuple[tuple, dict]] = []

    async def fetch(*args, **kwargs):
        seen.append((args, kwargs))
        return "done"

    result, _ = await retry_with_backoff(fetch, "url", 3, method="GET", max_retries=2)

    assert result == "done"
    assert seen == [(("url", 3), {"method": "GET"})]


@pytest.mark.asyncio
async def test_an_exhausted_retry_logs_the_attempts_it_made(sleeps, caplog):
    async def slow():
        raise httpx.TimeoutException("slow")

    with (
        caplog.at_level("ERROR", logger=http_retry.logger.name),
        pytest.raises(httpx.TimeoutException),
    ):
        await retry_with_backoff(slow, max_retries=2)

    assert "failed after 3 attempts" in caplog.text
