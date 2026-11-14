"""``SleepBlocker`` against a stand-in ``caffeinate``: the command line it
runs, what it does when that fails or dies, and how it counts."""

from __future__ import annotations

import logging
import os
import subprocess
import threading

import pytest

from igni_core.core.tools import sleep_blocker as module
from igni_core.core.tools.sleep_blocker import SleepBlocker


class _FakePopen:
    """A ``caffeinate`` that lives until it is told to die."""

    launched: list[_FakePopen] = []

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4000 + len(_FakePopen.launched)
        self.alive = True
        self.terminated = 0
        _FakePopen.launched.append(self)

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated += 1
        self.alive = False


@pytest.fixture(autouse=True)
def _macos_with_caffeinate(monkeypatch):
    _FakePopen.launched = []
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/caffeinate")
    monkeypatch.setattr(module.subprocess, "Popen", _FakePopen)


def test_the_first_acquire_runs_caffeinate_for_idle_sleep_tied_to_this_process():
    blocker = SleepBlocker()

    blocker.acquire()

    (proc,) = _FakePopen.launched
    assert proc.argv == ["caffeinate", "-i", "-w", str(os.getpid())]
    assert proc.kwargs["stdout"] is subprocess.DEVNULL
    assert proc.kwargs["stderr"] is subprocess.DEVNULL
    assert blocker.holding


def test_further_acquires_only_count_and_the_last_release_stops_it():
    blocker = SleepBlocker()

    blocker.acquire()
    blocker.acquire()
    blocker.release()

    assert len(_FakePopen.launched) == 1
    assert blocker.holding

    blocker.release()

    assert _FakePopen.launched[0].terminated == 1
    assert not blocker.holding


def test_releases_beyond_the_count_do_not_go_negative():
    blocker = SleepBlocker()
    blocker.release()
    blocker.release()

    blocker.acquire()

    assert blocker.holding
    blocker.release()
    assert not blocker.holding


def test_a_caffeinate_that_died_is_started_again_by_the_next_acquire():
    blocker = SleepBlocker()
    blocker.acquire()
    _FakePopen.launched[0].alive = False
    assert not blocker.holding

    blocker.acquire()

    assert len(_FakePopen.launched) == 2
    assert blocker.holding


def test_a_caffeinate_that_cannot_start_is_logged_and_the_count_is_kept(monkeypatch, caplog):
    def _fail(*_a, **_kw):
        raise OSError("no such file")

    monkeypatch.setattr(module.subprocess, "Popen", _fail)
    blocker = SleepBlocker()

    with caplog.at_level(logging.WARNING, logger=module.logger.name):
        blocker.acquire()

    assert not blocker.holding
    assert any("could not start caffeinate" in r.getMessage() for r in caplog.records)

    monkeypatch.setattr(module.subprocess, "Popen", _FakePopen)
    blocker.acquire()  # count is 2 now, and nothing is held: it starts
    assert blocker.holding
    blocker.release()
    assert blocker.holding, "the first acquire's reference was lost when the start failed"
    blocker.release()
    assert not blocker.holding


def test_releasing_after_caffeinate_already_exited_does_not_terminate_it():
    blocker = SleepBlocker()
    blocker.acquire()
    _FakePopen.launched[0].alive = False

    blocker.release()

    assert _FakePopen.launched[0].terminated == 0


def test_a_terminate_that_raises_is_swallowed(monkeypatch):
    blocker = SleepBlocker()
    blocker.acquire()

    def _boom():
        raise PermissionError("nope")

    monkeypatch.setattr(_FakePopen.launched[0], "terminate", _boom)

    blocker.release()  # must not raise

    assert not blocker.holding


def test_concurrent_acquires_start_one_caffeinate_and_balance_out():
    blocker = SleepBlocker()
    barrier = threading.Barrier(8)

    def _cycle():
        barrier.wait()
        blocker.acquire()

    threads = [threading.Thread(target=_cycle) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(_FakePopen.launched) == 1
    for _ in range(7):
        blocker.release()
    assert blocker.holding
    blocker.release()
    assert not blocker.holding


def test_off_macos_nothing_is_started(monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "linux")
    blocker = SleepBlocker()

    blocker.acquire()
    blocker.release()

    assert _FakePopen.launched == []
    assert not blocker.holding


def test_macos_without_caffeinate_on_path_is_unsupported(monkeypatch):
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    blocker = SleepBlocker()

    assert not blocker.supported
    blocker.acquire()
    assert _FakePopen.launched == []
