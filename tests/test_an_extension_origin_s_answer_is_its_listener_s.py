"""An extension origin's ``answer`` is what its own listener answers.

``docs/design/local-cloud-switch.md`` §15.3. The file resolution, the
refusals, the content type and the CSP were inside the listener's request
handling; igni's port in the cloud (``extensions/page_files.py``) needs
them too, without the listener. So they became
:meth:`ExtensionOrigin.answer`, and the listener reads the head, checks the
Host and asks it. This holds the two to one another, reply for reply, over
the cases the origin's own tests serve and refuse.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from igni_core.extensions.origin import ExtensionOrigin, HttpReply

TARGETS = [
    "/",
    "/index.html",
    "/panel.js",
    "/assets/icon.svg",
    "/assets/",
    "/empty/",
    "/data.bin",
    "/missing.js",
    "/../secret.txt",
    "/%2e%2e/secret.txt",
    "/leak.txt",
    "//assets/icon.svg",
    "/index.html%00.js",
    "/index.html?v=1#top",
    "/" + "a" * 5000,
]


@pytest.fixture
async def origin(tmp_path: Path):
    root = tmp_path / "ui"
    (root / "assets").mkdir(parents=True)
    (root / "empty").mkdir()
    (root / "index.html").write_text("<!doctype html><title>x</title>")
    (root / "panel.js").write_text("export {};\n")
    (root / "assets" / "icon.svg").write_text("<svg/>")
    (root / "data.bin").write_bytes(b"\x00\x01")
    (tmp_path / "secret.txt").write_text("not yours")
    (root / "leak.txt").symlink_to(tmp_path / "secret.txt")
    served = ExtensionOrigin(
        "acme.reviewer",
        root,
        frame_ancestors=("tauri://localhost",),
        connect_src="'self' https://svc.test",
    )
    await served.start()
    yield served
    await served.stop()


async def _listener_s(origin: ExtensionOrigin, method: str, target: str) -> tuple[HttpReply, bool]:
    reader = asyncio.StreamReader()
    reader.feed_data(
        f"{method} {target} HTTP/1.1\r\nHost: 127.0.0.1:{origin.port}\r\n\r\n".encode("latin-1")
    )
    reader.feed_eof()
    return await origin._reply_to(reader)


def _same(one: HttpReply, other: HttpReply) -> None:
    assert one.encode_head() == other.encode_head()
    assert one.body == other.body
    assert one.file == other.file
    assert one.length == other.length


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_the_answer_is_byte_for_byte_the_listener_s(origin, method, target):
    reply, headers_only = await _listener_s(origin, method, target)

    _same(reply, origin.answer(method, target))
    assert headers_only is (method == "HEAD")


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "OPTIONS"])
async def test_a_method_the_origin_does_not_serve_is_refused_alike(origin, method):
    reply, _ = await _listener_s(origin, method, "/index.html")

    answered = origin.answer(method, "/index.html")
    _same(reply, answered)
    assert answered.status == 405 and answered.extra["Allow"] == "GET, HEAD"


async def test_the_host_check_stays_the_listener_s(origin):
    reader = asyncio.StreamReader()
    reader.feed_data(b"GET /index.html HTTP/1.1\r\nHost: evil.example\r\n\r\n")
    reader.feed_eof()

    reply, _ = await origin._reply_to(reader)

    assert reply.status == 421
    assert origin.answer("GET", "/index.html").status == 200, "answer() trusts its caller"
