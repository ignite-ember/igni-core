"""An extension's page is framed by igni's own app, and by nothing else.

The app shows a page in an ``<iframe>``, so the origin that serves it
must name the app's origins in ``frame-ancestors``: the same set that
may open the backend's WebSocket (``transport/ws_auth.py``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from igni_core.backend import headless_extensions
from igni_core.backend.extensions_runtime import ExtensionsRuntime
from igni_core.core.prompts import PromptFragments, PromptLoader
from igni_core.core.prompts.contributions import PromptContributions
from igni_core.extensions.origin import ExtensionOrigins
from igni_core.extensions.store import ExtensionStore
from igni_core.transport.ws_auth import TAURI_ORIGINS, frame_ancestor_sources, origin_allowed
from tests.test_a_headless_run_starts_its_extensions import Session, _install_guard


def _ancestors(csp: str) -> list[str]:
    (directive,) = [d for d in csp.split("; ") if d.startswith("frame-ancestors")]
    return directive.split()[1:]


async def _csp_of(origins: ExtensionOrigins, tmp: Path) -> str:
    root = tmp / "ui"
    root.mkdir(exist_ok=True)
    (root / "index.html").write_text("hi")
    origin = await origins.serve("acme.page", root)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", origin.port)
        writer.write(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{origin.port}\r\n\r\n".encode())
        await writer.drain()
        head = (await reader.read(65536)).decode("latin-1")
        writer.close()
    finally:
        await origins.stop_all()
    (line,) = [ln for ln in head.split("\r\n") if ln.lower().startswith("content-security-policy")]
    return line.split(":", 1)[1].strip()


def _assert_only_igni_app(csp: str) -> None:
    sources = _ancestors(csp)
    assert "'none'" not in sources
    assert set(TAURI_ORIGINS) <= set(sources)
    for scheme in ("http", "https"):
        for host in ("127.0.0.1", "localhost", "[::1]"):
            assert f"{scheme}://{host}:*" in sources
    assert len(sources) == len(TAURI_ORIGINS) + 6
    # Nothing in the list is wider than what may open the WebSocket.
    for source in sources:
        assert origin_allowed(source.replace(":*", ":5173"))
    assert "*" not in [s for s in sources if "://" not in s]


def test_the_sources_are_the_ws_origins_written_as_csp_host_sources() -> None:
    sources = frame_ancestor_sources()
    assert set(TAURI_ORIGINS) <= set(sources)
    assert "http://[::1]:*" in sources and "https://localhost:*" in sources
    assert not any(s in ("*", "http:", "https:") for s in sources)


async def test_the_runtime_builds_origins_the_app_may_frame(tmp_path: Path) -> None:
    runtime = ExtensionsRuntime.build(
        mcp=object(),
        plugins_dir=tmp_path / "plugins",
        store=ExtensionStore(),
        prompts=PromptContributions(),
        prompt_loader=PromptLoader(PromptFragments()),
    )
    _assert_only_igni_app(await _csp_of(runtime.origins, tmp_path))


async def test_the_headless_run_builds_origins_the_app_may_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IGNI_DEV_EXTENSIONS", "1")
    _install_guard(tmp_path)
    seen: list[ExtensionOrigins] = []
    real = headless_extensions.ExtensionInstaller

    def spy(**kwargs: Any) -> Any:
        seen.append(kwargs["origins"])
        return real(**kwargs)

    monkeypatch.setattr(headless_extensions, "ExtensionInstaller", spy)
    extensions = headless_extensions.HeadlessExtensions(Session(tmp_path))
    await extensions.start()
    try:
        assert len(seen) == 1
        _assert_only_igni_app(await _csp_of(seen[0], tmp_path))
    finally:
        await extensions.stop()
