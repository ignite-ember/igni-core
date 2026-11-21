"""Distributed extensions reach an igni that is already running.

The R691 live test (``docs/design/local-cloud-switch.md`` §15.8) found
that the listing was read only at start-up and at sign-in: an extension
an admin switched on while a desktop or a cloud VM was running never
arrived until a restart. Now :class:`ExtensionDistribution` passes again
on the group pack's timer (``group_policy.poll_seconds``). A tick asks
nothing while nobody is signed in, applies nothing when the listing is
the one already applied, and otherwise installs, upgrades and removes
exactly as the start-up pass does. The account-only backend never runs
it (``test_the_account_only_backend_signs_in_and_runs_nothing``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from igni_core.extensions.distribution.sync import ExtensionDistribution
from igni_core.extensions.service_token import IgniSession
from igni_core.extensions.store import ExtensionStore
from tests.extension_distribution_helpers import (
    API_URL,
    SESSION,
    FakeIgni,
    Listed,
    Publisher,
    bundle_archive,
    manifest,
)

#: Short enough that a test sees several ticks in well under a second.
TICK = 0.01


class Lifecycle:
    """Starting and stopping, as calls; versions in ``broken`` do not start."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.broken: set[str] = set()

    async def activate(self, extension_id: str, bundle: Path) -> bool:
        self.calls.append(("activate", extension_id))
        version = json.loads((bundle / "manifest.json").read_text())["version"]
        return version not in self.broken

    async def deactivate(self, extension_id: str) -> None:
        self.calls.append(("deactivate", extension_id))


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.igni = FakeIgni()
        self.publisher = Publisher("RSA")
        self.store = ExtensionStore(tmp_path / "store")
        self.lifecycle = Lifecycle()
        self.signed_in = True
        self.distribution = ExtensionDistribution(
            self.store,
            api_url=lambda: API_URL,
            session=self.session,
            lifecycle=self.lifecycle,
            transport=self.igni.transport(),
        )

    def session(self) -> IgniSession | None:
        return IgniSession(access_token=SESSION) if self.signed_in else None

    def publish(self, extension_id: str = "acme.notes", version: str = "1.0.0") -> Listed:
        raw = manifest(extension_id, version)
        entry = Listed(
            bundle_archive(raw, {"wheels/placeholder.txt": b"x"}),
            self.publisher,
            extension_id=extension_id,
            version=version,
        )
        self.igni.publish(entry)
        return entry

    def listings(self) -> int:
        return sum(1 for r in self.igni.requests if r.url.path == "/v1/extensions")

    def installed(self, extension_id: str) -> str | None:
        record = self.store.provenance.of(extension_id)
        return record.version if record is not None else None

    async def until(self, predicate: Any, timeout: float = 10) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("condition never held")
            await asyncio.sleep(TICK / 2)

    async def ticks(self, n: int) -> None:
        """At least ``n`` more ticks went by, each asking for the listing."""
        start = self.listings()
        await self.until(lambda: self.listings() >= start + n)
        # And the last one finished applying what it read.
        async with self.distribution._lock:
            pass


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    world = World(tmp_path)
    yield world
    await world.distribution.stop()


# ── A tick ─────────────────────────────────────────────────────────


async def test_each_interval_asks_for_the_listing_again(world: World):
    world.distribution.start_polling(TICK)
    assert world.distribution.polling

    await world.ticks(3)

    assert world.listings() >= 3


async def test_the_timer_waits_an_interval_before_its_first_pass(world: World):
    """Start-up already passed once; the timer does not pass again at once."""
    world.distribution.start_polling(3600)
    await asyncio.sleep(TICK * 5)
    assert world.listings() == 0


async def test_an_extension_switched_on_while_running_is_installed_by_a_tick(world: World):
    await world.distribution.sync()
    assert world.installed("acme.notes") is None

    world.distribution.start_polling(TICK)
    world.publish()
    await world.until(lambda: world.installed("acme.notes") == "1.0.0")

    assert (world.store.path_for("acme.notes") / "manifest.json").is_file()
    assert ("activate", "acme.notes") in world.lifecycle.calls


async def test_a_new_version_published_while_running_replaces_the_old(world: World):
    world.publish()
    await world.distribution.sync()

    world.distribution.start_polling(TICK)
    world.publish(version="1.1.0")
    await world.until(lambda: world.installed("acme.notes") == "1.1.0")

    assert world.store.read(world.store.path_for("acme.notes")).version == "1.1.0"


async def test_an_extension_switched_off_while_running_is_removed_by_a_tick(world: World):
    world.publish()
    world.publish("acme.review")
    await world.distribution.sync()
    world.lifecycle.calls.clear()

    world.distribution.start_polling(TICK)
    world.igni.withdraw("acme.notes")
    await world.until(lambda: world.installed("acme.notes") is None)

    assert not world.store.path_for("acme.notes").exists()
    assert world.installed("acme.review") == "1.0.0", "what is still listed stays"
    assert world.lifecycle.calls == [("deactivate", "acme.notes")]


# ── Unchanged ──────────────────────────────────────────────────────


async def test_an_unchanged_listing_costs_the_listing_and_nothing_else(world: World, monkeypatch):
    world.publish()
    await world.distribution.sync()
    world.lifecycle.calls.clear()
    touched: list[str] = []
    monkeypatch.setattr(world.store, "prune_assets", lambda: touched.append("prune"))
    monkeypatch.setattr(
        world.store.listing, "record_group", lambda *a, **k: touched.append("group")
    )

    world.distribution.start_polling(TICK)
    await world.ticks(3)

    assert len(world.igni.downloads()) == 1, "downloaded once, at the first pass"
    assert world.lifecycle.calls == []
    assert touched == [], "nothing on disk is touched for an unchanged listing"


async def test_an_unchanged_listing_is_reported_as_such(world: World):
    world.publish()
    await world.distribution.sync()

    report = await world.distribution.sync(unless_unchanged=True)

    assert report.skipped == "the listing has not changed"
    assert report.installed == report.removed == report.unchanged == []


async def test_start_up_and_sign_in_apply_even_an_unchanged_listing(world: World):
    """Only the timer's pass stops at the listing: a sign-in is a new
    person's listing, and a start-up has nothing applied yet."""
    world.publish()
    await world.distribution.sync()

    report = await world.distribution.sync()

    assert report.skipped is None
    assert report.unchanged == ["acme.notes"]


async def test_a_pass_where_something_failed_is_tried_again_at_the_next_tick(world: World):
    """A download that failed is not "applied": the next tick tries again,
    though the listing did not change."""
    world.publish()
    await world.distribution.sync()
    world.lifecycle.broken.add("1.1.0")
    world.publish(version="1.1.0")
    first = await world.distribution.sync()
    assert "acme.notes" in first.failed

    world.lifecycle.broken.clear()
    world.distribution.start_polling(TICK)
    await world.until(lambda: world.installed("acme.notes") == "1.1.0")


async def test_a_pass_that_raises_half_way_is_not_taken_as_applied(world: World, monkeypatch):
    world.publish()
    await world.distribution.sync()

    def broken() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(world.store, "prune_assets", broken)
    with pytest.raises(OSError):
        await world.distribution.sync()
    monkeypatch.undo()

    report = await world.distribution.sync(unless_unchanged=True)
    assert report.skipped is None
    assert report.unchanged == ["acme.notes"]


# ── Nobody signed in ───────────────────────────────────────────────


async def test_a_tick_asks_nothing_while_nobody_is_signed_in(world: World):
    world.publish()
    world.signed_in = False
    world.distribution.start_polling(TICK)

    await asyncio.sleep(TICK * 10)

    assert world.igni.requests == []
    assert world.installed("acme.notes") is None


async def test_the_timer_picks_up_once_somebody_signs_in(world: World):
    world.publish()
    world.signed_in = False
    world.distribution.start_polling(TICK)
    await asyncio.sleep(TICK * 5)

    world.signed_in = True
    await world.until(lambda: world.installed("acme.notes") == "1.0.0")


async def test_a_tick_that_skips_says_so_only_at_debug(world: World, caplog):
    world.signed_in = False
    with caplog.at_level("DEBUG", logger="igni_core.extensions.distribution.sync"):
        await world.distribution._sync_logged(unless_unchanged=True)
        await world.distribution._sync_logged()

    levels = [r.levelname for r in caplog.records if "skipped" in r.getMessage()]
    assert levels == ["DEBUG", "INFO"]


# ── The timer itself ───────────────────────────────────────────────


async def test_zero_turns_the_timer_off(world: World):
    world.distribution.start_polling(0)
    assert not world.distribution.polling


def test_without_a_running_loop_there_is_no_timer(tmp_path: Path):
    world = World(tmp_path)
    world.distribution.start_polling(TICK)
    assert not world.distribution.polling


async def test_a_second_start_keeps_the_one_timer(world: World):
    world.distribution.start_polling(TICK)
    first = world.distribution._poll_task
    world.distribution.start_polling(TICK)
    assert world.distribution._poll_task is first


async def test_a_failing_pass_does_not_stop_the_timer(world: World, monkeypatch):
    calls: list[int] = []

    async def broken(*, unless_unchanged: bool = False) -> None:
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(world.distribution, "_sync_logged", broken)
    world.distribution.start_polling(TICK)

    await world.until(lambda: len(calls) >= 3)


async def test_a_listing_igni_refuses_keeps_the_timer_and_what_is_installed(world: World):
    world.publish()
    await world.distribution.sync()
    world.igni.listing_status = 503

    world.distribution.start_polling(TICK)
    await world.ticks(2)

    assert world.installed("acme.notes") == "1.0.0"
    assert world.distribution.polling


async def test_stop_stops_the_timer(world: World):
    world.distribution.start_polling(TICK)
    task = world.distribution._poll_task
    assert task is not None

    await world.distribution.stop()

    assert task.cancelled()
    assert not world.distribution.polling
    seen = world.listings()
    await asyncio.sleep(TICK * 5)
    assert world.listings() == seen
