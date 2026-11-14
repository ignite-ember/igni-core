"""What an extension's UI port says to a request it cannot or will not serve.

``test_an_extension_origin_serves_only_its_own_files`` pins the files
served and the main refusals. This pins the rest of the protocol
surface over real sockets: a head too large or never finished, a
request line that is not one, a version that is not HTTP/1, a Host
header in every spelling, the targets that must answer as if nothing
were there, a file that changes while it is sent, a client that
leaves mid-body, and the registry that keeps one origin per extension.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import struct
from pathlib import Path

import pytest

from igni_core.extensions import origin as origin_module
from igni_core.extensions.origin import (
    FILE_CHUNK_BYTES,
    MAX_HEAD_BYTES,
    ExtensionOrigin,
    ExtensionOrigins,
    HttpReply,
)

pytestmark = pytest.mark.asyncio

LOGGER = "igni_core.extensions.origin"


async def _raw(port: int, payload: bytes, *, close_write: bool = False) -> tuple[int, dict, bytes]:
    """Send ``payload`` as it is and parse whatever comes back."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(payload)
        await writer.drain()
        if close_write:
            writer.write_eof()
        data = await asyncio.wait_for(reader.read(), timeout=5)
    finally:
        writer.close()
        with contextlib.suppress(OSError, asyncio.TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), timeout=1)
    head, _, body = data.partition(b"\r\n\r\n")
    status_line, _, block = head.partition(b"\r\n")
    headers = {}
    for line in block.decode("latin-1").splitlines():
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return int(status_line.split()[1]), headers, body


async def _ask(port: int, target: str = "/", *, host: str | None = "", extra: str = "") -> tuple:
    """A GET whose Host is the bound one unless told otherwise."""
    if host == "":
        host = f"127.0.0.1:{port}"
    host_line = f"Host: {host}\r\n" if host is not None else ""
    request = f"GET {target} HTTP/1.1\r\n{host_line}{extra}\r\n"
    return await _raw(port, request.encode("latin-1"))


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "ext" / "ui"
    root.mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html>home")
    (root / "panel.js").write_text("export {};\n")
    (root / "empty").mkdir()
    (root / "sub").mkdir()
    (root / "sub" / "index.html").write_text("sub home")
    return root


@pytest.fixture
async def origin(bundle: Path):
    o = ExtensionOrigin("acme.scanner", bundle)
    await o.start()
    try:
        yield o
    finally:
        await o.stop()


class TestALifeCycle:
    async def test_the_port_is_zero_and_the_url_is_an_error_until_started(self, bundle):
        o = ExtensionOrigin("acme.scanner", bundle)
        assert o.port == 0
        with pytest.raises(RuntimeError, match="acme.scanner"):
            _ = o.url

    async def test_start_is_idempotent_and_binds_loopback(self, origin):
        port = origin.port
        await origin.start()
        assert origin.port == port != 0
        assert origin.url == f"http://127.0.0.1:{port}"

    async def test_stop_twice_is_safe_and_closes_the_port(self, bundle):
        o = ExtensionOrigin("acme.scanner", bundle)
        await o.start()
        port = o.port
        await o.stop()
        await o.stop()
        assert o.port == 0
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", port)

    async def test_a_stopped_origin_serves_again_after_start(self, bundle):
        o = ExtensionOrigin("acme.scanner", bundle)
        await o.start()
        await o.stop()
        await o.start()
        try:
            status, _, body = await _ask(o.port)
            assert (status, body) == (200, b"<!doctype html>home")
        finally:
            await o.stop()


class TestAMalformedRequest:
    async def test_a_request_line_without_three_parts_is_a_400(self, origin):
        for line in ("GET /\r\n", "GET\r\n", "GET /  HTTP/1.1\r\n", "\r\n"):
            status, headers, _ = await _raw(origin.port, f"{line}\r\n".encode())
            assert status == 400, line
            assert "content-security-policy" in headers

    async def test_a_version_that_is_not_http_1_is_a_505(self, origin):
        for version in ("HTTP/2", "HTTP/2.0", "HTTP/0.9", "SPDY/3", "http/1.1"):
            status, _, _ = await _raw(origin.port, f"GET / {version}\r\n\r\n".encode())
            assert status == 505, version

    async def test_http_1_0_is_served(self, origin):
        request = f"GET / HTTP/1.0\r\nHost: 127.0.0.1:{origin.port}\r\n\r\n"
        status, _, body = await _raw(origin.port, request.encode())
        assert (status, body) == (200, b"<!doctype html>home")

    async def test_only_origin_form_targets_are_accepted(self, origin):
        for target in (f"http://127.0.0.1:{origin.port}/", "127.0.0.1:80", "*", "index.html"):
            status, _, _ = await _raw(origin.port, f"GET {target} HTTP/1.1\r\n\r\n".encode())
            assert status == 400, target

    async def test_a_head_over_the_limit_is_a_431_and_no_traceback(self, origin, caplog):
        padding = "X-Pad: " + "a" * (MAX_HEAD_BYTES + 100)
        request = f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{origin.port}\r\n{padding}\r\n\r\n"
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            status, _, _ = await _raw(origin.port, request.encode())
        assert status == 431
        assert all(r.exc_info is None for r in caplog.records)

    async def test_a_head_just_under_the_limit_is_still_served(self, origin):
        padding = "X-Pad: " + "a" * (MAX_HEAD_BYTES - 200)
        status, _, _ = await _ask(origin.port, extra=padding + "\r\n")
        assert status == 200

    async def test_a_client_that_says_nothing_gets_a_408_after_the_deadline(
        self, origin, monkeypatch
    ):
        monkeypatch.setattr(origin_module, "HEAD_TIMEOUT", 0.2)
        status, headers, body = await _raw(origin.port, b"")
        assert status == 408
        assert body == b"408 Request Timeout\n"
        assert "content-security-policy" in headers

    async def test_a_client_that_stops_mid_head_gets_a_408_not_a_traceback(self, origin, caplog):
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            status, _, _ = await _raw(origin.port, b"GET / HTTP/1.1\r\nHost: x", close_write=True)
        assert status == 408
        assert all(r.exc_info is None for r in caplog.records)

    async def test_a_client_that_leaves_before_the_answer_costs_nothing(self, origin, caplog):
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            reader, writer = await asyncio.open_connection("127.0.0.1", origin.port)
            writer.write(b"GET / HTTP/1.1\r\n")
            await writer.drain()
            writer.close()
            await asyncio.sleep(0.1)
            status, _, _ = await _ask(origin.port)
        assert status == 200
        assert all(r.exc_info is None for r in caplog.records)

    @pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "OPTIONS", "CONNECT", "get"])
    async def test_any_other_method_is_a_405_naming_the_two_allowed(self, origin, method):
        request = f"{method} / HTTP/1.1\r\nHost: 127.0.0.1:{origin.port}\r\n\r\n"
        status, headers, _ = await _raw(origin.port, request.encode())
        assert status == 405
        assert headers["allow"] == "GET, HEAD"


class TestTheHostHeader:
    async def test_the_bracketed_form_of_the_bound_address_is_ours(self, origin):
        status, _, _ = await _ask(origin.port, host=f"[127.0.0.1]:{origin.port}")
        assert status == 200

    @pytest.mark.parametrize(
        "host",
        ["localhost:{port}", "127.0.0.1", "127.0.0.1:1", "evil.test", None],
    )
    async def test_every_other_host_is_a_421(self, origin, host):
        if host is not None:
            host = host.format(port=origin.port)
        status, headers, _ = await _ask(origin.port, host=host)
        assert (status, "content-security-policy" in headers) == (421, True)

    async def test_the_name_is_case_insensitive_and_the_last_one_wins(self, origin):
        good = f"127.0.0.1:{origin.port}"
        status, _, _ = await _raw(origin.port, f"GET / HTTP/1.1\r\nHOST: {good}\r\n\r\n".encode())
        assert status == 200
        status, _, _ = await _raw(
            origin.port, f"GET / HTTP/1.1\r\nHost: evil.test\r\nhost: {good}\r\n\r\n".encode()
        )
        assert status == 200
        status, _, _ = await _raw(
            origin.port, f"GET / HTTP/1.1\r\nHost: {good}\r\nHost: evil.test\r\n\r\n".encode()
        )
        assert status == 421

    async def test_a_folded_header_is_not_a_host(self, origin):
        request = f"GET / HTTP/1.1\r\nX-A: 1\r\n Host: 127.0.0.1:{origin.port}\r\n\r\n"
        status, _, _ = await _raw(origin.port, request.encode())
        assert status == 421

    async def test_a_head_with_a_foreign_host_is_refused_without_a_body(self, origin):
        request = "HEAD / HTTP/1.1\r\nHost: evil.test\r\n\r\n"
        status, headers, body = await _raw(origin.port, request.encode())
        assert status == 421
        assert int(headers["content-length"]) > 0
        assert body == b""

    async def test_non_ascii_bytes_in_the_head_do_not_break_the_answer(self, origin):
        status, _, _ = await _ask(origin.port, extra="X-Name: caf\xe9\r\n")
        assert status == 200


class TestATargetThatIsNothing:
    async def _assert_same_404(self, origin, target: str) -> tuple:
        status, headers, body = await _ask(origin.port, target)
        assert status == 404, target
        assert body == b"404 Not Found\n"
        return headers

    async def test_each_of_these_answers_the_same_404(self, origin, bundle):
        secret = bundle.parent / "secret.txt"
        secret.write_text("not yours")
        (bundle / "link").symlink_to(secret)
        targets = [
            "/missing.js",
            "/empty/",
            "/empty",
            "/%00",
            "/panel.js%00.png",
            "/%2e%2e/secret.txt",
            "/%2E%2E%2fsecret.txt",
            "/sub/%2e%2e/%2e%2e/secret.txt",
            "/../secret.txt",
            "/link",
            "//panel.js",
            "//host/panel.js",
        ]
        reference = await self._assert_same_404(origin, targets[0])
        for target in targets[1:]:
            headers = await self._assert_same_404(origin, target)
            assert headers == reference | {k: headers[k] for k in ("content-length",)}

    async def test_a_symlinked_root_is_served_but_a_link_out_of_it_is_not(self, tmp_path, bundle):
        link = tmp_path / "linked-root"
        link.symlink_to(bundle, target_is_directory=True)
        o = ExtensionOrigin("acme.scanner", link)
        await o.start()
        try:
            status, _, body = await _ask(o.port, "/panel.js")
            assert (status, body) == (200, b"export {};\n")
        finally:
            await o.stop()

    async def test_a_percent_escape_is_decoded_before_the_file_is_found(self, origin, bundle):
        (bundle / "a b.txt").write_text("spaced")
        status, _, body = await _ask(origin.port, "/a%20b.txt")
        assert (status, body) == (200, b"spaced")

    async def test_a_fragment_is_ignored_and_a_directory_serves_its_index(self, origin):
        status, _, body = await _ask(origin.port, "/sub/#frag")
        assert (status, body) == (200, b"sub home")
        status, _, body = await _ask(origin.port, "/sub?x=1")
        assert (status, body) == (200, b"sub home")

    async def test_a_path_too_long_for_the_filesystem_is_a_404_not_a_crash(self, origin, caplog):
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            status, _, _ = await _ask(origin.port, "/" + "a" * 5000)
            alive, _, _ = await _ask(origin.port)
        assert (status, alive) == (404, 200)
        assert all(r.exc_info is None for r in caplog.records)

    async def test_a_symlink_loop_is_a_404_not_a_crash(self, origin, bundle, caplog):
        (bundle / "loop").symlink_to(bundle / "loop")
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            status, _, body = await _ask(origin.port, "/loop")
        assert (status, body) == (404, b"404 Not Found\n")
        assert all(r.exc_info is None for r in caplog.records)

    async def test_a_file_that_cannot_be_measured_is_a_404(self, origin, monkeypatch):
        ghost = origin.root / "ghost.js"
        monkeypatch.setattr(ExtensionOrigin, "_resolve", lambda self, target: ghost)
        status, _, body = await _ask(origin.port, "/ghost.js")
        assert (status, body) == (404, b"404 Not Found\n")


class TestWhatATypeIsServedAs:
    @pytest.mark.parametrize(
        "name, served",
        [
            ("a.HTML", "text/html; charset=utf-8"),
            ("a.Js", "text/javascript; charset=utf-8"),
            ("a.woff2", "font/woff2"),
            ("a.wasm", "application/wasm"),
            ("a.unknownext", "application/octet-stream"),
            ("noextension", "application/octet-stream"),
            ("a.htm", "application/octet-stream"),
            ("a.xml", "application/octet-stream"),
            ("a.php", "application/octet-stream"),
            ("a.mp3", "audio/mpeg"),
            ("a.mp4", "video/mp4"),
            ("a.bmp", "image/bmp"),
        ],
    )
    async def test_the_table_then_safe_host_guesses_then_nothing(
        self, origin, bundle, name, served
    ):
        (bundle / name).write_bytes(b"x")
        status, headers, _ = await _ask(origin.port, f"/{name}")
        assert status == 200
        assert headers["content-type"] == served

    async def test_a_host_guessed_type_never_overrides_the_table(self, origin, bundle, monkeypatch):
        monkeypatch.setattr(
            origin_module.mimetypes, "guess_type", lambda name: ("image/x-evil", None)
        )
        (bundle / "a.js").write_text("1")
        (bundle / "a.bin").write_text("1")
        _, js, _ = await _ask(origin.port, "/a.js")
        _, binary, _ = await _ask(origin.port, "/a.bin")
        assert js["content-type"] == "text/javascript; charset=utf-8"
        assert binary["content-type"] == "image/x-evil"


class TestASendThatGoesWrong:
    async def test_a_file_that_changes_between_measure_and_send_is_warned_about(
        self, origin, bundle, monkeypatch, caplog
    ):
        target = bundle / "grows.txt"
        target.write_text("12345")
        original = ExtensionOrigin._content_type

        def grow(self, path):
            path.write_text("1234567890")  # after the stat, before the read
            return original(self, path)

        monkeypatch.setattr(ExtensionOrigin, "_content_type", grow)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            reader, writer = await asyncio.open_connection("127.0.0.1", origin.port)
            writer.write(
                f"GET /grows.txt HTTP/1.1\r\nHost: 127.0.0.1:{origin.port}\r\n\r\n".encode()
            )
            await writer.drain()
            data = await asyncio.wait_for(reader.read(), timeout=5)
            writer.close()
        assert b"Content-Length: 5\r\n" in data
        assert [r.levelname for r in caplog.records] == ["WARNING"]
        assert "sent 10 bytes" in caplog.text and "declared 5" in caplog.text

    async def test_a_client_that_resets_mid_body_is_swallowed(self, origin, bundle, caplog):
        (bundle / "big.bin").write_bytes(b"\0" * (FILE_CHUNK_BYTES * 400))
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            reader, writer = await asyncio.open_connection("127.0.0.1", origin.port)
            writer.write(f"GET /big.bin HTTP/1.1\r\nHost: 127.0.0.1:{origin.port}\r\n\r\n".encode())
            await writer.drain()
            await reader.read(1024)
            sock = writer.get_extra_info("socket")
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            writer.transport.abort()
            await asyncio.sleep(0.3)
            status, _, _ = await _ask(origin.port)
        assert status == 200
        assert all(r.exc_info is None for r in caplog.records)

    async def test_a_large_file_arrives_whole(self, origin, bundle):
        payload = bytes(range(256)) * (FILE_CHUNK_BYTES // 256 * 5 + 3)
        (bundle / "big.bin").write_bytes(payload)
        status, headers, body = await _ask(origin.port, "/big.bin")
        assert status == 200
        assert int(headers["content-length"]) == len(payload)
        assert body == payload


class TestTheCspOfEveryReply:
    async def test_connect_src_and_ancestors_come_from_the_constructor(self, bundle):
        o = ExtensionOrigin(
            "acme.scanner",
            bundle,
            frame_ancestors=["http://localhost:5173", "tauri://localhost"],
            connect_src="'self' https://svc.test wss://svc.test",
        )
        await o.start()
        try:
            for target in ("/", "/missing"):
                _, headers, _ = await _ask(o.port, target)
                csp = headers["content-security-policy"]
                assert "connect-src 'self' https://svc.test wss://svc.test" in csp
                assert "frame-ancestors http://localhost:5173 tauri://localhost" in csp
        finally:
            await o.stop()

    async def test_no_ancestors_means_none_may_frame_it(self, origin):
        _, headers, _ = await _ask(origin.port)
        csp = headers["content-security-policy"]
        assert csp.endswith("frame-ancestors 'none'")
        assert "connect-src 'self';" in csp
        assert "object-src 'none'" in csp and "form-action 'none'" in csp

    async def test_the_properties_report_what_was_asked(self, bundle):
        o = ExtensionOrigin(
            "acme.scanner",
            bundle,
            connect_src="'self' https://s.test",
            service_url="https://s.test",
        )
        assert (o.extension_id, o.root, o.connect_src, o.service_url) == (
            "acme.scanner",
            bundle.resolve(),
            "'self' https://s.test",
            "https://s.test",
        )


class TestTheReplyOnTheWire:
    async def test_the_head_frames_status_type_length_and_the_fixed_headers(self):
        head = HttpReply(200, "OK", body=b"hello").encode_head().decode("latin-1")
        lines = head.split("\r\n")
        assert lines[0] == "HTTP/1.1 200 OK"
        assert head.endswith("\r\n\r\n")
        assert "Content-Type: text/plain; charset=utf-8" in lines
        assert "Content-Length: 5" in lines
        assert "Connection: close" in lines
        assert "X-Content-Type-Options: nosniff" in lines
        assert "Cross-Origin-Resource-Policy: same-origin" in lines
        assert "Cache-Control: no-store" in lines
        assert "Referrer-Policy: no-referrer" in lines

    async def test_a_declared_length_beats_the_body_length_and_extras_override(self):
        reply = HttpReply(
            404,
            "Not Found",
            length=99,
            content_type="x/y",
            extra={"Cache-Control": "max-age=1", "Allow": "GET"},
        )
        headers = reply.headers()
        assert headers["Content-Length"] == "99"
        assert headers["Content-Type"] == "x/y"
        assert headers["Cache-Control"] == "max-age=1"
        assert headers["Allow"] == "GET"


class TestOneOriginPerExtension:
    @pytest.fixture
    async def origins(self):
        registry = ExtensionOrigins(frame_ancestors=["http://localhost:5173"])
        try:
            yield registry
        finally:
            await registry.stop_all()

    async def test_unknown_ids_have_nothing(self, origins):
        assert origins.url_for("nobody") is None
        assert origins.service_url_for("nobody") is None
        assert origins.urls() == {}
        assert len(origins) == 0
        assert "nobody" not in origins
        await origins.drop("nobody")
        await origins.stop_all()

    async def test_a_running_origin_is_reported_by_every_accessor(self, origins, bundle):
        served = await origins.serve("a", bundle, service_url="https://s.test")
        assert "a" in origins and len(origins) == 1
        assert origins.url_for("a") == served.url
        assert origins.service_url_for("a") == "https://s.test"
        assert origins.urls() == {"a": served.url}
        _, headers, _ = await _ask(served.port)
        assert "frame-ancestors http://localhost:5173" in headers["content-security-policy"]

    async def test_the_same_arguments_return_the_running_origin(self, origins, bundle):
        first = await origins.serve("a", bundle, connect_src="'self' https://s.test")
        again = await origins.serve("a", bundle, connect_src="'self' https://s.test")
        assert again is first and len(origins) == 1

    async def test_a_changed_root_replaces_the_origin_and_stops_the_old_port(
        self, origins, bundle, tmp_path
    ):
        other = tmp_path / "other"
        other.mkdir()
        (other / "index.html").write_text("other home")
        first = await origins.serve("a", bundle)
        old_port = first.port
        second = await origins.serve("a", other)
        assert second is not first and len(origins) == 1
        assert first.port == 0
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", old_port)
        assert (await _ask(second.port))[2] == b"other home"

    @pytest.mark.parametrize(
        "change",
        [{"connect_src": "'self' https://new.test"}, {"service_url": "https://new.test"}],
    )
    async def test_a_changed_policy_replaces_the_origin_and_serves_the_new_csp(
        self, origins, bundle, change
    ):
        first = await origins.serve("a", bundle)
        second = await origins.serve("a", bundle, **change)
        assert second is not first and first.port == 0
        _, headers, _ = await _ask(second.port)
        expected = change.get("connect_src")
        if expected:
            assert f"connect-src {expected};" in headers["content-security-policy"]
        else:
            assert origins.service_url_for("a") == "https://new.test"

    async def test_racing_callers_leave_one_listener(self, origins, bundle):
        served = await asyncio.gather(*(origins.serve("a", bundle) for _ in range(8)))
        assert len({id(o) for o in served}) == 1
        assert len(origins) == 1

    async def test_drop_stops_one_and_leaves_the_rest(self, origins, bundle):
        a = await origins.serve("a", bundle)
        b = await origins.serve("b", bundle)
        await origins.drop("a")
        assert "a" not in origins and "b" in origins
        assert a.port == 0 and b.port != 0
        assert origins.urls() == {"b": b.url}

    async def test_stop_all_stops_every_origin_and_empties_the_registry(self, origins, bundle):
        a = await origins.serve("a", bundle)
        b = await origins.serve("b", bundle)
        ports = [a.port, b.port]
        await origins.stop_all()
        assert len(origins) == 0 and origins.urls() == {}
        for port in ports:
            with pytest.raises(OSError):
                await asyncio.open_connection("127.0.0.1", port)
