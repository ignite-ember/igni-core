"""The local server a browser sign-in redirects back to, driven over real HTTP.

The handler and the server are real; only the exchange call to igni
server (``httpx.post``) is answered by the test. ``test_auth_client.py``
pins the code, the refusals and the address; this file pins what the
browser is told on success, what a malformed exchange answer does, and
how the CLI waits for the token.
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest

from igni_core.core.auth.callback_server import CallbackServer

API = "https://api.example.test"


def _get(url: str) -> tuple[int, dict[str, str], str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 — the test's own loopback URL
            return response.status, dict(response.headers), response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode()


@contextmanager
def _serving(monkeypatch, answer, api_url: str | None = API) -> Iterator[CallbackServer]:
    """A callback server serving requests, whose exchange answers ``answer(url, json)``."""
    posted: list[tuple[str, object]] = []

    def post(url, json=None, timeout=None):  # noqa: ANN001, ARG001
        posted.append((url, json))
        return answer(url, json)

    monkeypatch.setattr(httpx, "post", post)
    cb = CallbackServer(api_url=api_url)
    cb.posted = posted  # type: ignore[attr-defined]
    try:
        yield cb
    finally:
        cb.stop()


def _token_answer(token: str = "tok-1"):
    return lambda url, body: httpx.Response(200, json={"access_token": token})


class TestWhatTheBrowserIsTold:
    def test_success_shows_the_page_with_no_referrer_and_no_secret(self, monkeypatch):
        with _serving(monkeypatch, _token_answer("secret-jwt")) as cb:
            status, headers, body = _get(f"{cb.callback_url}?code=c1")
        assert status == 200
        assert headers["Referrer-Policy"] == "no-referrer"
        assert "text/html" in headers["Content-Type"]
        assert "<title>CLI Authenticated</title>" in body
        assert "secret-jwt" not in body

    def test_every_refusal_is_the_same_400_page(self, monkeypatch):
        pages = set()
        answers = [
            None,  # no code at all
            lambda url, body: httpx.Response(500, text="boom"),
            lambda url, body: httpx.Response(200, json={"access_token": ""}),
            lambda url, body: httpx.Response(200, json={"access_token": None}),
            lambda url, body: httpx.Response(200, json={}),
        ]
        for answer in answers:
            query = "" if answer is None else "?code=c"
            with _serving(monkeypatch, answer or _token_answer()) as cb:
                status, headers, body = _get(f"{cb.callback_url}{query}")
                assert cb._token is None
            assert status == 400
            assert "Referrer-Policy" not in headers
            pages.add(body)
        assert len(pages) == 1
        assert "Sign-in did not complete" in pages.pop()

    def test_an_empty_code_is_a_missing_code(self, monkeypatch):
        with _serving(
            monkeypatch, lambda *a: pytest.fail("an empty code must not be redeemed")
        ) as cb:
            status, _, _ = _get(f"{cb.callback_url}?code=")
        assert status == 400
        assert cb._token is None

    def test_the_path_does_not_matter_only_the_code(self, monkeypatch):
        with _serving(monkeypatch, _token_answer("t")) as cb:
            status, _, _ = _get(f"http://127.0.0.1:{cb.port}/anything?code=c")
        assert status == 200
        assert cb._token == "t"


class TestRedeemingTheCode:
    def test_the_code_goes_to_the_exchange_of_the_server_without_a_double_slash(self, monkeypatch):
        with _serving(monkeypatch, _token_answer(), api_url=f"{API}/") as cb:
            _get(f"{cb.callback_url}?code=abc")
        assert cb.posted == [(f"{API}/v1/auth/exchange", {"code": "abc"})]

    def test_a_hostile_code_is_passed_on_as_data_and_nothing_more(self, monkeypatch):
        with _serving(monkeypatch, _token_answer()) as cb:
            _get(f"{cb.callback_url}?code=%C3%A9%2F..%2F%22&code=second")
        assert cb.posted == [(f"{API}/v1/auth/exchange", {"code": 'é/../"'})]

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(200, text="<html>a proxy's page</html>"),
            httpx.Response(200, json=["not", "an", "object"]),
            httpx.Response(200, json="a string"),
            httpx.Response(200, content=b""),
        ],
        ids=["html", "list", "string", "empty"],
    )
    def test_an_answer_that_is_not_a_json_object_is_a_refusal_the_browser_sees(
        self, monkeypatch, response
    ):
        with _serving(monkeypatch, lambda url, body: response) as cb:
            status, _, body = _get(f"{cb.callback_url}?code=c")
        assert status == 400
        assert "Sign-in did not complete" in body
        assert cb._token is None


class TestWaitingForTheToken:
    async def test_a_token_that_already_arrived_is_returned_at_once(self):
        cb = CallbackServer()
        try:
            cb._deliver_token("early")
            assert await cb.wait_for_token(timeout=0.01) == "early"
        finally:
            cb.stop()

    async def test_a_token_arriving_while_waiting_wakes_the_waiter(self, monkeypatch):
        with _serving(monkeypatch, _token_answer("late")) as cb:
            waiter = asyncio.create_task(cb.wait_for_token(timeout=5))
            await asyncio.sleep(0.05)
            status, _, _ = await asyncio.to_thread(_get, f"{cb.callback_url}?code=c")
            assert status == 200
            assert await waiter == "late"

    async def test_no_token_in_time_is_none(self):
        cb = CallbackServer()
        try:
            assert await cb.wait_for_token(timeout=0.05) is None
        finally:
            cb.stop()

    async def test_waiting_again_after_a_timeout_still_gets_the_token(self):
        cb = CallbackServer()
        try:
            assert await cb.wait_for_token(timeout=0.05) is None
            waiter = asyncio.create_task(cb.wait_for_token(timeout=5))
            await asyncio.sleep(0.05)
            await asyncio.to_thread(cb._deliver_token, "after-retry")
            assert await waiter == "after-retry"
        finally:
            cb.stop()

    async def test_a_token_delivered_as_the_wait_begins_is_not_lost(self):
        """The request thread can deliver between the check for a token and the
        waiter registering its future; the waiter must still return it."""
        cb = CallbackServer()
        loop = asyncio.get_running_loop()
        create = loop.create_future

        delivered = []

        def create_then_deliver():
            future = create()
            if not delivered:  # the waiter's own future; asyncio makes others after it
                delivered.append(True)
                cb._deliver_token("in-the-gap")  # the handler thread, at that instant
            return future

        loop.create_future = create_then_deliver  # type: ignore[method-assign]
        try:
            assert await cb.wait_for_token(timeout=0.5) == "in-the-gap"
        finally:
            del loop.create_future
            cb.stop()


class TestLifetime:
    async def test_the_context_manager_stops_the_server_on_exit(self):
        async with CallbackServer() as cb:
            port = cb.port
        assert cb._server.socket.fileno() == -1
        with pytest.raises(OSError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/callback", timeout=1)  # noqa: S310

    def test_the_serving_thread_ends_once_a_token_was_delivered(self, monkeypatch):
        with _serving(monkeypatch, _token_answer()) as cb:
            _get(f"{cb.callback_url}?code=c")
            cb._thread.join(timeout=5)
            assert not cb._thread.is_alive()

    def test_two_servers_keep_their_own_ports_and_tokens(self, monkeypatch):
        with (
            _serving(monkeypatch, _token_answer("one")) as a,
            _serving(monkeypatch, _token_answer("two")) as b,
        ):
            assert a.port != b.port
            _get(f"{b.callback_url}?code=c")
            assert b._token == "two"
            assert a._token is None
