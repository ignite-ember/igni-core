"""What ``activity.json`` says about igni, and how it keeps saying it.

``docs/CLOUD.md`` §9. The node agent reads the file to decide whether
the VM may sleep and how much is going on in it, so the numbers in it
are behaviour: how many runs are working, whether a busy lease still
holds and when it expires, when igni is to be woken, and what the VM's
credential is. The file is written whole, and written again whenever
something in it moved — or every :attr:`ActivityFile.HEARTBEAT_SECONDS`
while igni is not suspendable, so the guest agent does not call it
stale.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from igni_cloud.guest import ActivityFile
from igni_cloud.guest.activity_file import GuestActivity

from igni_core.backend.activity import Activity, ActivityState, SessionActivity
from igni_core.core.activity import ActivityRegistry, BusyKind

NINE = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)


class SometimesBroken:
    """An ``app.activity`` that cannot be read after a while."""

    def __init__(self) -> None:
        self.broken = False

    def __call__(self) -> Activity:
        if self.broken:
            raise RuntimeError("the backend is gone")
        return Activity.of([])


def test_a_running_session_is_one_run_working(tmp_path: Path):
    running = Activity.of([SessionActivity(session_id="s1", state=ActivityState.RUNNING)])
    file = ActivityFile(tmp_path / "activity.json", activity=lambda: running, clients=lambda: 0)
    assert file.refresh()
    written = json.loads(file.path.read_text())
    assert written["running"] == 1, "a session with a run working is one run working"
    assert written["state"] == "running" and written["suspendable"] is False


def test_a_session_waiting_on_a_person_is_not_running(tmp_path: Path):
    waiting = Activity.of(
        [SessionActivity(session_id="s1", state=ActivityState.WAITING, waiting_approvals=1)]
    )
    file = ActivityFile(tmp_path / "activity.json", activity=lambda: waiting, clients=lambda: 0)
    assert file.refresh()
    written = json.loads(file.path.read_text())
    assert written["running"] == 0, "waiting on a person is not working"
    assert written["waiting_approvals"] == 1


def test_two_running_sessions_are_two_runs_working(tmp_path: Path):
    running = Activity.of(
        [
            SessionActivity(session_id="s1", state=ActivityState.RUNNING),
            SessionActivity(session_id="s2", state=ActivityState.RUNNING),
        ]
    )
    file = ActivityFile(tmp_path / "activity.json", activity=lambda: running, clients=lambda: 0)
    assert file.refresh()
    assert json.loads(file.path.read_text())["running"] == 2


def test_a_lease_that_was_not_let_go_has_no_expiry(tmp_path: Path):
    registry = ActivityRegistry(clock=lambda: NINE)
    registry.hold(BusyKind.PROCESS, "process:7", "watching")
    file = ActivityFile(
        tmp_path / "activity.json",
        activity=lambda: Activity.now([], registry),
        clients=lambda: 0,
    )
    assert file.refresh()
    [lease] = json.loads(file.path.read_text())["busy"]
    assert lease["expires_at"] is None, "not let go: nothing to wake for"
    assert lease["since"] == int(NINE.timestamp() * 1000)


def test_a_lease_that_expires_carries_when_it_expires(tmp_path: Path):
    registry = ActivityRegistry(clock=lambda: NINE)
    registry.hold(BusyKind.MONITOR, "monitor:ci", "watching", ttl=90)
    file = ActivityFile(
        tmp_path / "activity.json",
        activity=lambda: Activity.now([], registry),
        clients=lambda: 0,
    )
    assert file.refresh()
    [lease] = json.loads(file.path.read_text())["busy"]
    assert lease["expires_at"] == int((NINE + timedelta(seconds=90)).timestamp() * 1000)
    assert lease["since"] == int(NINE.timestamp() * 1000)


def test_nothing_due_means_nothing_to_wake_for(tmp_path: Path):
    file = ActivityFile(
        tmp_path / "activity.json", activity=lambda: Activity.of([]), clients=lambda: 0
    )
    assert file.refresh()
    assert json.loads(file.path.read_text())["wake_at"] is None


def test_without_a_credential_there_is_none_to_keep(tmp_path: Path):
    file = ActivityFile(
        tmp_path / "activity.json", activity=lambda: Activity.of([]), clients=lambda: 0
    )
    assert file.refresh()
    assert json.loads(file.path.read_text())["credential"] is None


def test_the_credential_state_is_in_the_file(tmp_path: Path):
    file = ActivityFile(
        tmp_path / "activity.json",
        activity=lambda: Activity.of([]),
        clients=lambda: 0,
        credential=lambda: "ready",
    )
    assert file.refresh()
    assert json.loads(file.path.read_text())["credential"] == "ready"


def test_a_forced_refresh_writes_the_file_again(tmp_path: Path):
    now = [1000.0]
    file = ActivityFile(
        tmp_path / "activity.json",
        activity=lambda: Activity.of([]),
        clients=lambda: 0,
        clock=lambda: now[0],
    )
    assert file.refresh()
    first = file.path.read_text()
    now[0] += 10
    assert not file.refresh(), "nothing changed: nothing to say"
    assert file.path.read_text() == first
    assert file.refresh(force=True), "asked to write it again"
    assert json.loads(file.path.read_text())["written_at_ms"] == int(now[0] * 1000)


def test_the_file_remembers_what_it_wrote(tmp_path: Path):
    now = [1000.0]
    file = ActivityFile(
        tmp_path / "activity.json",
        activity=lambda: Activity.of([]),
        clients=lambda: 0,
        clock=lambda: now[0],
    )
    assert file.written is None, "nothing written yet"
    assert file.refresh()
    written = file.written
    assert written is not None and written.state is ActivityState.IDLE
    assert written.written_at_ms == 1_000_000
    now[0] += 10
    assert not file.refresh()
    assert file.written is written, "the last snapshot written is kept"


def test_the_file_forgets_what_it_wrote_when_igni_stops(tmp_path: Path):
    file = ActivityFile(
        tmp_path / "activity.json", activity=lambda: Activity.of([]), clients=lambda: 0
    )
    assert file.refresh()
    assert file.written is not None
    asyncio.run(file.stop())
    assert file.written is None, "an igni that stopped keeps nothing"


def test_two_snapshots_differ_only_in_when_they_were_written():
    one = GuestActivity.of(Activity.of([]), clients=0, now=1000.0)
    two = GuestActivity.of(Activity.of([]), clients=0, now=2000.0)
    assert one.same_as(two), "the time it was written is not a change"
    assert not one.same_as(None), "nothing written yet is not the same"
    three = GuestActivity.of(Activity.of([]), clients=1, now=1000.0)
    assert not one.same_as(three), "a window came: that is a change"


def test_an_activity_that_cannot_be_read_leaves_the_file_as_it_was(tmp_path: Path):
    source = SometimesBroken()
    file = ActivityFile(tmp_path / "activity.json", activity=source, clients=lambda: 0)
    assert file.refresh()
    before = file.path.read_text()
    source.broken = True
    assert not file.refresh(), "the file keeps its last content"
    assert file.path.read_text() == before


def test_a_file_that_cannot_be_written_is_warned_about_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a file where the run directory should be")
    file = ActivityFile(
        blocker / "activity.json", activity=lambda: Activity.of([]), clients=lambda: 0
    )
    with caplog.at_level(logging.WARNING, logger="igni_cloud.guest.activity_file"):
        assert not file.refresh(), "nowhere to write it"
        assert not file.refresh()
    assert len(caplog.records) == 1, "warned once: the tmpfs is still full"
    assert file.written is None


def test_the_file_is_written_whole(tmp_path: Path):
    registry = ActivityRegistry()
    for index in range(50):
        registry.hold(BusyKind.PROCESS, f"process:{index}", "wàching ünicode ✓")
    sessions = [
        SessionActivity(session_id=f"s{index}", state=ActivityState.WAITING) for index in range(20)
    ]
    file = ActivityFile(
        tmp_path / "activity.json",
        activity=lambda: Activity.of(sessions, busy=registry.busy()),
        clients=lambda: 3,
    )
    assert file.refresh()
    written = json.loads(file.path.read_text())
    assert len(written["busy"]) == 50
    assert written["busy"][0]["reason"] == "wàching ünicode ✓"
    assert len(written["sessions"]) == 20
    assert written["sessions"][0]["session_id"] == "s0"
    assert [p.name for p in tmp_path.iterdir()] == ["activity.json"], "nothing half-written"


async def test_the_heartbeat_rewrites_the_file_by_itself(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ActivityFile, "HEARTBEAT_SECONDS", 0.05)
    running = Activity.of([SessionActivity(session_id="s1", state=ActivityState.RUNNING)])
    file = ActivityFile(tmp_path / "activity.json", activity=lambda: running, clients=lambda: 0)
    file.start()
    try:
        first = json.loads(file.path.read_text())["written_at_ms"]
        await asyncio.sleep(0.3)
        second = json.loads(file.path.read_text())["written_at_ms"]
        assert second > first, "the heartbeat rewrote it without being asked"
    finally:
        await file.stop()


async def test_an_idle_igni_is_never_woken_for_nothing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ActivityFile, "HEARTBEAT_SECONDS", 0.05)
    file = ActivityFile(
        tmp_path / "activity.json", activity=lambda: Activity.of([]), clients=lambda: 0
    )
    file.start()
    try:
        first = json.loads(file.path.read_text())["written_at_ms"]
        await asyncio.sleep(0.3)
        second = json.loads(file.path.read_text())["written_at_ms"]
        assert second == first, "suspendable: no timer runs at all"
    finally:
        await file.stop()


def test_the_heartbeat_interval_is_what_the_protocol_asks():
    assert ActivityFile.HEARTBEAT_SECONDS <= 5, "the guest agent asks for every 5 s at most"
    assert ActivityFile.HEARTBEAT_SECONDS == 4.0
