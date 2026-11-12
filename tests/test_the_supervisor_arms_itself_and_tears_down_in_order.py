"""The supervisor arms signals, watchdog and transport watcher in one call, and
teardown ends things in a fixed order whatever fails on the way.

``BackendSupervisor.started`` is the one constructor the backend entry point
uses; teardown is the one place the pool, igni's background processes, the
transport and the lockfile come down. Only the pool, the backend and the
transport are doubles: they are what the supervisor is handed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

import pytest

from igni_core.backend import supervisor as supervisor_mod
from igni_core.backend.lockfile import Lockfile
from igni_core.backend.schemas_rpc import LifecyclePhase
from igni_core.backend.supervisor import BackendSupervisor


class _Recorder:
    """Records the order in which the supervisor reaches its collaborators."""

    def __init__(self) -> None:
        self.order: list[str] = []


class _Transport:
    def __init__(self, rec: _Recorder, *, fails: bool = False) -> None:
        self._rec = rec
        self._fails = fails

    async def close(self) -> None:
        self._rec.order.append("transport")
        if self._fails:
            raise RuntimeError("already closed")


class _Pool:
    def __init__(self, rec: _Recorder, *, fails: bool = False) -> None:
        self._rec = rec
        self._fails = fails

    async def shutdown(self) -> list:
        self._rec.order.append("pool")
        if self._fails:
            raise RuntimeError("pool exploded")
        return []


class _Registry:
    def __init__(self, rec: _Recorder, *, fails: bool = False) -> None:
        self._rec = rec
        self._fails = fails

    def kill_all(self) -> int:
        self._rec.order.append("kill_all")
        if self._fails:
            raise RuntimeError("registry gone")
        return 2


class _Backend:
    def __init__(self, rec: _Recorder, *, kill_fails: bool = False) -> None:
        self._rec = rec

        class _Sup:
            registry = _Registry(rec, fails=kill_fails)

        self.supervisor = _Sup()

    async def shutdown(self) -> None:
        self._rec.order.append("backend")


# ── started() ─────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
async def test_started_arms_the_signals_the_watchdog_and_the_transport_watcher(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("IGNI_PARENT_PID", "0")
    rec = _Recorder()
    backend = _Backend(rec)
    sup = BackendSupervisor.started(
        transport=_Transport(rec),
        loop=asyncio.get_running_loop(),
        project_dir=tmp_path,
        backend=backend,
    )
    try:
        # the transport watcher is armed: a shutdown request closes the transport
        sup.request_shutdown()
        await asyncio.sleep(0.05)
        assert rec.order == ["transport"]

        # the shutdown target before a pool exists is the backend it was given
        await sup.teardown()
        assert "backend" in rec.order

        # the signal handler is live: SIGTERM to this process sets the event
        # on a fresh supervisor, which a plain constructor would not.
        rec2 = _Recorder()
        armed = BackendSupervisor.started(
            transport=_Transport(rec2),
            loop=asyncio.get_running_loop(),
            project_dir=tmp_path,
            backend=_Backend(rec2),
        )
        assert not armed.shutdown_event.is_set()
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(armed.wait_for_shutdown(), timeout=2)
        await armed.teardown()
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGUSR1):
            asyncio.get_running_loop().remove_signal_handler(sig)


async def test_started_arms_the_parent_watchdog(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IGNI_PARENT_PID", "4242")
    rec = _Recorder()
    probed: list[int] = []

    class _Probe:
        def is_alive(self, pid: int) -> bool:
            probed.append(pid)
            return True

    monkeypatch.setattr(supervisor_mod, "ParentProcessProbe", _Probe)
    monkeypatch.setattr(
        BackendSupervisor, "install_signal_handlers", lambda self: None, raising=True
    )
    sup = BackendSupervisor.started(
        transport=_Transport(rec),
        loop=asyncio.get_running_loop(),
        project_dir=tmp_path,
        backend=_Backend(rec),
    )
    await asyncio.sleep(0.05)
    assert probed == [4242]
    await sup.teardown()


# ── teardown order and failures ───────────────────────────────────


async def test_teardown_goes_pool_then_background_processes_then_transport_then_lockfile(
    tmp_path,
) -> None:
    (tmp_path / ".igni").mkdir()
    rec = _Recorder()
    sup = BackendSupervisor(
        transport=_Transport(rec), loop=asyncio.get_running_loop(), project_dir=tmp_path
    )
    sup.set_pool(_Pool(rec))
    sup.set_backend_fallback(_Backend(rec))
    sup.write_discovery_lockfile(5555, ws_token="prog")

    real_remove = Lockfile.remove

    def remove(self):  # type: ignore[no-untyped-def]
        rec.order.append("lockfile")
        return real_remove(self)

    Lockfile.remove = remove  # type: ignore[method-assign]
    try:
        await sup.teardown()
    finally:
        Lockfile.remove = real_remove  # type: ignore[method-assign]

    assert rec.order == ["pool", "kill_all", "transport", "lockfile"]
    assert Lockfile(tmp_path).read() is None


async def test_a_registry_that_cannot_kill_does_not_stop_the_rest_of_teardown(tmp_path) -> None:
    rec = _Recorder()
    sup = BackendSupervisor(
        transport=_Transport(rec), loop=asyncio.get_running_loop(), project_dir=tmp_path
    )
    sup.set_pool(_Pool(rec))
    sup.set_backend_fallback(_Backend(rec, kill_fails=True))

    await sup.teardown()

    assert rec.order == ["pool", "kill_all", "transport"]
    assert sup.phase is LifecyclePhase.STOPPED


async def test_a_pool_that_crashes_on_shutdown_is_reported_and_teardown_goes_on(
    tmp_path, caplog
) -> None:
    rec = _Recorder()
    sup = BackendSupervisor(
        transport=_Transport(rec), loop=asyncio.get_running_loop(), project_dir=tmp_path
    )
    sup.set_pool(_Pool(rec, fails=True))
    sup.set_backend_fallback(_Backend(rec))

    with caplog.at_level(logging.WARNING, logger=supervisor_mod.__name__):
        await sup.teardown()

    assert rec.order == ["pool", "kill_all", "transport"]
    assert sup.phase is LifecyclePhase.STOPPED
    assert any(
        r.levelno >= logging.WARNING and "pool exploded" in r.getMessage() for r in caplog.records
    )


async def test_a_transport_that_cannot_close_does_not_leave_the_lockfile(tmp_path) -> None:
    (tmp_path / ".igni").mkdir()
    rec = _Recorder()
    sup = BackendSupervisor(
        transport=_Transport(rec, fails=True),
        loop=asyncio.get_running_loop(),
        project_dir=tmp_path,
    )
    sup.write_discovery_lockfile(5556)

    await sup.teardown()

    assert Lockfile(tmp_path).read() is None
    assert sup.phase is LifecyclePhase.STOPPED


# ── evictor without a pool ────────────────────────────────────────


async def test_an_evictor_asked_for_before_a_pool_exists_warns_and_starts_nothing(
    tmp_path, caplog
) -> None:
    rec = _Recorder()
    sup = BackendSupervisor(
        transport=_Transport(rec), loop=asyncio.get_running_loop(), project_dir=tmp_path
    )

    with caplog.at_level(logging.WARNING, logger=supervisor_mod.__name__):
        sup.start_evictor(0.01)

    assert sup._evictor_task is None
    assert any("before set_pool" in r.getMessage() for r in caplog.records)
