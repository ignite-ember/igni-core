"""The egress proxy's allow-list, its refusals, its rewriting and its relays.

``test_an_extension_process_is_confined_to_its_grants.py`` shows the
proxy tunnelling, refusing and authenticating on the main path. This file
pins the rest of what a client of the proxy sees: how an allow-list entry
is read, the status a malformed, oversized or unreachable request gets
instead of a dropped connection, how plain HTTP is rewritten before it is
forwarded, the SOCKS5 reply codes, what a stop does to live tunnels, and
the loopback relay and its teardown. Everything runs over real sockets
with a connector that opens a local upstream.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from igni_core.extensions.sandbox import proxy as proxy_module
from igni_core.extensions.sandbox.proxy import (
    EgressProxy,
    HostAllowList,
    LoopbackRelay,
    canonical_host,
    stop_relays,
)

pytestmark = pytest.mark.extension_sandbox


@pytest.fixture
def short_dir() -> Iterator[Path]:
    # A unix socket path is limited to about a hundred characters.
    with tempfile.TemporaryDirectory(prefix="igni-px-") as name:
        yield Path(name)


class Echo:
    """An upstream that records what it is sent and says ``seen`` back."""

    def __init__(self) -> None:
        self.received: list[bytes] = []
        self.connected_to: list[tuple[str, int]] = []
        self._server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._answer, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _answer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        self.received.append(head)
        writer.write(b"seen")
        await writer.drain()
        writer.close()

    async def connect(self, host: str, port: int):
        self.connected_to.append((host, port))
        return await asyncio.open_connection("127.0.0.1", self.port)


@pytest.fixture
async def echo():
    server = Echo()
    await server.start()
    yield server
    await server.stop()


async def refusing_connect(host: str, port: int):
    raise ConnectionRefusedError(111, "nobody is listening")


async def hanging_connect(host: str, port: int):
    await asyncio.sleep(3600)


async def ask(port: int, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 10)
    writer.close()
    return data


@pytest.fixture
async def running():
    """Start proxies on loopback; stop them afterwards."""
    started: list[EgressProxy] = []

    async def start(proxy: EgressProxy) -> int:
        started.append(proxy)
        return await proxy.start_tcp()

    yield start
    for proxy in started:
        await proxy.stop()


# ── The allow-list: exact host and port, as written ───────────────────


def test_a_host_is_compared_without_case_brackets_spaces_or_a_trailing_dot():
    assert canonical_host("  Host.Example.COM. ") == "host.example.com"
    assert canonical_host("[::1]") == "::1"


def test_an_entry_without_a_port_is_https_and_one_with_a_port_is_that_port():
    assert HostAllowList.parse("api.test") == ("api.test", 443)
    assert HostAllowList.parse("api.test:8080") == ("api.test", 8080)
    assert HostAllowList.parse("[::1]:9000") == ("::1", 9000)
    assert HostAllowList.parse(" API.Test. ") == ("api.test", 443)


@pytest.mark.parametrize(
    "entry",
    ["", "   ", "host:notaport", "host:70000", ":443", "[::1", "[::1]:x", "host:-1"],
)
def test_an_entry_that_names_no_host_or_port_is_none(entry):
    assert HostAllowList.parse(entry) is None


def test_malformed_entries_are_dropped_and_the_rest_kept():
    allow = HostAllowList(["ok.test", "", "[::1", "bad:port", "[::1]:9", "ok.test:80"])
    assert allow.entries == ["[::1]:9", "ok.test:443", "ok.test:80"]


def test_a_host_is_permitted_only_on_the_exact_port_and_never_by_wildcard_or_subdomain():
    allow = HostAllowList(["api.test", "*.wild.test"])
    assert allow.permits("API.test.", 443)
    assert not allow.permits("api.test", 80)
    assert not allow.permits("sub.api.test", 443)
    assert not allow.permits("x.wild.test", 443)
    allow.add("Extra.Test", 8443)
    assert allow.permits("extra.test", 8443)


def test_an_empty_list_is_falsy_and_a_list_with_an_entry_is_truthy():
    assert not HostAllowList()
    assert not HostAllowList(["", "host:bad"])
    assert HostAllowList(["a.test"])


def test_the_proxy_url_carries_the_token_only_when_there_is_one():
    allow = HostAllowList()
    assert EgressProxy("x", allow).url(1234) == "http://127.0.0.1:1234"
    assert EgressProxy("x", allow, token="s3cret").url(1234) == "http://igni:s3cret@127.0.0.1:1234"


# ── A request the proxy cannot serve is answered, not dropped ─────────


async def test_a_connect_without_a_port_is_a_bad_request(running):
    port = await running(EgressProxy("acme.probe", HostAllowList(["a.test"])))
    for target in (b"a.test", b"[::1]", b"[::1"):
        answer = await ask(port, b"CONNECT " + target + b" HTTP/1.1\r\n\r\n")
        assert answer.startswith(b"HTTP/1.1 400 Bad Request"), target
        assert answer.endswith(b"CONNECT needs host:port"), target


async def test_a_request_line_that_is_not_http_is_a_bad_request(running):
    port = await running(EgressProxy("acme.probe", HostAllowList(["a.test"])))
    answer = await ask(port, b"NONSENSE\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 400 Bad Request")
    assert answer.endswith(b"not an HTTP request")


@pytest.mark.parametrize(
    "target",
    [
        b"/just/a/path",
        b"https://a.test/",
        b"ftp://a.test/",
        b"http:///nohost",
        b"http://a.test:99999/x",
        b"http://[::1/x",
    ],
)
async def test_only_absolute_http_requests_and_connect_are_proxied(running, target):
    port = await running(EgressProxy("acme.probe", HostAllowList(["a.test"])))
    answer = await ask(port, b"GET " + target + b" HTTP/1.1\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 400 Bad Request"), target
    assert answer.endswith(b"only absolute http:// requests and CONNECT are proxied"), target


async def test_a_head_over_64_kib_is_answered_431(running):
    port = await running(EgressProxy("acme.probe", HostAllowList(["a.test"])))
    big = b"GET http://a.test/ HTTP/1.1\r\nX-Fill: " + b"a" * 70_000 + b"\r\n\r\n"
    answer = await ask(port, big)
    assert answer.startswith(b"HTTP/1.1 431 Request Header Fields Too Large")


async def test_an_upstream_that_cannot_be_reached_is_a_502_over_http_and_code_5_over_socks(running):
    port = await running(
        EgressProxy("acme.probe", HostAllowList(["a.test"]), connect=refusing_connect)
    )
    connect = await ask(port, b"CONNECT a.test:443 HTTP/1.1\r\n\r\n")
    assert connect.startswith(b"HTTP/1.1 502 Bad Gateway")
    assert b"could not reach a.test:443" in connect and b"nobody is listening" in connect
    plain = await ask(port, b"GET http://a.test:443/ HTTP/1.1\r\n\r\n")
    assert plain.startswith(b"HTTP/1.1 502 Bad Gateway")
    assert b"could not reach a.test:443" in plain
    socks = await ask(port, b"\x05\x01\x00\x05\x01\x00\x03\x06a.test\x01\xbb")
    assert socks[2:4] == b"\x05\x05"


async def test_an_upstream_that_does_not_answer_in_time_is_a_502(running, monkeypatch):
    monkeypatch.setattr(proxy_module, "CONNECT_TIMEOUT_S", 0.05)
    port = await running(
        EgressProxy("acme.probe", HostAllowList(["a.test"]), connect=hanging_connect)
    )
    answer = await ask(port, b"CONNECT a.test:443 HTTP/1.1\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 502 Bad Gateway")


async def test_a_client_that_sends_nothing_in_time_is_dropped_without_a_reply(running, monkeypatch):
    monkeypatch.setattr(proxy_module, "HEAD_TIMEOUT_S", 0.05)
    port = await running(EgressProxy("acme.probe", HostAllowList(["a.test"])))
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    assert await asyncio.wait_for(reader.read(), 10) == b""
    writer.close()


async def test_a_client_that_leaves_in_the_middle_of_its_head_is_dropped_quietly(running, caplog):
    proxy = EgressProxy("acme.probe", HostAllowList(["a.test"]))
    port = await running(proxy)
    with caplog.at_level(logging.WARNING):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET http://a.test/ HTT")
        await writer.drain()
        writer.close()
        await asyncio.sleep(0.1)
    assert not caplog.records and proxy.refused == []


async def test_a_refusal_is_logged_with_the_extension_and_what_it_may_reach(running, caplog):
    port = await running(EgressProxy("acme.probe", HostAllowList(["b.test", "a.test:8080"])))
    with caplog.at_level(logging.WARNING):
        await ask(port, b"CONNECT [::1]:22 HTTP/1.1\r\n\r\n")
    assert "extension acme.probe tried to reach [::1]:22" in caplog.text
    assert "allowed: a.test:8080, b.test:443" in caplog.text


# ── Authentication: Basic for HTTP, user and password for SOCKS5 ──────


@pytest.mark.parametrize(
    "header",
    [
        b"",
        b"Proxy-Authorization: Basic aWduaTpub3BlCg==\r\n",
        b"Proxy-Authorization: Bearer s3cret\r\n",
        b"Proxy-Authorization: Basic\r\n",
        b"Proxy-Authorization: Basic \xe9\xe9\xe9\r\n",
    ],
)
async def test_http_without_the_exact_basic_credentials_is_a_407(running, header):
    port = await running(EgressProxy("acme.probe", HostAllowList(["a.test"]), token="s3cret"))
    answer = await ask(port, b"CONNECT a.test:443 HTTP/1.1\r\n" + header + b"\r\n")
    assert answer.startswith(b"HTTP/1.1 407 Proxy Authentication Required")
    assert b'Proxy-Authenticate: Basic realm="igni"' in answer
    assert b"igni sandbox: this is acme.probe's proxy" in answer


async def test_socks5_credentials_must_be_the_user_and_the_token(running, echo):
    port = await running(
        EgressProxy("acme.probe", HostAllowList(["a.test"]), connect=echo.connect, token="s3cret")
    )

    async def login(user: bytes, password: bytes, methods: bytes = b"\x02") -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"\x05" + bytes([len(methods)]) + methods)
        chosen = await reader.readexactly(2)
        if chosen != b"\x05\x02":
            writer.close()
            return chosen
        writer.write(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
        status = await reader.readexactly(2)
        writer.close()
        return status

    assert await login(b"igni", b"s3cret") == b"\x01\x00"
    assert await login(b"igni", b"wrong") == b"\x01\x01"
    assert await login(b"other", b"s3cret") == b"\x01\x01"
    # Without the user/password method on offer, there is no acceptable method.
    assert await login(b"", b"", methods=b"\x00") == b"\x05\xff"
    assert await login(b"", b"", methods=b"") == b"\x05\xff"


# ── SOCKS5: addresses and the replies for what it will not do ─────────


async def test_socks5_connects_to_an_ipv4_an_ipv6_and_a_domain_address(running, echo):
    port = await running(
        EgressProxy(
            "acme.probe",
            HostAllowList(["10.1.2.3:80", "[2001:db8::1]:80", "häfen.test:80"]),
            connect=echo.connect,
        )
    )

    async def reach(address: bytes) -> int:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"\x05\x01\x00" + b"\x05\x01\x00" + address + (80).to_bytes(2, "big"))
        await writer.drain()
        await reader.readexactly(2)
        reply = await reader.readexactly(10)
        writer.close()
        return reply[1]

    assert await reach(b"\x01" + bytes([10, 1, 2, 3])) == 0
    v6 = bytes.fromhex("20010db8000000000000000000000001")
    assert await reach(b"\x04" + v6) == 0
    name = "häfen.test".encode("idna")
    assert await reach(b"\x03" + bytes([len(name)]) + name) == 0
    assert echo.connected_to == [("10.1.2.3", 80), ("2001:db8::1", 80), ("häfen.test", 80)]
    # An address the list does not hold is refused, whatever its family.
    assert await reach(b"\x01" + bytes([10, 1, 2, 4])) == 2


@pytest.mark.parametrize(
    ("request_bytes", "code"),
    [
        (b"\x05\x02\x00\x01\x7f\x00\x00\x01\x00\x50", 7),  # BIND
        (b"\x05\x03\x00\x01\x7f\x00\x00\x01\x00\x50", 7),  # UDP ASSOCIATE
        (b"\x04\x01\x00\x01\x7f\x00\x00\x01\x00\x50", 7),  # not version 5
        (b"\x05\x01\x00\x09", 8),  # an address type that does not exist
        (b"\x05\x01\x00\x03\x02\xff\xfe\x01\xbb", 4),  # a name that is not a name
    ],
)
async def test_socks5_names_what_it_will_not_do_in_the_reply_code(running, request_bytes, code):
    port = await running(EgressProxy("acme.probe", HostAllowList(["127.0.0.1:80"])))
    answer = await ask(port, b"\x05\x01\x00" + request_bytes)
    assert answer[:2] == b"\x05\x00"
    assert answer[2:4] == bytes([5, code])


# ── Plain HTTP is rewritten before it is forwarded ────────────────────


async def test_plain_http_is_forwarded_in_origin_form_without_the_proxys_own_headers(running, echo):
    port = await running(
        EgressProxy("acme.probe", HostAllowList(["a.test:80"]), connect=echo.connect)
    )
    request = (
        b"POST http://A.test/path/x?q=1&r=2 HTTP/1.1\r\n"
        b"Host: a.test\r\n"
        b"Proxy-Authorization: Basic zzz\r\n"
        b"Proxy-Connection: keep-alive\r\n"
        b"Connection: keep-alive\r\n"
        b"Keep-Alive: 300\r\n"
        b"X-Kept: yes\r\n\r\n"
    )
    assert await ask(port, request) == b"seen"
    assert echo.connected_to == [("a.test", 80)]  # no port in the URL is port 80
    assert echo.received == [
        b"POST /path/x?q=1&r=2 HTTP/1.1\r\nHost: a.test\r\nX-Kept: yes\r\nConnection: close\r\n\r\n"
    ]


async def test_plain_http_to_a_host_with_no_path_asks_for_the_root(running, echo):
    port = await running(
        EgressProxy("acme.probe", HostAllowList(["a.test:80"]), connect=echo.connect)
    )
    await ask(port, b"GET http://a.test HTTP/1.1\r\n\r\n")
    assert echo.received[0].startswith(b"GET / HTTP/1.1\r\n")


# ── Start, stop ───────────────────────────────────────────────────────


async def test_stop_ends_the_tunnels_in_flight_and_the_listener():
    class Silent:
        async def connect(self, host: str, port: int):
            async def hold(reader, writer):
                await asyncio.sleep(3600)

            server = await asyncio.start_server(hold, "127.0.0.1", 0)
            self.server = server
            return await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])

    upstream = Silent()
    proxy = EgressProxy("acme.probe", HostAllowList(["a.test"]), connect=upstream.connect)
    port = await proxy.start_tcp()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"CONNECT a.test:443 HTTP/1.1\r\n\r\n")
    await writer.drain()
    assert b" 200 " in await reader.readuntil(b"\r\n\r\n")
    await asyncio.wait_for(proxy.stop(), 10)
    try:
        # The tunnel is torn down: the client reads end of file, not a hang.
        assert await asyncio.wait_for(reader.read(), 10) == b""
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", port)
    finally:
        writer.close()
        upstream.server.close()


async def test_a_unix_socket_proxy_is_private_replaces_a_stale_file_and_serves(short_dir, echo):
    path = short_dir / "proxy.sock"
    path.write_text("left behind by a crash")
    proxy = EgressProxy("acme.probe", HostAllowList(["a.test"]), connect=echo.connect)
    assert await proxy.start_unix(path) == path
    try:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(b"CONNECT evil.test:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        assert (await reader.read()).startswith(b"HTTP/1.1 403")
        writer.close()
    finally:
        await proxy.stop()


# ── The loopback relay, and tearing relays down ───────────────────────


async def test_a_loopback_relay_carries_bytes_to_the_port_and_is_private(short_dir):
    seen: list[bytes] = []

    async def serve(reader, writer):
        seen.append(await reader.readexactly(4))
        writer.write(b"pong")
        await writer.drain()
        writer.close()

    target = await asyncio.start_server(serve, "127.0.0.1", 0)
    relay = LoopbackRelay(target.sockets[0].getsockname()[1], short_dir / "relay.sock")
    await relay.start()
    try:
        assert stat.S_IMODE(os.stat(relay.path).st_mode) == 0o600
        reader, writer = await asyncio.open_unix_connection(str(relay.path))
        writer.write(b"ping")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 10) == b"pong"
        assert seen == [b"ping"]
        writer.close()
    finally:
        await relay.stop()
        target.close()
        await target.wait_closed()


async def test_a_loopback_relay_closes_at_once_when_its_port_is_down(short_dir):
    probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    dead_port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()
    relay = LoopbackRelay(dead_port, short_dir / "relay.sock")
    await relay.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(relay.path))
        assert await asyncio.wait_for(reader.read(), 10) == b""
        writer.close()
    finally:
        await relay.stop()


async def test_stopping_a_relay_twice_or_before_it_started_is_harmless(short_dir):
    await LoopbackRelay(1, short_dir / "never.sock").stop()
    relay = LoopbackRelay(1, short_dir / "relay.sock")
    await relay.start()
    await relay.stop()
    await relay.stop()


async def test_a_relay_that_will_not_stop_is_named_and_the_others_still_stop(short_dir, caplog):
    stopped: list[int] = []

    class Stuck(LoopbackRelay):
        async def stop(self) -> None:
            raise RuntimeError("the socket is wedged")

    class Fine(LoopbackRelay):
        async def stop(self) -> None:
            stopped.append(self.port)

    relays = [
        Fine(1001, short_dir / "a"),
        Stuck(1002, short_dir / "b"),
        Fine(1003, short_dir / "c"),
    ]
    with caplog.at_level(logging.WARNING):
        await stop_relays(relays)
    assert stopped == [1001, 1003]
    assert "a sandbox relay on port 1002 did not stop: the socket is wedged" in caplog.text
