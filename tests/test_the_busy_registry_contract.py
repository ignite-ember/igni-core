"""The activity registry's own contract (``core/activity/registry.py``).

Ordering, wakes, timers, watchers, renewal and the wire-visible kinds.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta

import pytest

from igni_core.core.activity import ActivityRegistry, BusyKind
from igni_core.core.activity.registry import BusyLease


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def test_the_busy_kinds_are_the_ten_stable_strings():
    assert {kind.name: kind.value for kind in BusyKind} == {
        "POST_RUN": "post_run",
        "MODEL_CALL": "model_call",
        "HTTP_CALL": "http_call",
        "MCP_CALL": "mcp_call",
        "EXTENSION_CALL": "extension_call",
        "SCHEDULED_TASK": "scheduled_task",
        "PROCESS": "process",
        "MONITOR": "monitor",
        "EXTENSION": "extension",
        "BACKGROUND": "background",
    }


def test_leases_get_unique_ids_and_come_back_oldest_first():
    clock = Clock()
    registry = ActivityRegistry(clock=clock)
    first = registry.hold(BusyKind.MODEL_CALL, "a", "one")
    clock.now += timedelta(seconds=5)
    second = registry.hold(BusyKind.HTTP_CALL, "b", "two")
    clock.now -= timedelta(seconds=10)
    third = registry.hold(BusyKind.PROCESS, "c")
    assert len({first.id, second.id, third.id}) == 3
    assert first.id.startswith("busy-")
    assert [lease.holder for lease in registry.busy()] == ["c", "a", "b"]
    held = registry.lease(second.id)
    assert held is not None and (held.kind, held.reason) == (BusyKind.HTTP_CALL, "two")
    assert held.expires_at is None and held.since == datetime(2026, 10, 26, 12, 0, 5, tzinfo=UTC)


def test_lease_looks_up_a_held_lease_and_none_for_a_gone_one():
    registry = ActivityRegistry()
    lease = registry.hold(BusyKind.MONITOR, "m")
    assert registry.lease(lease.id) is not None
    lease.release()
    assert registry.lease(lease.id) is None
    assert registry.lease("busy-999") is None


def test_ids_stay_unique_when_threads_hold_at_once():
    registry = ActivityRegistry()
    ids: list[str] = []

    def take() -> None:
        for _ in range(100):
            ids.append(registry.hold(BusyKind.BACKGROUND, "t").id)

    threads = [threading.Thread(target=take) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(ids) == len(set(ids)) == 800
    assert len(registry.busy()) == 800


def test_a_lease_expires_exactly_at_its_expiry_and_zero_ttl_is_already_over():
    clock = Clock()
    registry = ActivityRegistry(clock=clock)
    registry.hold(BusyKind.EXTENSION, "x", ttl=10)
    clock.now += timedelta(seconds=9, microseconds=999999)
    assert registry.is_busy()
    clock.now += timedelta(microseconds=1)
    assert not registry.is_busy(), "expires_at == now counts as expired"
    registry.hold(BusyKind.EXTENSION, "zero", ttl=0)
    assert registry.busy() == []


def test_a_lease_without_a_ttl_never_expires():
    clock = Clock()
    registry = ActivityRegistry(clock=clock)
    registry.hold(BusyKind.PROCESS, "p")
    clock.now += timedelta(days=3650)
    assert registry.is_busy()


def test_busylease_expired_is_true_only_with_an_expiry_at_or_before_now():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    base = {"id": "busy-1", "kind": BusyKind.PROCESS, "holder": "h", "since": now}
    assert not BusyLease(**base).expired(now)
    assert not BusyLease(**base, expires_at=now + timedelta(seconds=1)).expired(now)
    assert BusyLease(**base, expires_at=now).expired(now)
    assert BusyLease(**base, expires_at=now - timedelta(seconds=1)).expired(now)


def test_dropping_an_expired_lease_is_logged_with_its_id(caplog: pytest.LogCaptureFixture):
    clock = Clock()
    registry = ActivityRegistry(clock=clock)
    lease = registry.hold(BusyKind.EXTENSION, "x", ttl=1)
    clock.now += timedelta(seconds=2)
    with caplog.at_level(logging.INFO, logger="igni_core.core.activity.registry"):
        assert registry.busy() == []
    assert any(lease.id in record.getMessage() for record in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="igni_core.core.activity.registry"):
        registry.busy()
    assert caplog.records == [], "logged once, when dropped"


def test_renew_returns_the_updated_lease_and_none_for_one_that_is_gone():
    clock = Clock()
    registry = ActivityRegistry(clock=clock)
    lease = registry.hold(BusyKind.EXTENSION, "x", ttl=10)
    renewed = lease.renew(60)
    assert renewed is not None
    assert renewed.expires_at == clock.now + timedelta(seconds=60)
    assert renewed.id == lease.id and renewed.holder == "x"
    cleared = lease.renew(None)
    assert cleared is not None and cleared.expires_at is None
    clock.now += timedelta(days=1)
    assert registry.is_busy(), "a cleared expiry never lapses"
    lease.release()
    assert lease.renew(5) is None
    assert registry.busy() == []


def test_renewing_an_expired_lease_that_was_not_yet_dropped_revives_it():
    clock = Clock()
    registry = ActivityRegistry(clock=clock)
    lease = registry.hold(BusyKind.EXTENSION, "x", ttl=1)
    clock.now += timedelta(seconds=5)
    assert lease.renew(30) is not None
    assert [held.holder for held in registry.busy()] == ["x"]


def test_renewing_a_lease_that_expired_and_was_dropped_returns_none():
    clock = Clock()
    registry = ActivityRegistry(clock=clock)
    lease = registry.hold(BusyKind.EXTENSION, "x", ttl=1)
    clock.now += timedelta(seconds=5)
    registry.busy()
    heard: list[int] = []
    registry.watch(lambda: heard.append(1))
    assert lease.renew(30) is None
    assert heard == [] and registry.busy() == []


def test_releasing_twice_or_an_unknown_id_is_harmless_and_silent():
    registry = ActivityRegistry()
    lease = registry.hold(BusyKind.PROCESS, "p")
    heard: list[int] = []
    registry.watch(lambda: heard.append(1))
    lease.release()
    lease.release()
    registry.release("busy-404")
    assert heard == [1]


def test_a_lease_lets_go_on_exit_even_by_exception_sync():
    registry = ActivityRegistry()
    with pytest.raises(ValueError), registry.hold(BusyKind.PROCESS, "p"):
        assert registry.is_busy()
        raise ValueError
    assert registry.busy() == []


async def test_a_lease_lets_go_on_exit_even_by_exception_async():
    registry = ActivityRegistry()
    with pytest.raises(ValueError):
        async with registry.hold(BusyKind.PROCESS, "p") as lease:
            assert registry.lease(lease.id) is not None
            raise ValueError
    assert registry.busy() == []


def test_wakes_are_listed_by_time_and_the_earliest_is_wake_at():
    registry = ActivityRegistry()
    assert registry.wake_at() is None and registry.wakes() == []
    late = datetime(2026, 12, 1, tzinfo=UTC)
    early = datetime(2026, 11, 1, tzinfo=UTC)
    registry.wake("a-late", late, "later")
    registry.wake("z-early", early, "sooner")
    assert [(w.holder, w.at, w.reason) for w in registry.wakes()] == [
        ("z-early", early, "sooner"),
        ("a-late", late, "later"),
    ]
    assert registry.wake_at() == early


def test_a_holders_new_wake_replaces_its_old_one():
    registry = ActivityRegistry()
    registry.wake("sched", datetime(2026, 11, 1, tzinfo=UTC))
    later = datetime(2026, 11, 9, tzinfo=UTC)
    registry.wake("sched", later, "moved")
    [only] = registry.wakes()
    assert (only.at, only.reason) == (later, "moved")


def test_a_wake_is_withdrawn_with_none_and_watchers_hear_only_real_changes():
    registry = ActivityRegistry()
    heard: list[int] = []
    registry.watch(lambda: heard.append(1))
    at = datetime(2026, 11, 1, tzinfo=UTC)
    registry.wake("a", at, "why")
    registry.wake("a", at, "why")
    assert len(heard) == 1, "identical wake: nothing changed"
    registry.wake("a", at, "other reason")
    registry.wake("a", at + timedelta(seconds=1), "other reason")
    assert len(heard) == 3
    registry.wake("a", None)
    assert len(heard) == 4 and registry.wake_at() is None
    registry.wake("a", None)
    registry.wake("never-known", None)
    assert len(heard) == 4


def test_rearm_calls_every_timer_and_a_failing_one_does_not_stop_the_others(
    caplog: pytest.LogCaptureFixture,
):
    registry = ActivityRegistry()
    calls: list[str] = []

    def bad() -> None:
        calls.append("bad")
        raise RuntimeError("timer broke")

    registry.on_rearm(lambda: calls.append("first"))
    registry.on_rearm(bad)
    registry.on_rearm(lambda: calls.append("last"))
    with caplog.at_level(logging.ERROR, logger="igni_core.core.activity.registry"):
        registry.rearm()
    assert calls == ["first", "bad", "last"]
    assert any("re-arming a timer failed" in r.getMessage() for r in caplog.records)


def test_off_rearm_removes_a_timer_and_ignores_an_unknown_one():
    registry = ActivityRegistry()
    calls: list[str] = []

    def timer() -> None:
        calls.append("t")

    registry.on_rearm(timer)
    registry.off_rearm(lambda: None)
    registry.rearm()
    registry.off_rearm(timer)
    registry.rearm()
    assert calls == ["t"]


def test_a_failing_watcher_is_logged_and_does_not_stop_others_or_the_caller(
    caplog: pytest.LogCaptureFixture,
):
    registry = ActivityRegistry()
    heard: list[str] = []

    def bad() -> None:
        raise RuntimeError("watcher broke")

    registry.watch(bad)
    registry.watch(lambda: heard.append("ok"))
    with caplog.at_level(logging.ERROR, logger="igni_core.core.activity.registry"):
        lease = registry.hold(BusyKind.PROCESS, "p")
    assert heard == ["ok"] and registry.lease(lease.id) is not None
    assert any("an activity watcher failed" in r.getMessage() for r in caplog.records)


def test_unwatch_stops_calls_and_ignores_an_unknown_watcher():
    registry = ActivityRegistry()
    heard: list[int] = []

    def watcher() -> None:
        heard.append(1)

    registry.watch(watcher)
    registry.hold(BusyKind.PROCESS, "p")
    registry.unwatch(lambda: None)
    registry.unwatch(watcher)
    registry.hold(BusyKind.PROCESS, "q")
    assert heard == [1]


def test_a_watcher_may_ask_the_registry_while_it_is_told():
    registry = ActivityRegistry()
    seen: list[int] = []
    registry.watch(lambda: seen.append(len(registry.busy())))
    lease = registry.hold(BusyKind.PROCESS, "p")
    lease.renew(5)
    lease.release()
    assert seen == [1, 1, 0]


def test_the_shared_registry_is_one_until_reset():
    ActivityRegistry.reset_shared()
    first = ActivityRegistry.shared()
    assert ActivityRegistry.shared() is first
    ActivityRegistry.reset_shared()
    assert ActivityRegistry.shared() is not first


def test_the_default_clock_is_timezone_aware_utc():
    registry = ActivityRegistry()
    lease = registry.hold(BusyKind.PROCESS, "p", ttl=5)
    held = registry.lease(lease.id)
    assert held is not None and held.expires_at is not None
    assert held.since.utcoffset() == timedelta(0)
    assert held.expires_at - held.since == timedelta(seconds=5)
    assert abs(datetime.now(UTC) - held.since) < timedelta(seconds=5)
