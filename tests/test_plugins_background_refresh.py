"""Tests for the background marketplace refresh.

Verifies that :meth:`MarketplaceWarmupPhase.start_background` (what
:meth:`Session.start_marketplace_refresh_background` runs):

  - Schedules an asyncio task on the running loop.
  - Calls ``MarketplaceRegistryStore.refresh`` for each registered marketplace.
  - Continues past per-marketplace failures (one bad URL doesn't
    abort the others — failures are log-and-swallow per the design).
  - Is a no-op when there's no running event loop (so calling it
    from a sync context doesn't raise).
  - Is a no-op when no marketplaces are registered.

The phase is handed settings carrying just what the refresh reads
(``settings.storage.data_dir``) and a fake registry store, so no
network is touched.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from igni_core.core.plugins.marketplace_store import MarketplaceRegistryStore
from igni_core.core.plugins.models import MarketplaceCatalog, MarketplaceEntry
from igni_core.core.session.startup.marketplace import MarketplaceWarmupPhase

# Async tests pick up the asyncio mark via pytest-asyncio's auto mode.
# Module-level mark would also (incorrectly) tag
# test_refresh_without_running_loop_is_noop, which is intentionally sync.


# ── Test harness ────────────────────────────────────────────────────


def _store(registry: MagicMock | None = None) -> MagicMock:
    """A registry store whose ``load`` returns *registry*."""
    store = MagicMock(spec=MarketplaceRegistryStore)
    store.load.return_value = registry if registry is not None else MagicMock(marketplaces=[])
    return store


def _warmup(tmp_path: Path, store: MagicMock | None = None) -> MarketplaceWarmupPhase:
    """The marketplace warmup phase a session builds, over settings that
    carry just what the refresh reads (``storage.data_dir``) and a
    store factory handing back *store*."""
    settings = MagicMock()
    settings.storage.data_dir = str(tmp_path / "ember")
    store = store if store is not None else _store()
    return MarketplaceWarmupPhase(settings_ref=lambda: settings, store_for=lambda _d: store)


async def _drain() -> None:
    """Wait for every task the phase spawned on this loop."""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# ── Behavior ───────────────────────────────────────────────────────


async def test_refresh_calls_refresh_marketplace_for_each_entry(
    tmp_path: Path,
) -> None:
    """The background task iterates every registered marketplace and
    invokes ``store.refresh(name)`` exactly once
    each. Without this loop the in-session catalog would never
    update past install time."""
    fake_registry = MagicMock()
    e1 = MagicMock()
    e1.fetched_within.return_value = False  # stale: refreshed
    e1.name = "m1"
    e2 = MagicMock()
    e2.fetched_within.return_value = False  # stale: refreshed
    e2.name = "m2"
    fake_registry.marketplaces = [e1, e2]

    store = _store(fake_registry)
    _warmup(tmp_path, store).start_background()
    await _drain()

    assert store.refresh.call_count == 2
    called_names = {c.args[0] for c in store.refresh.call_args_list}
    assert called_names == {"m1", "m2"}


async def test_refresh_continues_past_failures(tmp_path: Path) -> None:
    """A failing marketplace (raise from ``store.refresh``)
    is logged and swallowed — the next marketplace still gets
    refreshed. Without this, one bad URL in the registry would
    silently break refresh for everything after it."""
    fake_registry = MagicMock()
    good = MagicMock()
    good.fetched_within.return_value = False  # stale: refreshed
    good.name = "good"
    bad = MagicMock()
    bad.fetched_within.return_value = False  # stale: refreshed
    bad.name = "bad"
    fake_registry.marketplaces = [bad, good]

    def _side_effect(name):
        if name == "bad":
            raise RuntimeError("simulated network failure")
        return MagicMock()

    store = _store(fake_registry)
    store.refresh.side_effect = _side_effect
    _warmup(tmp_path, store).start_background()
    await _drain()

    # Both were attempted even though the first raised.
    assert store.refresh.call_count == 2
    names_called = [c.args[0] for c in store.refresh.call_args_list]
    assert "bad" in names_called and "good" in names_called


async def test_refresh_no_marketplaces_is_noop(tmp_path: Path) -> None:
    """Empty registry → the inner loop runs zero times — but the
    task is still scheduled and completes cleanly. Important so a
    fresh install (no marketplaces yet) doesn't crash on session
    start."""
    fake_registry = MagicMock()
    fake_registry.marketplaces = []

    store = _store(fake_registry)
    _warmup(tmp_path, store).start_background()
    await _drain()

    store.refresh.assert_not_called()


def test_refresh_without_running_loop_is_noop(tmp_path: Path) -> None:
    """Calling ``start_marketplace_refresh_background`` outside an
    event loop must NOT raise — it bails silently. Mirrors the
    behavior of ``start_codeindex_background``: callers can invoke
    these eagerly from sync contexts (e.g. test harnesses) without
    needing a loop. (This test runs sync so the function sees no
    running loop.)"""
    warmup = _warmup(tmp_path)
    # No exception means pass — the early-return on RuntimeError fires.
    warmup.start_background()


async def test_refresh_passes_data_dir_through(tmp_path: Path) -> None:
    """The store the phase works on is built over
    ``settings.storage.data_dir`` — if it weren't, the refresh would
    silently read from ``~/.igni`` instead of whatever the session's
    configured data dir is (breaking custom XDG-style layouts and
    tests). Both the auto-register step and the refresh step go
    through that one store."""
    fake_registry = MagicMock()
    e = MagicMock()
    e.fetched_within.return_value = False  # stale: refreshed
    e.name = "m1"
    fake_registry.marketplaces = [e]
    store = _store(fake_registry)
    built_for: list[Path] = []

    def _store_for(data_dir: Path) -> MagicMock:
        built_for.append(data_dir)
        return store

    settings = MagicMock()
    settings.storage.data_dir = str(tmp_path / "ember")
    MarketplaceWarmupPhase(settings_ref=lambda: settings, store_for=_store_for).start_background()
    await _drain()

    assert built_for == [tmp_path / "ember"]
    assert store.load.call_count >= 1
    store.refresh.assert_called_once_with("m1")


async def test_a_catalog_fetched_recently_is_not_fetched_again(tmp_path: Path) -> None:
    """A refresh clones the marketplace's repository: once per
    ``REFRESH_EVERY_SECONDS`` is enough, not once per session start."""
    fresh, stale = MagicMock(), MagicMock()
    fresh.name, stale.name = "fresh", "stale"
    fresh.fetched_within.return_value = True
    stale.fetched_within.return_value = False
    registry = MagicMock()
    registry.marketplaces = [fresh, stale]
    store = _store(registry)
    store.refresh.return_value = None
    phase = _warmup(tmp_path, store)
    await phase._refresh_registered(registry, store)
    assert [c.args[0] for c in store.refresh.call_args_list] == ["stale"]
    fresh.fetched_within.assert_called_once_with(MarketplaceWarmupPhase.REFRESH_EVERY_SECONDS)


def test_an_entry_knows_how_long_ago_it_was_fetched() -> None:
    entry = MarketplaceEntry(name="m", url="u")
    assert not entry.fetched_within(3600), "never fetched"
    entry.mark_fetched(MarketplaceCatalog(name="m"))
    assert entry.fetched_within(3600)
    later = datetime.now(UTC) + timedelta(hours=2)
    assert not entry.fetched_within(3600, now=later)
