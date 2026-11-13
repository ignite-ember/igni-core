"""``MarketplaceWarmupPhase``: auto-registration, timeouts and the refresh window.

A fake store records what the phase asks of it; nothing touches the network.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from igni_core.core.config.schemas.plugins import PluginsConfig
from igni_core.core.config.settings import Settings, StorageConfig
from igni_core.core.plugins.marketplace_store import DEFAULT_MARKETPLACES
from igni_core.core.plugins.models import MarketplaceCatalog, MarketplaceEntry, MarketplaceRegistry
from igni_core.core.session.startup import MarketplaceWarmupPhase

DEFAULT_NAME, DEFAULT_URL = DEFAULT_MARKETPLACES[0]
ORG_URL = "https://git.example.com/acme/plugins"


class _Store:
    def __init__(self, entries=(), *, add_error=None, add_delay=0.0, refresh_error=None):
        self.registry = MarketplaceRegistry(marketplaces=list(entries))
        self.added: list[tuple[str, str]] = []
        self.refreshed: list[str] = []
        self.add_error = add_error
        self.add_delay = add_delay
        self.refresh_error = refresh_error

    def load(self):
        return self.registry

    def add(self, url, *, added_by="user"):
        if self.add_delay:
            time.sleep(self.add_delay)
        if self.add_error:
            raise self.add_error
        self.added.append((url, added_by))

    def refresh(self, name):
        if self.refresh_error:
            raise self.refresh_error
        self.refreshed.append(name)


def _settings(tmp_path: Path, **plugins) -> Settings:
    return Settings(storage=StorageConfig(data_dir=str(tmp_path)), plugins=PluginsConfig(**plugins))


def _run(settings: Settings, store: _Store, **tune) -> MarketplaceWarmupPhase:
    phase = MarketplaceWarmupPhase(settings_ref=lambda: settings, store_for=lambda _d: store)
    for k, v in tune.items():
        setattr(phase, k, v)
    asyncio.run(phase._refresh_all())
    return phase


def _entry(name, url, **kw) -> MarketplaceEntry:
    return MarketplaceEntry(name=name, url=url, **kw)


def test_the_catalog_window_is_six_hours():
    assert MarketplaceWarmupPhase.REFRESH_EVERY_SECONDS == 6 * 3600.0


def test_the_default_is_registered_as_default_and_the_organisations_own_as_user(tmp_path):
    store = _Store()
    _run(_settings(tmp_path, default_marketplace=True, marketplaces=[ORG_URL]), store)
    assert store.added == [(DEFAULT_URL, "default"), (ORG_URL, "user")]


def test_a_name_already_registered_under_another_url_is_not_registered_again(tmp_path):
    store = _Store([_entry(DEFAULT_NAME, "https://mirror.example/x", last_fetched=None)])
    _run(_settings(tmp_path, default_marketplace=True), store)
    assert store.added == []


def test_a_url_already_registered_under_another_name_is_not_registered_again(tmp_path):
    store = _Store([_entry("mine", DEFAULT_URL)])
    _run(_settings(tmp_path, default_marketplace=True), store)
    assert store.added == []


def test_no_default_is_registered_when_the_default_marketplace_is_off(tmp_path):
    store = _Store()
    _run(_settings(tmp_path, default_marketplace=False), store)
    assert store.added == []


def test_a_failing_add_is_logged_and_the_next_marketplace_is_still_added(tmp_path, caplog):
    store = _Store(add_error=RuntimeError("clone refused"))
    with caplog.at_level(logging.WARNING):
        _run(_settings(tmp_path, default_marketplace=True, marketplaces=[ORG_URL]), store)
    messages = [r.getMessage() for r in caplog.records]
    assert any(DEFAULT_URL in m and "clone refused" in m for m in messages)
    assert any(ORG_URL in m and "clone refused" in m for m in messages)


def test_a_slow_add_is_abandoned_after_its_timeout_and_the_rest_continue(tmp_path, caplog):
    store = _Store(add_delay=0.5)
    with caplog.at_level(logging.WARNING):
        _run(
            _settings(tmp_path, default_marketplace=True, marketplaces=[ORG_URL]),
            store,
            _TIMEOUT_ADD=0.05,
        )
    assert sum("Auto-registering marketplace" in r.getMessage() for r in caplog.records) == 2


def test_a_slow_refresh_times_out_and_the_next_entry_is_still_refreshed(tmp_path, caplog):
    store = _Store([_entry("a", "/a"), _entry("b", "/b")])
    real = store.refresh

    def refresh(name):
        if name == "a":
            time.sleep(0.5)
        real(name)

    store.refresh = refresh
    with caplog.at_level(logging.WARNING):
        _run(_settings(tmp_path), store, _TIMEOUT_REFRESH=0.05)
    # The thread of "a" still finishes later; the phase moved on without it.
    assert store.refreshed[0] == "b"
    assert any("Marketplace refresh for 'a' failed" in r.getMessage() for r in caplog.records)


def test_a_failing_refresh_is_logged_with_the_marketplace_name(tmp_path, caplog):
    store = _Store([_entry("a", "/a")], refresh_error=RuntimeError("boom"))
    with caplog.at_level(logging.WARNING):
        _run(_settings(tmp_path), store)
    assert [r.getMessage() for r in caplog.records] == ["Marketplace refresh for 'a' failed: boom"]


def test_a_catalog_just_inside_the_window_is_kept_and_one_just_outside_is_refreshed(tmp_path):
    from datetime import UTC, datetime, timedelta

    def stamp(seconds):
        return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()

    cached = MarketplaceCatalog(name="x", plugins=[])
    store = _Store(
        [
            _entry("fresh", "/f", last_fetched=stamp(6 * 3600 - 120), cached=cached),
            _entry("stale", "/s", last_fetched=stamp(6 * 3600 + 120), cached=cached),
            _entry("never", "/n"),
        ]
    )
    _run(_settings(tmp_path), store)
    assert store.refreshed == ["stale", "never"]


def test_the_store_is_built_afresh_for_each_pass_from_the_current_settings(tmp_path):
    current = {"s": _settings(tmp_path / "one")}
    dirs: list[Path] = []

    def store_for(d):
        dirs.append(d)
        return _Store()

    phase = MarketplaceWarmupPhase(settings_ref=lambda: current["s"], store_for=store_for)
    asyncio.run(phase._refresh_all())
    current["s"] = _settings(tmp_path / "two")
    asyncio.run(phase._refresh_all())
    assert dirs == [tmp_path / "one", tmp_path / "two"]


async def test_blocking_store_calls_do_not_block_the_loop(tmp_path):
    store = _Store([_entry("a", "/a")])
    real = store.refresh
    ticks = 0

    def refresh(name):
        time.sleep(0.2)
        real(name)

    store.refresh = refresh

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    t = asyncio.create_task(ticker())
    phase = MarketplaceWarmupPhase(
        settings_ref=lambda: _settings(tmp_path), store_for=lambda _d: store
    )
    await phase._refresh_all()
    t.cancel()
    assert store.refreshed == ["a"]
    assert ticks >= 5
