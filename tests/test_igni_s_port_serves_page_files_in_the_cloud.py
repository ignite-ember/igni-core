"""In the cloud, igni's own port serves the extensions' page files to the gateway.

``docs/design/local-cloud-switch.md`` §15.3. A plain ``GET`` under
``/.igni/pages/<id>/`` on igni's WebSocket port, before any upgrade, is
answered from that extension's own origin — the same files, refusals,
types and CSP — but only on a request the node agent marked
``X-Igni-Gateway: pages``, addressed to igni's own ``Host``. Driven here
against a real :class:`WebSocketServerTransport` and real
:class:`ExtensionOrigins`, over a raw socket, as the gateway asks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from igni_core.backend.transport_builder import TransportBuilder
from igni_core.core.layer import Layer, layers
from igni_core.extensions.origin import ExtensionOrigins
from igni_core.extensions.page_files import PageFiles
from igni_core.transport.websocket import WebSocketServerTransport
from igni_core.transport.ws_auth import frame_ancestor_sources

EXT = "acme.reviewer"


async def _get(
    port: int, path: str, *, mark: str | None = "pages", host: str | None = None
) -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    lines = [f"GET {path} HTTP/1.1", f"Host: {host or f'127.0.0.1:{port}'}"]
    if mark is not None:
        lines.append(f"X-Igni-Gateway: {mark}")
    lines += ["Connection: close", "User-Agent: igni-gateway", "", ""]
    try:
        writer.write("\r\n".join(lines).encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        status_line, _, block = head.partition(b"\r\n")
        headers: dict[str, list[str]] = {}
        for line in block.decode().splitlines():
            if ":" in line:
                name, _, value = line.partition(":")
                headers.setdefault(name.strip().lower(), []).append(value.strip())
        length = int(headers.get("content-length", ["0"])[0])
        body = await reader.readexactly(length) if length else b""
        flat = {name: values[0] for name, values in headers.items()}
        assert all(len(v) == 1 for v in headers.values()), headers
        return int(status_line.split()[1]), flat, body
    finally:
        writer.close()
        with contextlib.suppress(OSError, asyncio.TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), 1)


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "ext" / EXT / "ui"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>Reviewer</title>")
    (root / "app.js").write_text("export const ready = true;\n")
    (root / "assets" / "index.html").write_text("assets index")
    (tmp_path / "secret.txt").write_text("not yours")
    (root / "leak.txt").symlink_to(tmp_path / "secret.txt")
    return root


@pytest.fixture
async def served(bundle: Path) -> AsyncIterator[tuple[int, ExtensionOrigins, PageFiles]]:
    origins = ExtensionOrigins(frame_ancestors=frame_ancestor_sources())
    await origins.serve(EXT, bundle, connect_src="'self' https://svc.test")
    pages = PageFiles()
    pages.bind(origins)
    transport = WebSocketServerTransport(port=0, pages=pages)
    await transport.start()
    try:
        yield transport.port, origins, pages
    finally:
        await transport.close()
        await origins.stop_all()


async def test_a_marked_request_gets_the_file_with_the_extension_s_own_headers(served):
    port, origins, _ = served

    status, headers, body = await _get(port, f"/.igni/pages/{EXT}/index.html")

    assert status == 200
    assert body == b"<!doctype html><title>Reviewer</title>"
    assert headers["content-type"].startswith("text/html")
    assert headers["content-length"] == str(len(body))
    csp = headers["content-security-policy"]
    assert f"frame-ancestors {' '.join(frame_ancestor_sources())}" in csp
    assert "connect-src 'self' https://svc.test" in csp
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["cross-origin-resource-policy"] == "same-origin"
    assert headers["cache-control"] == "no-store"
    origin = origins.get(EXT)
    assert origin is not None
    assert csp == origin.answer("GET", "/index.html").extra["Content-Security-Policy"]


async def test_the_extension_alone_and_a_query_name_the_index(served):
    port, _, _ = served

    assert (await _get(port, f"/.igni/pages/{EXT}"))[0] == 200
    assert (await _get(port, f"/.igni/pages/{EXT}/"))[2].startswith(b"<!doctype")
    assert (await _get(port, f"/.igni/pages/{EXT}/app.js?v=3"))[
        2
    ] == b"export const ready = true;\n"


async def test_a_directory_is_served_as_its_index(served):
    port, _, _ = served

    status, _, body = await _get(port, f"/.igni/pages/{EXT}/assets/")

    assert (status, body) == (200, b"assets index")


@pytest.mark.parametrize("mark", [None, "control", "page", "pages,control"])
async def test_without_the_pages_mark_there_is_no_file(served, mark):
    port, _, _ = served

    status, _, body = await _get(port, f"/.igni/pages/{EXT}/index.html", mark=mark)

    assert status == 404 and b"Reviewer" not in body


@pytest.mark.parametrize("host", ["localhost:{port}", "127.0.0.1", "evil.example", "127.0.0.1:1"])
async def test_a_host_that_is_not_igni_s_own_is_misdirected(served, host):
    port, _, _ = served

    status, _, _ = await _get(port, f"/.igni/pages/{EXT}/index.html", host=host.format(port=port))

    assert status == 421


@pytest.mark.parametrize(
    "path",
    [
        "/.igni/pages/acme.absent/index.html",
        "/.igni/pages/Not-An-Id/index.html",
        "/.igni/pages/../index.html",
        f"/.igni/pages/{EXT}/../../secret.txt",
        f"/.igni/pages/{EXT}/%2e%2e/%2e%2e/secret.txt",
        f"/.igni/pages/{EXT}/leak.txt",
        f"/.igni/pages/{EXT}//index.html",
        f"/.igni/pages/{EXT}/index.html%00.js",
        f"/.igni/pages/{EXT}/missing.js",
    ],
)
async def test_what_the_origin_would_refuse_is_not_found(served, path):
    port, _, _ = served

    status, _, body = await _get(port, path)

    assert status == 404
    assert b"not yours" not in body


async def test_a_file_over_the_cap_is_not_served(served, bundle, monkeypatch, caplog):
    port, _, _ = served
    monkeypatch.setattr(PageFiles, "MAX_BYTES", 10)

    status, _, _ = await _get(port, f"/.igni/pages/{EXT}/index.html")

    assert status == 404
    assert "over the 10 a page file may be" in caplog.text


async def test_unbound_page_files_answer_nothing(bundle):
    transport = WebSocketServerTransport(port=0, pages=PageFiles())
    await transport.start()
    try:
        status, _, _ = await _get(transport.port, f"/.igni/pages/{EXT}/index.html")
    finally:
        await transport.close()

    assert status == 404


async def test_a_reinstall_that_moves_the_root_serves_the_new_files(served, tmp_path):
    port, origins, _ = served
    newer = tmp_path / "v2"
    newer.mkdir()
    (newer / "index.html").write_text("version two")

    await origins.serve(EXT, newer)

    assert (await _get(port, f"/.igni/pages/{EXT}/index.html"))[2] == b"version two"


async def test_a_websocket_upgrade_on_the_same_port_still_connects(served):
    port, _, _ = served

    async with connect(f"ws://127.0.0.1:{port}") as window:
        welcome = json.loads(await asyncio.wait_for(window.recv(), 10))

    assert welcome["type"] == "welcome"


async def test_an_upgrade_under_the_prefix_is_a_websocket_not_a_file(served):
    """A page request is a plain GET; an upgrade is the guard's, whatever its path."""
    port, _, _ = served

    async with connect(f"ws://127.0.0.1:{port}/.igni/pages/{EXT}/index.html") as window:
        welcome = json.loads(await asyncio.wait_for(window.recv(), 10))

    assert welcome["type"] == "welcome"


async def test_a_plain_get_elsewhere_gets_the_guard_s_answer(served):
    port, _, _ = served

    status, _, body = await _get(port, "/index.html")

    assert status != 200 and b"Reviewer" not in body


# ── Which layers serve them ───────────────────────────────────────


class _ServesPages(Layer):
    def serves_page_files(self) -> bool:
        return True


async def test_where_the_layer_serves_no_pages_the_port_answers_no_file(monkeypatch):
    builder = TransportBuilder(socket_path=None, ws_port=0)
    transport = await builder.build()
    try:
        assert builder.page_files is None
        status, _, body = await _get(builder.ws_transport.port, f"/.igni/pages/{EXT}/index.html")  # type: ignore[union-attr]
    finally:
        await transport.close()

    # The guard's answer: refused, or a failed upgrade. Never a file.
    assert status in (403, 426) and b"Reviewer" not in body


async def test_where_the_layer_serves_pages_the_builder_gives_the_port_page_files(monkeypatch):
    monkeypatch.setattr(layers, "current", lambda: _ServesPages())
    builder = TransportBuilder(socket_path=None, ws_port=0)
    transport = await builder.build()
    try:
        assert isinstance(builder.page_files, PageFiles)
        status, _, _ = await _get(builder.ws_transport.port, f"/.igni/pages/{EXT}/index.html")  # type: ignore[union-attr]
    finally:
        await transport.close()

    assert status == 404, "page files, unbound until the extension runtime exists"


def test_the_prefix_and_the_cap_are_the_contract():
    assert PageFiles.PREFIX == "/.igni/pages/"
    assert PageFiles.MAX_BYTES == 32 * 1024 * 1024


@pytest.mark.skipif(sys.platform == "win32", reason="the loop's signal handlers")
async def test_the_backend_binds_its_page_files_to_the_extension_runtime_s_origins(
    tmp_path, monkeypatch
):
    """The transport listens before the extension runtime exists; the boot
    binds the two once it does."""
    from igni_core.backend.app import BackendApp
    from igni_core.backend.schemas_lifecycle import BackendLaunch

    monkeypatch.setattr(layers, "current", lambda: _ServesPages())
    monkeypatch.delenv("IGNI_PARENT_PID", raising=False)
    (tmp_path / ".igni").mkdir()
    app = BackendApp(BackendLaunch(ws_port=0, project_dir=tmp_path))
    loop = asyncio.get_running_loop()
    running = asyncio.create_task(app.run())
    try:
        async with asyncio.timeout(30):
            while app.serving_port is None and not running.done():
                await asyncio.sleep(0.02)
        assert not running.done(), running.exception()
        page_files = app._transport_builder.page_files
        assert page_files is not None and page_files._origins is not None
        status, _, _ = await _get(app.serving_port, f"/.igni/pages/{EXT}/index.html")  # type: ignore[arg-type]
        assert status == 404, "bound, and no such extension is running"
    finally:
        app.request_shutdown()
        await asyncio.wait_for(running, 30)
        for sig in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGUSR1", None)):
            if sig is not None:
                loop.remove_signal_handler(sig)
