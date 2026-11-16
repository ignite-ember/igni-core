"""A headless run builds its turn's prompt once the extensions' contributions are in.

Each READY extension's prompt text is read in the background
(``ExtensionContributions``). ``igni -m`` rebuilt its agents right after
the extensions started, before any of it arrived, so the run carried none
of an extension's prompt contributions, bundled files included (campaign
R295).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from igni_core.backend import headless_extensions as module
from igni_core.backend.headless_extensions import HeadlessExtensions
from igni_core.extensions.contributions import ExtensionContributions


async def test_settled_waits_for_the_text_being_read():
    contributions = ExtensionContributions.__new__(ExtensionContributions)
    done: list[str] = []

    async def read() -> None:
        await asyncio.sleep(0.01)
        done.append("read")

    contributions._pending = {asyncio.ensure_future(read())}

    await contributions.settled(timeout=5)

    assert done == ["read"]


async def test_the_run_rebuilds_its_agents_after_the_contributions_settle(monkeypatch):
    order: list[str] = []

    class _Store:
        def installed(self) -> list[str]:
            return ["bundle"]

    class _Launcher:
        async def start_installed(self, skip: Any = None) -> None:
            order.append("started")

    class _Contributions:
        async def settled(self, timeout: float) -> None:
            await asyncio.sleep(0)
            order.append("settled")

    session = SimpleNamespace(
        settings=SimpleNamespace(),
        reload_group_agents=lambda: order.append("rebuilt"),
    )
    extensions = HeadlessExtensions(session)

    def launcher(store: Any) -> _Launcher:
        extensions._contributions = _Contributions()  # type: ignore[assignment]
        return _Launcher()

    async def no_sync(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(module, "ExtensionStore", _Store)
    monkeypatch.setattr(extensions, "_launcher", launcher)
    monkeypatch.setattr(extensions, "_sync", no_sync)

    await extensions.start()

    assert order == ["started", "settled", "rebuilt"]


async def test_a_plugin_copied_in_as_its_extension_starts_is_loaded_in_that_run(
    monkeypatch, tmp_path
):
    """R299 / R276: the plugin is copied in as the extension starts, after
    the run loaded its skills, hooks and agents — the first run after an
    install answered "Unknown command", and only the next run had them."""
    order: list[str] = []
    plugins = tmp_path / "plugins"
    plugins.mkdir()

    class _Store:
        def installed(self) -> list[str]:
            return ["bundle"]

    class _Launcher:
        async def start_installed(self, skip: Any = None) -> None:
            (plugins / "local.plug").mkdir()
            order.append("started")

    session = SimpleNamespace(
        settings=SimpleNamespace(),
        reload_plugins=lambda: order.append("plugins reloaded"),
        reload_group_agents=lambda: order.append("rebuilt"),
    )
    extensions = HeadlessExtensions(session)

    def launcher(store: Any) -> _Launcher:
        extensions._plugins_dir = plugins
        return _Launcher()

    async def no_sync(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(module, "ExtensionStore", _Store)
    monkeypatch.setattr(extensions, "_launcher", launcher)
    monkeypatch.setattr(extensions, "_sync", no_sync)

    await extensions.start()

    assert order == ["started", "plugins reloaded", "rebuilt"]
