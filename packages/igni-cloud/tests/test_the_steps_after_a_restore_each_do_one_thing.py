"""``igni_cloud/guest/restore_steps.py``: what a serving igni does after a restore."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from igni_cloud.guest.activity_file import ActivityFile
from igni_cloud.guest.entropy import ProcessEntropy
from igni_cloud.guest.files import AtomicFile
from igni_cloud.guest.resume import Restored, RestoreWatch
from igni_cloud.guest.restore_steps import RestoreSteps
from igni_cloud.logs import StdoutLog

from igni_core.backend.activity import Activity
from igni_core.core.activity import ActivityRegistry
from igni_core.core.db.engine import engines


class Notifications:
    def __init__(self, ran: list[str]) -> None:
        self._ran = ran

    async def resumed(self, restored: Restored, /) -> None:
        self._ran.append(f"notifications:{restored.generation}")


class Rig:
    """The real steps on a real watch; the world they touch is recorded."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.ran: list[str] = []
        self.restored_file = tmp_path / "restored"
        self.activity_path = tmp_path / "activity.json"
        self.logs = StdoutLog()
        self.registry = ActivityRegistry()
        self.registry.on_rearm(lambda: self.ran.append("rearm"))
        self.entropy = ProcessEntropy()
        self.activity = ActivityFile(
            self.activity_path, activity=lambda: Activity.of([]), clients=lambda: 0
        )
        self.watch = RestoreWatch(self.restored_file, watch=False)
        monkeypatch.setattr(RestoreWatch, "AFTER_ANSWER_SECONDS", 0.01)
        monkeypatch.setattr(RestoreWatch, "LATER_SECONDS", 0.5)

        async def reset_pools() -> None:
            self.ran.append("pools")

        monkeypatch.setattr(engines, "reset_pools", reset_pools)
        monkeypatch.setattr(self.logs, "hold", lambda: self.ran.append("hold"))
        monkeypatch.setattr(self.logs, "release", lambda: self.ran.append("release"))

        async def after_restore(_restored: Any) -> None:
            self.ran.append("entropy")

        monkeypatch.setattr(self.entropy, "after_restore", after_restore)
        self.steps = RestoreSteps(logs=self.logs, activities=self.registry, entropy=self.entropy)

    def restore(self, generation: int = 1) -> None:
        AtomicFile(self.restored_file).write(
            json.dumps({"generation": generation, "at_ms": 1, "clock_step_ms": 1.0}).encode()
        )


async def test_the_steps_run_in_their_order_with_notifications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = Rig(tmp_path, monkeypatch)
    rig.steps.register(
        rig.watch, activity=rig.activity, notifications=Notifications(rig.ran)
    )
    rig.watch.start()
    try:
        rig.restore()
        assert await asyncio.wait_for(rig.watch.check(), 2)
    finally:
        await rig.watch.stop()
    assert rig.ran == ["hold", "pools", "entropy", "release", "notifications:1", "rearm"]


async def test_without_a_notification_centre_that_step_is_left_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = Rig(tmp_path, monkeypatch)
    rig.steps.register(rig.watch, activity=rig.activity, notifications=None)
    rig.watch.start()
    try:
        rig.restore()
        assert await asyncio.wait_for(rig.watch.check(), 2)
    finally:
        await rig.watch.stop()
    assert rig.ran == ["hold", "pools", "entropy", "release", "rearm"]


async def test_the_activity_file_is_rewritten_after_a_restore_though_nothing_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = Rig(tmp_path, monkeypatch)
    assert rig.activity.refresh()
    rig.activity_path.unlink()
    assert not rig.activity.refresh(), "idle and unchanged: it would not write"
    rig.steps.register(rig.watch, activity=rig.activity, notifications=None)
    rig.watch.start()
    try:
        rig.restore()
        assert await asyncio.wait_for(rig.watch.check(), 2)
    finally:
        await rig.watch.stop()
    assert json.loads(rig.activity_path.read_text())["state"] == "idle"


async def test_the_log_writer_is_held_and_released_by_the_real_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(RestoreWatch, "AFTER_ANSWER_SECONDS", 0.01)
    monkeypatch.setattr(RestoreWatch, "LATER_SECONDS", 0.5)
    activity = ActivityFile(
        tmp_path / "activity.json", activity=lambda: Activity.of([]), clients=lambda: 0
    )
    logs = StdoutLog()
    log = logs.attach("igni_cloud.tests.restore_steps")
    steps = RestoreSteps(logs=logs, activities=ActivityRegistry(), entropy=ProcessEntropy())
    watch = RestoreWatch(tmp_path / "restored2", watch=False)
    steps.register(watch, activity=activity, notifications=None)
    monkeypatch.setattr(engines, "reset_pools", lambda: asyncio.sleep(0))
    seen_while_held: list[str] = []

    async def probe(_restored: Any) -> None:
        log.info("during")
        logs.flush()
        seen_while_held.append(capsys.readouterr().out)

    watch.on_restore(probe)
    watch.start()
    try:
        AtomicFile(tmp_path / "restored2").write(
            json.dumps({"generation": 1, "at_ms": 1, "clock_step_ms": 1.0}).encode()
        )
        assert await asyncio.wait_for(watch.check(), 2)
        logs.flush()
        written = capsys.readouterr().out
    finally:
        await watch.stop()
        log.removeHandler(logs.handler())
        logs.reset()
    assert seen_while_held == [""], "held until the first answer was through"
    assert "during" in written, "and released after"
