"""The marketplace panel's controller: what it lists, and on which thread it works.

``tests/test_plugins_backend.py`` pins the reply wording of every
operation. This pins the rest of :class:`MarketplaceController`: that
``list_registered`` reads the real registry file, that the git and
catalog work of add, remove and refresh runs in a worker thread rather
than on the event loop that serves every session, and that each call
builds its gateway from the session's settings as they are now.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from igni_core.backend.marketplace_controller import MarketplaceController
from igni_core.core.plugins.marketplace_store import MarketplaceRegistryStore
from igni_core.core.plugins.models import (
    MarketplaceCatalog,
    MarketplaceEntry,
    MarketplacePluginEntry,
    MarketplaceRegistry,
)


def _entry(name: str, plugins: int = 0) -> MarketplaceEntry:
    catalog = MarketplaceCatalog(
        name=name,
        plugins=[
            MarketplacePluginEntry(name=f"p{i}", source=f"https://p{i}.git") for i in range(plugins)
        ],
    )
    return MarketplaceEntry(
        name=name, url=f"https://{name}.git", last_fetched="2026-05-28T10:00:00Z", cached=catalog
    )


def _controller(data_dir: Path) -> MarketplaceController:
    session = MagicMock()
    session.settings.storage.data_dir = str(data_dir)
    return MarketplaceController(session)


def _register(data_dir: Path, *entries: MarketplaceEntry) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    MarketplaceRegistryStore(data_dir).save(MarketplaceRegistry(marketplaces=list(entries)))


def test_nothing_registered_lists_nothing(tmp_path: Path) -> None:
    assert _controller(tmp_path / "data").list_registered() == []


def test_the_registered_marketplaces_are_listed_with_their_cached_plugins(tmp_path: Path) -> None:
    _register(tmp_path, _entry("m1", plugins=2), _entry("m2"))

    listed = _controller(tmp_path).list_registered()

    assert [m.name for m in listed] == ["m1", "m2"]
    assert listed[0].url == "https://m1.git"
    assert listed[0].last_fetched == "2026-05-28T10:00:00Z"
    assert [p.name for p in listed[0].plugins] == ["p0", "p1"]
    assert [p.source for p in listed[0].plugins] == ["https://p0.git", "https://p1.git"]
    assert listed[1].plugins == []


def test_each_call_reads_the_data_dir_the_session_has_now(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    _register(first, _entry("in-a"))
    _register(second, _entry("in-b"))
    session = MagicMock()
    controller = MarketplaceController(session)

    session.settings.storage.data_dir = str(first)
    assert [m.name for m in controller.list_registered()] == ["in-a"]
    session.settings.storage.data_dir = str(second)
    assert [m.name for m in controller.list_registered()] == ["in-b"]


async def test_add_remove_and_refresh_work_off_the_event_loop(tmp_path: Path) -> None:
    loop_thread = threading.current_thread()
    seen: dict[str, threading.Thread] = {}

    def on(name: str, result: object):
        def run(*args: object, **kwargs: object) -> object:
            seen[name] = threading.current_thread()
            return result

        return run

    controller = _controller(tmp_path)
    with (
        patch.object(MarketplaceRegistryStore, "add", side_effect=on("add", _entry("m"))),
        patch.object(MarketplaceRegistryStore, "remove", side_effect=on("remove", True)),
        patch.object(MarketplaceRegistryStore, "refresh", side_effect=on("refresh", _entry("m"))),
    ):
        await controller.add("https://m.git")
        await controller.remove("m")
        await controller.refresh("m")

    assert set(seen) == {"add", "remove", "refresh"}
    assert all(thread is not loop_thread for thread in seen.values())


async def test_refreshing_all_works_off_the_event_loop_too(tmp_path: Path) -> None:
    loop_thread = threading.current_thread()
    seen: list[threading.Thread] = []
    _register(tmp_path, _entry("m1"))

    def refresh(name: str) -> MarketplaceEntry:
        seen.append(threading.current_thread())
        return _entry(name)

    with patch.object(MarketplaceRegistryStore, "refresh", side_effect=refresh):
        reply = await _controller(tmp_path).refresh(None)

    assert seen and all(thread is not loop_thread for thread in seen)
    assert "1 ok" in reply.text


async def test_an_empty_name_refreshes_every_marketplace(tmp_path: Path) -> None:
    _register(tmp_path, _entry("m1"), _entry("m2"))
    refreshed: list[str] = []

    def refresh(name: str) -> MarketplaceEntry:
        refreshed.append(name)
        return _entry(name)

    with patch.object(MarketplaceRegistryStore, "refresh", side_effect=refresh):
        reply = await _controller(tmp_path).refresh("")

    assert sorted(refreshed) == ["m1", "m2"]
    assert "2 ok" in reply.text
