"""The edges of the ``$`` shell runner: what a user reads when it is not the happy path.

``tests/test_dollar_shell_is_watched.py`` pins that a ``$`` command is
visible to the watcher. These pin what the result says: nothing to run,
output cap, merged stderr, a command that cannot start, how a backgrounded
command is announced, and that the runner uses the supervisor it is given.
"""

from __future__ import annotations

import signal

import pytest

from igni_core.backend.captured_shell_runner import CapturedShellRunner
from igni_core.core.tools.process_supervisor import ProcessSupervisor


@pytest.fixture
def supervisor():
    return ProcessSupervisor()


def _runner(tmp_path, supervisor, **kw):
    return CapturedShellRunner(project_dir=tmp_path, supervisor=supervisor, **kw)


def _stop_all(supervisor):
    for row in supervisor.registry.all_known():
        mp = supervisor.registry.get(row.pid)
        if mp is not None and mp.is_running():
            mp.kill()
        supervisor.registry.remove(row.pid)


@pytest.mark.parametrize("command", ["", "   ", "\n\t "])
async def test_nothing_to_run_returns_empty_output_and_spawns_nothing(
    tmp_path, supervisor, command
):
    result = await _runner(tmp_path, supervisor).run(command)

    assert (result.output, result.exit_code, result.pid) == ("", 0, None)
    assert supervisor.registry.all_known() == []


def test_the_runner_needs_a_supervisor_and_has_no_process_wide_fallback(tmp_path):
    with pytest.raises(TypeError):
        CapturedShellRunner(project_dir=tmp_path)  # type: ignore[call-arg]


async def test_a_command_is_recorded_in_the_supervisor_it_was_given(tmp_path):
    mine, other = ProcessSupervisor(), ProcessSupervisor()

    await _runner(tmp_path, mine).run("echo only-mine")

    assert any("only-mine" in row.cmd for row in mine.registry.all_known())
    assert other.registry.all_known() == []


async def test_a_command_runs_in_the_project_directory(tmp_path, supervisor):
    (tmp_path / "marker.txt").write_text("x")

    result = await _runner(tmp_path, supervisor).run("ls")

    assert "marker.txt" in result.output


async def test_stderr_is_merged_into_the_output(tmp_path, supervisor):
    result = await _runner(tmp_path, supervisor).run("echo to-out; echo to-err 1>&2; exit 3")

    assert "to-out" in result.output and "to-err" in result.output
    assert result.exit_code == 3


async def test_output_is_cut_to_its_last_bytes(tmp_path, supervisor):
    result = await _runner(tmp_path, supervisor, max_output_bytes=10).run(
        "echo aaaaaaaaaaaaaaaaaaaa; echo END-MARK"
    )

    assert len(result.output) <= 10
    assert result.output.endswith("END-MARK")


async def test_a_command_that_cannot_start_says_so_with_exit_minus_one(tmp_path, supervisor):
    result = await _runner(tmp_path / "missing-dir", supervisor).run("echo hi")

    assert result.exit_code == -1
    assert result.output.startswith("could not start: ")
    assert result.pid is None
    assert supervisor.registry.all_known() == []


async def test_the_last_line_of_a_fast_command_is_not_lost(tmp_path, supervisor):
    result = await _runner(tmp_path, supervisor).run("printf 'one\\ntwo\\nlast-no-newline'")

    assert result.output.endswith("last-no-newline")


async def test_a_command_killed_by_a_signal_reports_the_negative_signal_number(
    tmp_path, supervisor
):
    result = await _runner(tmp_path, supervisor).run("kill -9 $$")

    assert result.exit_code == -signal.SIGKILL
    assert result.backgrounded is False


async def test_a_command_that_outlives_the_wait_is_announced_and_named(tmp_path, supervisor):
    started: list[tuple[int, str]] = []
    supervisor.registry.bus.on("start", lambda ev: started.append((ev.pid, ev.cmd)))
    try:
        result = await _runner(tmp_path, supervisor, timeout_seconds=1).run("echo up; sleep 30")

        assert result.backgrounded is True
        assert result.exit_code == 0
        assert result.pid is not None
        assert started == [(result.pid, "echo up; sleep 30")]
        assert result.output.startswith(
            f"(still running after 1s — backgrounded as PID {result.pid}; "
            "see the Watcher to follow or stop it)\n"
        )
        assert "up" in result.output
        mp = supervisor.registry.get(result.pid)
        assert mp is not None and mp.is_running(), "backgrounded, not killed"
    finally:
        _stop_all(supervisor)


async def test_a_quick_command_is_never_announced_to_the_watcher(tmp_path, supervisor):
    started: list[int] = []
    supervisor.registry.bus.on("start", lambda ev: started.append(ev.pid))

    result = await _runner(tmp_path, supervisor).run("echo quick")

    assert started == []
    assert result.backgrounded is False


async def test_a_backgrounded_command_s_output_is_capped_too(tmp_path, supervisor):
    try:
        result = await _runner(
            tmp_path, supervisor, timeout_seconds=1, max_output_bytes=40
        ).run("echo xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx; sleep 30")

        assert result.backgrounded is True
        assert len(result.output) <= 40
    finally:
        _stop_all(supervisor)
