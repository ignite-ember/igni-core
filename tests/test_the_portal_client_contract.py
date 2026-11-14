"""What :class:`PortalClient` does at the edges the other suites do not reach.

Real client, real ``httpx.AsyncClient`` over ``httpx.MockTransport``: the
requests it sends and what it answers when igni server answers badly.
"""

from __future__ import annotations

import logging
import webbrowser
from collections.abc import Callable

import httpx
import pytest

from igni_core.core.activity import ActivityRegistry
from igni_core.core.auth import portal_client as portal_module
from igni_core.core.auth.portal_client import PortalClient
from igni_core.core.config.endpoint import ApiUrlNotConfigured

API = "https://api.example.invalid"


@pytest.fixture
def wire(monkeypatch):
    """Route the client's HTTP through a handler; records requests, timeouts and busy state."""
    seen: dict = {"requests": [], "timeouts": [], "busy": []}
    real = httpx.AsyncClient

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> dict:
        def respond(request: httpx.Request) -> httpx.Response:
            seen["requests"].append(request)
            seen["busy"].append([(b.holder, b.kind) for b in ActivityRegistry.shared().busy()])
            return handler(request)

        def factory(*args, **kwargs):
            seen["timeouts"].append(kwargs.get("timeout"))
            kwargs["transport"] = httpx.MockTransport(respond)
            return real(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        return seen

    return install


class TestConfigured:
    def test_a_set_server_is_configured_and_none_is_not(self):
        assert PortalClient(API).configured is True
        assert PortalClient("").configured is False

    def test_the_api_url_loses_its_trailing_slashes(self):
        assert PortalClient(API + "//").api_url == API

    def test_a_portal_with_a_port_is_used_as_given(self):
        client = PortalClient(API, portal_url="https://p.example:8443/")
        assert client.login_url(9) == "https://p.example:8443/cli-auth?port=9"


class TestValidateToken:
    async def test_a_token_that_cannot_be_a_header_is_refused_not_raised(self, wire):
        seen = wire(lambda r: httpx.Response(200, json={}))
        result = await PortalClient(API).validate_token("tok-\u00fc")
        assert (result.ok, result.reason) == (False, "decode_error")
        assert seen["requests"] == []
        assert await PortalClient(API).fetch_group_pack("tok-\u00fc") is None

    async def test_it_sends_the_bearer_to_the_me_endpoint_with_the_timeout(self, wire):
        seen = wire(lambda r: httpx.Response(200, json={"email": "a@b.c", "full_name": "Ada"}))
        result = await PortalClient(API, http_timeout=7.5).validate_token("tok-1")
        [req] = seen["requests"]
        assert str(req.url) == f"{API}/v1/portal/me"
        assert req.headers["authorization"] == "Bearer tok-1"
        assert seen["timeouts"] == [7.5]
        assert result.ok and result.user is not None and result.user.full_name == "Ada"

    async def test_each_call_is_shown_as_busy_with_igni_server_while_it_runs(self, wire):
        seen = wire(lambda r: httpx.Response(200, json={}))
        await PortalClient(API).validate_token("t")
        [during] = seen["busy"]
        assert [holder for holder, _ in during] == ["igni-server"]
        assert ActivityRegistry.shared().busy() == []

    async def test_a_body_that_is_not_json_is_a_decode_error(self, wire):
        wire(lambda r: httpx.Response(200, content=b"<html>"))
        result = await PortalClient(API).validate_token("t")
        assert (result.ok, result.reason, result.status_code) == (False, "decode_error", 200)
        assert result.error

    @pytest.mark.parametrize("body", [{"email": 5}, ["a"], "text"])
    async def test_a_body_of_the_wrong_shape_is_a_schema_mismatch(self, wire, body):
        wire(lambda r: httpx.Response(200, json=body))
        result = await PortalClient(API).validate_token("t")
        assert (result.ok, result.reason, result.status_code) == (False, "schema_mismatch", 200)

    @pytest.mark.parametrize("status", [401, 403, 500])
    async def test_a_refusal_carries_its_status(self, wire, status):
        wire(lambda r: httpx.Response(status))
        result = await PortalClient(API).validate_token("t")
        assert (result.ok, result.reason, result.status_code) == (False, "http_error", status)

    async def test_a_dead_connection_is_a_network_error_not_a_raise(self, wire):
        def boom(request):
            raise httpx.ConnectError("refused")

        wire(boom)
        result = await PortalClient(API).validate_token("t")
        assert (result.ok, result.reason) == (False, "network_error")
        assert "refused" in result.error


class TestFetchJwks:
    async def test_it_reads_the_published_keys(self, wire):
        seen = wire(lambda r: httpx.Response(200, json={"keys": []}))
        jwks = await PortalClient(API + "/").fetch_jwks()
        assert jwks is not None and jwks.keys == []
        assert str(seen["requests"][0].url) == f"{API}/.well-known/jwks.json"
        assert [h for h, _ in seen["busy"][0]] == ["igni-server"]

    async def test_a_failed_fetch_is_none_and_never_raises(self, wire):
        def boom(request):
            raise httpx.ReadTimeout("slow")

        wire(boom)
        assert await PortalClient(API).fetch_jwks() is None

    async def test_a_non_200_is_none(self, wire):
        wire(lambda r: httpx.Response(503))
        assert await PortalClient(API).fetch_jwks() is None

    @pytest.mark.parametrize("content", [b"not json", b"[1]"])
    async def test_a_body_that_is_not_a_key_set_is_none(self, wire, content):
        wire(lambda r: httpx.Response(200, content=content))
        assert await PortalClient(API).fetch_jwks() is None

    async def test_unconfigured_it_refuses_naming_what_it_was_doing(self):
        with pytest.raises(ApiUrlNotConfigured, match="signing keys"):
            await PortalClient("").fetch_jwks()


class TestPackRefusalAndTimeout:
    async def test_unconfigured_it_refuses_naming_the_operation(self):
        with pytest.raises(ApiUrlNotConfigured, match="group policy"):
            await PortalClient("").fetch_group_pack("t")

    async def test_the_pack_poll_uses_the_configured_timeout_and_shows_busy(self, wire):
        seen = wire(lambda r: httpx.Response(204))
        assert await PortalClient(API, http_timeout=3.0).fetch_group_pack("t") is None
        assert seen["timeouts"] == [3.0]
        assert [h for h, _ in seen["busy"][0]] == ["igni-server"]

    async def test_an_empty_etag_is_sent_as_no_etag(self, wire):
        seen = wire(lambda r: httpx.Response(204))
        await PortalClient(API).fetch_group_pack("t", etag="")
        assert "if-none-match" not in seen["requests"][0].headers

    @pytest.mark.parametrize("content", [b"[]", b"3", b"not json"])
    async def test_a_body_that_is_not_a_pack_is_none(self, wire, content):
        wire(lambda r: httpx.Response(200, content=content))
        assert await PortalClient(API).fetch_group_pack("t") is None


class _FakeServer:
    """Stands in for the loopback callback server; only the browser hand-off is real."""

    port = 4321
    callback_url = "http://127.0.0.1:4321/callback"
    created: list[dict] = []

    def __init__(self, api_url=None, http_timeout=15.0):
        type(self).created.append({"api_url": api_url, "http_timeout": http_timeout})

    token: str | None = "the-token"
    wait_error: Exception | None = None
    enter_error: Exception | None = None

    async def __aenter__(self):
        if self.enter_error:
            raise self.enter_error
        return self

    async def __aexit__(self, *exc):
        return None

    async def wait_for_token(self, timeout=300.0):
        type(self).waited = timeout
        if self.wait_error:
            raise self.wait_error
        return self.token


@pytest.fixture
def server(monkeypatch):
    class Server(_FakeServer):
        created: list[dict] = []

    monkeypatch.setattr(portal_module, "CallbackServer", Server)
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    Server.opened = opened
    return Server


class TestStartCallback:
    def test_it_hands_the_server_the_api_url_and_timeout(self, server):
        client = PortalClient(API + "/", http_timeout=4.0)
        assert isinstance(client.start_callback(), server)
        assert server.created == [{"api_url": API, "http_timeout": 4.0}]

    def test_unconfigured_it_refuses_naming_signing_in(self):
        with pytest.raises(ApiUrlNotConfigured, match="Signing in"):
            PortalClient("").start_callback()


class TestRunLogin:
    async def test_a_token_comes_back_after_the_browser_is_opened_at_the_login_url(self, server):
        result = await PortalClient(API, http_timeout=2.0).run_login(timeout=9)
        assert (result.ok, result.token, result.callback_url) == (
            True,
            "the-token",
            server.callback_url,
        )
        assert server.opened == [f"{PortalClient(API).portal_url}/cli-auth?port=4321"]
        assert server.created == [{"api_url": API, "http_timeout": 2.0}]
        assert server.waited == 9

    async def test_no_callback_is_a_timeout_not_a_raise(self, server):
        server.token = None
        result = await PortalClient(API).run_login(timeout=0.1)
        assert (result.ok, result.token, result.reason) == (False, "", "timeout")
        assert result.callback_url == server.callback_url
        assert "timed out" in result.error

    async def test_a_port_that_cannot_be_bound_is_port_bind_failed(self, server):
        server.enter_error = OSError("address in use")
        result = await PortalClient(API).run_login()
        assert (result.ok, result.reason) == (False, "port_bind_failed")
        assert "address in use" in result.error

    async def test_anything_else_that_goes_wrong_is_handler_error(self, server):
        server.wait_error = RuntimeError("kaboom")
        result = await PortalClient(API).run_login()
        assert (result.ok, result.reason, result.error) == (False, "handler_error", "kaboom")

    @pytest.mark.parametrize("failure", [webbrowser.Error("no browser"), OSError("no display")])
    async def test_a_browser_that_will_not_open_is_logged_and_the_login_goes_on(
        self, server, monkeypatch, caplog, failure
    ):
        def refuse(url):
            raise failure

        monkeypatch.setattr(webbrowser, "open", refuse)
        with caplog.at_level(logging.WARNING, logger=portal_module.logger.name):
            result = await PortalClient(API).run_login()
        assert (result.ok, result.token) == (True, "the-token")
        [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert "/cli-auth?port=4321" in record.getMessage()

    async def test_unconfigured_it_is_not_a_login_that_quietly_fails(self, server):
        result = await PortalClient("").run_login()
        assert (result.ok, result.reason) == (False, "handler_error")
        assert "Signing in" in result.error
