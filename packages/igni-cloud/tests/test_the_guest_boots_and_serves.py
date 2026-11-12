"""The guest's half of the guest contract, as the backend runs it.

``docs/CLOUD.md`` §9. ``igni_cloud.guest.contract.CloudGuest`` is the
cloud layer's backend hooks: the ready file, the late binding, the
activity file, the restore and suspend watches, and the call the
gateway makes to hand a person's session to the VM. So what a node
agent, a window or the gateway sees is behaviour: that a ``ready`` file
left by an earlier igni is gone before this one starts, that the
``vm_sign_in`` call is mounted and reaches the credential, that a window
coming or going rewrites ``activity.json``, that an answer after a
restore lets the waiting steps run, and that a paused VM hands its
database connections back.
"""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path
from typing import Any

import pytest
from igni_cloud.guest import CloudGuest, GuestPaths, ProducerEnv, ReadyFile
from igni_cloud.guest.files import AtomicFile
from igni_cloud.guest.resume import RestoreWatch

from igni_core.backend.activity import Activity
from igni_core.backend.pool_rpcs import VmSignInRpc
from igni_core.core.config.schemas.storage_and_paths import StorageConfig
from igni_core.core.db.engine import engines
from igni_core.protocol import messages as msg

ORG = "org-acme"
PRODUCER = "person:u-ada"
INCARNATION = "m-1"


@pytest.fixture
def cloud(cloud_layer: Any, monkeypatch) -> None:
    """The cloud layer, with nothing named yet: a warm base's igni."""
    for name in (
        StorageConfig.DATABASE_URL_ENV,
        ProducerEnv.ORG_ENV,
        ProducerEnv.PRODUCER_ENV,
    ):
        monkeypatch.setenv(name, "")  # recorded, so put back as it was
        monkeypatch.delenv(name, raising=False)


class FakeTransport:
    """The backend's root transport, with the members the guest uses."""

    def __init__(self, clients: int = 1) -> None:
        self.client_count = clients
        self.sent: list[msg.Message] = []
        self.observers: list[Any] = []
        self.watchers: list[Any] = []

    async def send(self, message: msg.Message) -> None:
        self.sent.append(message)

    def observe(self, observer: Any) -> None:
        self.observers.append(observer)

    def watch_connections(self, watcher: Any) -> None:
        self.watchers.append(watcher)


class FakeModel:
    """The host object model, with the members the guest uses."""

    def __init__(self) -> None:
        self.services = types.SimpleNamespace(backend=self, activity_watch=self)
        self.listeners: list[Any] = []

    def activity(self) -> Activity:
        return Activity.of([])

    def listen(self, listener: Any) -> None:
        self.listeners.append(listener)


def _paths(tmp_path: Path) -> GuestPaths:
    return GuestPaths.from_env({GuestPaths.RUN_DIR_ENV: str(tmp_path)})


def _guest(tmp_path: Path) -> CloudGuest:
    return CloudGuest(
        _paths(tmp_path),
        restores=RestoreWatch(_paths(tmp_path).restored, watch=False),
        gc_freeze=False,
        practise=False,
    )


# ── Booting ───────────────────────────────────────────────────────


def test_a_ready_file_left_by_an_earlier_igni_is_removed(tmp_path: Path):
    paths = _paths(tmp_path)
    guest = CloudGuest(paths, gc_freeze=False, practise=False)
    (tmp_path / "ready").write_text("left by an igni that exited\n")
    guest.starting()
    assert not (tmp_path / "ready").exists(), (
        "a file left by an earlier igni must not say a new one serves"
    )


def test_the_guest_is_built_on_the_run_directory_the_environment_names(monkeypatch):
    monkeypatch.setenv(GuestPaths.RUN_DIR_ENV, "/run/igni")
    guest = CloudGuest.from_env()
    assert guest.paths.activity == Path("/run/igni/activity.json")
    assert guest.paths.ready == Path("/run/igni/ready")
    assert guest.paths.producer_env == Path("/run/igni/producer.env")
    assert isinstance(guest.ready, ReadyFile)


async def test_the_guest_does_not_wait_when_the_environment_names_a_producer(
    cloud, tmp_path: Path, monkeypatch
):
    monkeypatch.setenv(StorageConfig.DATABASE_URL_ENV, "sqlite:///x.db")
    guest = _guest(tmp_path)
    assert await guest.bind(None) is False, "the environment already names a producer"
    assert guest.binding.bound is None


# ── The call the gateway makes ─────────────────────────────────────


async def test_the_vm_sign_in_call_is_mounted_on_the_orchestrator(tmp_path: Path):
    guest = _guest(tmp_path)
    mounted: list[Any] = []

    class Orchestrator:
        def mount(self, rpc: Any) -> None:
            mounted.append(rpc)

    guest.serving(model=FakeModel(), transport=FakeTransport(), orchestrator=Orchestrator())
    try:
        assert len(mounted) == 1, "the gateway's call is mounted"
        assert isinstance(mounted[0], VmSignInRpc)
    finally:
        await guest.stop()


async def test_the_vm_sign_in_call_reaches_the_credential(tmp_path: Path):
    guest = _guest(tmp_path)
    mounted: list[Any] = []

    class Orchestrator:
        def mount(self, rpc: Any) -> None:
            mounted.append(rpc)

    guest.serving(model=FakeModel(), transport=FakeTransport(), orchestrator=Orchestrator())
    try:
        answer = await mounted[0].answer(
            msg.RPCRequest(id="r1", method="vm_sign_in", args={"session_token": "a-session"})
        )
        assert answer.error == ""
        assert answer.result is not None
        assert answer.result.state == "needs_sign_in", "no credential here: the person must sign in"
    finally:
        await guest.stop()


async def test_a_vm_sign_in_call_without_a_session_is_refused(tmp_path: Path):
    guest = _guest(tmp_path)
    mounted: list[Any] = []

    class Orchestrator:
        def mount(self, rpc: Any) -> None:
            mounted.append(rpc)

    guest.serving(model=FakeModel(), transport=FakeTransport(), orchestrator=Orchestrator())
    try:
        answer = await mounted[0].answer(
            msg.RPCRequest(id="r1", method="vm_sign_in", args={"session_token": ""})
        )
        assert answer.result is None
        assert "session_token" in answer.error
    finally:
        await guest.stop()


# ── Serving: the windows that come and go ─────────────────────────


async def test_a_window_that_comes_or_goes_is_written_into_the_activity_file(tmp_path: Path):
    guest = _guest(tmp_path)
    transport = FakeTransport(clients=1)
    guest.serving(model=FakeModel(), transport=transport)
    try:
        written = json.loads((tmp_path / "activity.json").read_text())
        assert written["clients"] == 1
        transport.client_count = 2
        transport.watchers[0]("attached", "c1")
        assert json.loads((tmp_path / "activity.json").read_text())["clients"] == 2, (
            "a window came: the file says so"
        )
        transport.client_count = 1
        transport.watchers[0]("detached", "c1")
        assert json.loads((tmp_path / "activity.json").read_text())["clients"] == 1
    finally:
        await guest.stop()


async def test_the_guest_watches_the_windows_and_the_answers(tmp_path: Path):
    guest = _guest(tmp_path)
    transport = FakeTransport()
    guest.serving(model=FakeModel(), transport=transport)
    try:
        assert len(transport.watchers) == 1, "the windows are watched"
        assert len(transport.observers) == 1, "the answers are observed"
    finally:
        await guest.stop()


async def test_an_answer_lets_the_restore_waiting_steps_run(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(RestoreWatch, "LATER_SECONDS", 0.05)
    paths = _paths(tmp_path)
    guest = CloudGuest(
        paths,
        restores=RestoreWatch(paths.restored, watch=False),
        gc_freeze=False,
        practise=False,
    )
    transport = FakeTransport()
    guest.serving(model=FakeModel(), transport=transport)
    try:
        AtomicFile(paths.restored).write(
            json.dumps({"generation": 2, "at_ms": 1, "clock_step_ms": 0.0}).encode()
        )
        assert await guest.restores.check()
        timeline = guest.restores.timeline
        assert timeline is not None
        transport.observers[0](msg.RPCResponse(id="r1"))
        assert any(step.step == "first answer" for step in timeline.record.steps), (
            "the answer went out: the waiting steps may run"
        )
    finally:
        await guest.stop()


async def test_a_window_attaching_is_marked_on_the_resume_timeline(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(RestoreWatch, "LATER_SECONDS", 0.05)
    paths = _paths(tmp_path)
    guest = CloudGuest(
        paths,
        restores=RestoreWatch(paths.restored, watch=False),
        gc_freeze=False,
        practise=False,
    )
    transport = FakeTransport()
    guest.serving(model=FakeModel(), transport=transport)
    try:
        AtomicFile(paths.restored).write(
            json.dumps({"generation": 2, "at_ms": 1, "clock_step_ms": 0.0}).encode()
        )
        assert await guest.restores.check()
        timeline = guest.restores.timeline
        assert timeline is not None
        transport.watchers[0]("attached", "c1")
        assert any(step.step == "window attached" for step in timeline.record.steps)
    finally:
        await guest.stop()


# ── Pausing ───────────────────────────────────────────────────────


async def test_a_paused_vm_hands_its_database_connections_back(tmp_path: Path, monkeypatch):
    closed: list[str] = []

    async def close_connections() -> None:
        closed.append("closed")

    monkeypatch.setattr(engines, "close_connections", close_connections)
    paths = _paths(tmp_path)
    guest = CloudGuest(
        paths,
        restores=RestoreWatch(paths.restored, watch=False),
        gc_freeze=False,
        practise=False,
    )
    guest.serving(model=FakeModel(), transport=FakeTransport())
    try:
        AtomicFile(paths.suspending).write(json.dumps({"token": "t1"}).encode())
        answer = await guest.suspends.check()
        assert answer is not None, "the suspend was answered"
        assert closed == ["closed"], "a paused VM holds no database connection"
        assert answer.unfinished == []
    finally:
        await guest.stop()


# ── Stopping ──────────────────────────────────────────────────────


async def test_stopping_before_serving_is_harmless(tmp_path: Path):
    guest = _guest(tmp_path)
    await guest.stop()
    assert not (tmp_path / "activity.json").exists()


async def test_stopping_twice_is_harmless(tmp_path: Path):
    guest = _guest(tmp_path)
    transport = FakeTransport()
    guest.serving(model=FakeModel(), transport=transport)
    await guest.stop()
    await guest.stop()
    assert not (tmp_path / "activity.json").exists()
    assert not (tmp_path / "ready").exists()


async def test_a_stopped_guest_leaves_no_watch_running(tmp_path: Path):
    guest = _guest(tmp_path)
    guest.serving(model=FakeModel(), transport=FakeTransport())
    await guest.stop()
    running = {task.get_name() for task in asyncio.all_tasks() if not task.done()}
    assert "restore-watch" not in running, "a stopped guest does not keep watching"
    assert "suspend-watch" not in running, "nor does it keep waiting to be paused"
