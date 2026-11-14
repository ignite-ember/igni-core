"""An extension's busy lease: its floor, its default, its renewal after it lapsed, its races.

``self.busy`` keeps igni awake for as long as an extension says it is
working, and never longer than the cap without being asked again.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from igni_core.core.activity import ActivityRegistry, BusyKind
from igni_core.extensions.busy import ExtensionBusy


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _lifetime(lease) -> float:
    assert lease.expires_at is not None
    return (lease.expires_at - lease.since).total_seconds()


@pytest.mark.parametrize("asked", [0, -5, 0.2, float("nan")])
def test_a_ttl_below_a_second_is_a_second(asked):
    lease = ExtensionBusy(ActivityRegistry()).busy("acme.indexer", "indexing", asked)
    assert _lifetime(lease) == 1.0


def test_no_ttl_means_the_longest_lease():
    lease = ExtensionBusy(ActivityRegistry()).busy("acme.indexer", "indexing")
    assert _lifetime(lease) == ExtensionBusy.MAX_TTL_SECONDS == 600.0


def test_a_ttl_in_between_is_kept_as_asked():
    lease = ExtensionBusy(ActivityRegistry()).busy("acme.indexer", "indexing", 42)
    assert _lifetime(lease) == 42.0


def test_done_lets_go_of_one_reason_only_and_is_silent_when_there_is_none():
    registry = ActivityRegistry()
    busy = ExtensionBusy(registry)
    busy.busy("acme.indexer", "indexing")
    busy.busy("acme.indexer", "syncing")
    busy.done("acme.indexer", "never-taken")
    busy.done("acme.unknown", "indexing")
    assert sorted(b.reason for b in registry.busy()) == ["indexing", "syncing"]
    busy.done("acme.indexer", "indexing")
    assert [b.reason for b in registry.busy()] == ["syncing"]
    busy.done("acme.indexer", "indexing")  # twice is as silent
    assert [b.reason for b in registry.busy()] == ["syncing"]


def test_the_same_reason_from_two_extensions_is_two_leases():
    registry = ActivityRegistry()
    busy = ExtensionBusy(registry)
    first = busy.busy("acme.one", "indexing")
    second = busy.busy("acme.two", "indexing")
    assert first.id != second.id
    busy.done("acme.one", "indexing")
    assert [b.holder for b in registry.busy()] == ["acme.two"]


def test_stopping_an_extension_that_held_nothing_changes_nothing():
    registry = ActivityRegistry()
    busy = ExtensionBusy(registry)
    busy.busy("acme.other", "indexing")
    busy.release_all("acme.unknown")
    assert [b.holder for b in registry.busy()] == ["acme.other"]


def test_asking_again_after_the_lease_lapsed_takes_a_new_one():
    clock = Clock()
    registry = ActivityRegistry(clock=clock)
    busy = ExtensionBusy(registry)
    first = busy.busy("acme.indexer", "indexing", 10)
    clock.now += timedelta(seconds=11)
    assert registry.busy() == []
    again = busy.busy("acme.indexer", "indexing", 10)
    assert again.id != first.id
    assert [b.id for b in registry.busy()] == [again.id]


def test_asking_again_after_the_lease_was_released_elsewhere_takes_a_new_one():
    registry = ActivityRegistry()
    busy = ExtensionBusy(registry)
    first = busy.busy("acme.indexer", "indexing")
    registry.release(first.id)
    again = busy.busy("acme.indexer", "indexing")
    assert again.id != first.id
    assert [b.id for b in registry.busy()] == [again.id]


def test_two_callers_for_one_reason_leave_one_lease():
    class Slow(ActivityRegistry):
        def hold(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            time.sleep(0.05)
            return super().hold(*args, **kwargs)

    registry = Slow()
    busy = ExtensionBusy(registry)
    threads = [
        threading.Thread(target=busy.busy, args=("acme.indexer", "indexing")) for _ in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(registry.busy()) == 1
    busy.done("acme.indexer", "indexing")
    assert registry.busy() == [], "nothing orphaned that done() cannot reach"


def test_without_a_registry_the_shared_one_is_used():
    ActivityRegistry.reset_shared()
    try:
        busy = ExtensionBusy()
        assert busy.registry is ActivityRegistry.shared()
        busy.busy("acme.indexer", "indexing")
        [held] = ActivityRegistry.shared().busy()
        assert (held.kind, held.holder) == (BusyKind.EXTENSION, "acme.indexer")
    finally:
        ActivityRegistry.reset_shared()
