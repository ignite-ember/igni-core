"""``HeadlessRun.run`` ends the process with the session's own status.

``igni -m`` and ``igni -p`` hand a whole session to ``HeadlessRun``; what a
script sees afterwards is the exit code, the traceback on stderr, and whether
the process came back at all. A session that exits with a message
(``sys.exit("...")``) used to crash the exit handler itself with a
``ValueError`` from ``int()``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

import pytest

from igni_core.cli.headless_run import HeadlessRun


def _run(session, cleanup=lambda: None) -> None:
    HeadlessRun(drain_timeout_seconds=0.05).run(session, cleanup=cleanup)


async def _returns() -> None:
    return None


def _exits(code):
    async def session() -> None:
        raise SystemExit(code)

    return session()


def test_a_session_that_finishes_returns_without_exiting() -> None:
    _run(_returns())


@pytest.mark.parametrize("code", [None, 0])
def test_a_session_exiting_with_no_status_is_a_success(code) -> None:
    _run(_exits(code))


def test_a_session_exit_code_is_the_process_exit_code() -> None:
    with pytest.raises(SystemExit) as raised:
        _run(_exits(3))
    assert raised.value.code == 3


def test_a_session_exiting_with_a_message_prints_it_and_fails(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        _run(_exits("the model is not configured"))
    assert raised.value.code == 1
    assert "the model is not configured" in capsys.readouterr().err


def test_a_session_that_raises_prints_the_traceback_and_exits_one(capsys) -> None:
    async def session() -> None:
        raise RuntimeError("boom from the session")

    with pytest.raises(SystemExit) as raised:
        _run(session())
    assert raised.value.code == 1
    err = capsys.readouterr().err
    assert "Traceback" in err and "boom from the session" in err


def test_a_session_that_raises_still_has_its_leftover_tasks_cancelled() -> None:
    leaked: list[asyncio.Task[None]] = []

    async def session() -> None:
        async def forever() -> None:
            await asyncio.sleep(3600)

        leaked.append(asyncio.create_task(forever()))
        await asyncio.sleep(0)
        raise RuntimeError("after leaking")

    with pytest.raises(SystemExit):
        _run(session())
    assert leaked[0].cancelled()


def test_cleanup_runs_after_the_session_and_before_exit() -> None:
    order: list[str] = []

    async def session() -> None:
        order.append("session")

    _run(session(), cleanup=lambda: order.append("cleanup"))
    assert order == ["session", "cleanup"]


def test_cleanup_runs_when_the_session_failed() -> None:
    called: list[bool] = []
    with pytest.raises(SystemExit) as raised:
        _run(_exits(2), cleanup=lambda: called.append(True))
    assert called == [True] and raised.value.code == 2


def test_a_cleanup_that_fails_is_logged_and_does_not_change_the_status(caplog) -> None:
    def cleanup() -> None:
        raise OSError("worktree is gone")

    with caplog.at_level(logging.DEBUG, logger="igni_core.cli.headless_run"):
        _run(_returns(), cleanup=cleanup)
    assert any("cleanup failed on exit" in r.getMessage() for r in caplog.records)

    with pytest.raises(SystemExit) as raised:
        _run(_exits(4), cleanup=cleanup)
    assert raised.value.code == 4


def test_output_is_flushed_before_exit(monkeypatch) -> None:
    flushed: list[str] = []

    class Stream:
        def __init__(self, name: str) -> None:
            self.name = name

        def flush(self) -> None:
            flushed.append(self.name)

        def write(self, _text: str) -> int:
            return 0

    monkeypatch.setattr("sys.stdout", Stream("stdout"))
    monkeypatch.setattr("sys.stderr", Stream("stderr"))
    _run(_returns())
    assert flushed == ["stdout", "stderr"]


class _HardExit(BaseException):
    """Stands in for the process ending at ``os._exit``."""


def _hard_exit_recorder(monkeypatch) -> list[int]:
    left: list[int] = []

    def fake_exit(code: int) -> None:
        left.append(code)
        raise _HardExit

    monkeypatch.setattr(os, "_exit", fake_exit)
    return left


@pytest.fixture
def lingering_thread():
    stop = threading.Event()
    thread = threading.Thread(target=stop.wait, daemon=False)
    thread.start()
    yield thread
    stop.set()
    thread.join()


def test_a_non_daemon_thread_makes_the_run_leave_without_joining_it(
    monkeypatch, lingering_thread
) -> None:
    left = _hard_exit_recorder(monkeypatch)
    with pytest.raises(_HardExit):
        _run(_exits(7))
    assert left == [7]


def test_a_non_daemon_thread_does_not_turn_success_into_failure(
    monkeypatch, lingering_thread
) -> None:
    left = _hard_exit_recorder(monkeypatch)
    with pytest.raises(_HardExit):
        _run(_returns())
    assert left == [0]


def test_daemon_threads_and_the_main_thread_are_not_lingering() -> None:
    stop = threading.Event()
    daemon = threading.Thread(target=stop.wait, daemon=True)
    daemon.start()
    try:
        assert HeadlessRun._lingering_threads() == []
    finally:
        stop.set()
        daemon.join()


def test_a_non_daemon_thread_is_lingering(lingering_thread) -> None:
    assert HeadlessRun._lingering_threads() == [lingering_thread]
