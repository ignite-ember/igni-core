"""``ProcessesController.stop`` on a live process, and the bounds of ``read_tail``.

* a real shell child is killed, its reader task is awaited, and the tail
  read back afterwards is the whole of what it printed;
* a reader that never ends does not hold the stop for more than its
  two-second bound, and a reader that blew up does not fail it;
* ``read_tail`` is bounded by its ``tail`` and answers an unknown pid
  with nothing rather than an error.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from igni_core.backend.server_processes import ProcessesController
from igni_core.core.tools.managed_process import ManagedProcess
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from tests.process_fakes import FakeTrackedProcess


@pytest.fixture
def supervisor() -> ProcessSupervisor:
    return ProcessSupervisor()


async def test_a_real_process_is_killed_and_its_output_is_whole(supervisor) -> None:
    proc = await asyncio.create_subprocess_shell(
        "echo one; echo two; sleep 30",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,  # as the shell tool starts it: kill takes the group
    )
    mp = ManagedProcess(proc, "echo one; echo two; sleep 30", supervisor)
    pid = supervisor.registry.add(mp)
    mp.start_reader()
    processes = ProcessesController(supervisor)
    try:
        for _ in range(100):
            if processes.read_tail(pid).output.count("\n") >= 1:
                break
            await asyncio.sleep(0.05)
        live = processes.read_tail(pid)
        assert (live.is_running, live.exit_code) == (True, None)
        assert [row.pid for row in processes.list() if row.is_running] == [pid]

        stopped = await processes.stop(pid)

        assert (stopped.killed, stopped.message) == (True, f"pid {pid} killed")
        assert mp.reader_task is not None and mp.reader_task.done()
        after = processes.read_tail(pid)
        assert after.output == "one\ntwo"
        assert after.is_running is False
        assert after.exit_code is not None
        again = await processes.stop(pid)
        assert again.killed is False
        assert again.message.startswith(f"pid {pid} already exited (rc=")
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


async def test_stop_waits_for_the_reader_to_flush_the_last_line(supervisor) -> None:
    fake = FakeTrackedProcess(pid=501, output=["early"])

    async def flush() -> None:
        await asyncio.sleep(0.2)
        fake._output.append("late")

    fake._reader_task = asyncio.ensure_future(flush())
    supervisor.registry.add(fake)
    processes = ProcessesController(supervisor)

    result = await processes.stop(501)

    assert result.killed is True
    assert processes.read_tail(501).output == "early\nlate"


async def test_a_reader_that_never_ends_holds_the_stop_for_two_seconds_at_most(supervisor) -> None:
    fake = FakeTrackedProcess(pid=502)
    fake._reader_task = asyncio.ensure_future(asyncio.sleep(60))
    supervisor.registry.add(fake)

    started = time.monotonic()
    result = await ProcessesController(supervisor).stop(502)

    assert result.killed is True and result.message == "pid 502 killed"
    assert 1.5 < time.monotonic() - started < 5


async def test_a_reader_that_raised_does_not_fail_the_stop(supervisor) -> None:
    async def boom() -> None:
        raise RuntimeError("reader died")

    fake = FakeTrackedProcess(pid=503)
    fake._reader_task = asyncio.ensure_future(boom())
    await asyncio.sleep(0)
    supervisor.registry.add(fake)

    result = await ProcessesController(supervisor).stop(503)

    assert (result.killed, result.message) == (True, "pid 503 killed")


async def test_an_unknown_pid_cannot_be_stopped_or_read(supervisor) -> None:
    processes = ProcessesController(supervisor)

    stopped = await processes.stop(999)
    tail = processes.read_tail(999)

    assert (stopped.killed, stopped.message) == (False, "pid 999 not in registry")
    assert (tail.output, tail.is_running, tail.exit_code) == ("", False, None)
    assert processes.list() == []


def test_the_tail_is_the_last_lines_asked_for(supervisor) -> None:
    supervisor.registry.add(
        FakeTrackedProcess(pid=7, output=["a", "b", "c", "d"], running=False, returncode=3)
    )
    processes = ProcessesController(supervisor)

    assert processes.read_tail(7, tail=2).output == "c\nd"
    assert processes.read_tail(7, tail=500).output == "a\nb\nc\nd"
    assert processes.read_tail(7).exit_code == 3
    [row] = processes.list()
    assert (row.pid, row.is_running, row.exit_code) == (7, False, 3)


def test_a_running_process_has_no_exit_code_yet(supervisor) -> None:
    # A handle may already carry a return code while the process is still
    # reported running; the panel must not show it as an exit.
    supervisor.registry.add(FakeTrackedProcess(pid=8, output=["x"], running=True, returncode=9))

    tail = ProcessesController(supervisor).read_tail(8)

    assert (tail.is_running, tail.exit_code) == (True, None)


class _Orphan(FakeTrackedProcess):
    """A process re-adopted after a restart: nothing reads it, nobody reaps it."""

    @property
    def is_orphan(self) -> bool:
        return True


async def test_stopping_an_orphan_takes_it_out_of_the_registry(supervisor) -> None:
    orphan = _Orphan(pid=504)
    supervisor.registry.add(orphan)
    processes = ProcessesController(supervisor)

    result = await processes.stop(504)

    assert (result.killed, result.message) == (True, "pid 504 killed")
    assert supervisor.registry.get(504) is None
    assert processes.list() == []
