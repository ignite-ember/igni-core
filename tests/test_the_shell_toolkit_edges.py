"""The shell toolkit's edges: what a list command, a bad directory, the
clamped waits, a stubborn process and a missing registry each do.

Each test drives ``IgniShellTools`` the way the model does — by tool
name, with real shell commands on a toolkit that owns its supervisor —
and reads the string that comes back.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import pytest

from igni_core.core.tools import shell as shell_module
from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.tools.shell import IgniShellTools
from igni_core.core.tools.shell_config import ShellToolsConfig


@pytest.fixture
async def supervisor():
    sup = ProcessSupervisor()
    try:
        yield sup
    finally:
        sup.registry.kill_all()
        await asyncio.sleep(0.3)  # let the killed children be reaped inside the loop


@pytest.fixture
def tools(tmp_path, supervisor):
    return IgniShellTools(base_dir=tmp_path, supervisor=supervisor)


def _pid_from(text: str) -> int:
    match = re.search(r"PID (\d+)", text)
    assert match, f"no pid in: {text!r}"
    return int(match.group(1))


async def test_a_list_command_is_joined_into_one_shell_string(tools):
    result = await tools.run_shell_command(["echo", "one", "two"])

    assert result == "one two"


async def test_the_base_dir_is_where_the_command_runs(tools, tmp_path):
    result = await tools.run_shell_command("pwd")

    assert Path(result.strip()).resolve() == tmp_path.resolve()


async def test_a_directory_that_does_not_exist_is_an_error_string_not_a_raise(tmp_path, supervisor):
    gone = tmp_path / "gone"
    tools = IgniShellTools(base_dir=gone, supervisor=supervisor)

    result = await tools.run_shell_command("echo hi")

    assert result.startswith("Error starting command: ")
    assert supervisor.registry.all_running() == []


async def test_a_wait_of_zero_or_less_is_one_second(tools, supervisor):
    started = await tools.run_shell_command("sleep 30", background=True)
    pid = _pid_from(started)

    loop = asyncio.get_running_loop()
    began = loop.time()
    result = await tools.await_process(pid, timeout_seconds=0)
    took = loop.time() - began

    assert "Still running" in result
    assert "Waited 1s and gave up" in result
    assert 0.9 <= took < 5

    negative = await tools.await_process(pid, timeout_seconds=-5)
    assert "Waited 1s and gave up" in negative


async def test_a_wait_above_a_day_is_a_day(tools, monkeypatch):
    started = await tools.run_shell_command("sleep 30", background=True)
    pid = _pid_from(started)

    async def _give_up(awaitable, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(shell_module.asyncio, "wait_for", _give_up)

    result = await tools.await_process(pid, timeout_seconds=10**9)

    assert "Waited 86400s and gave up" in result


async def test_a_watch_of_zero_is_one_second(tools):
    started = await tools.run_shell_command("sleep 30", background=True)
    pid = _pid_from(started)

    loop = asyncio.get_running_loop()
    began = loop.time()
    result = await tools.watch_process(pid, seconds=0)
    took = loop.time() - began

    assert "No new output in the last 1s." in result
    assert 0.9 <= took < 5


async def test_a_watch_above_thirty_seconds_is_thirty(tools, monkeypatch):
    started = await tools.run_shell_command("sleep 30", background=True)
    pid = _pid_from(started)

    async def _give_up(awaitable, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(shell_module.asyncio, "wait_for", _give_up)

    result = await tools.watch_process(pid, seconds=100)

    assert "No new output in the last 30s." in result


async def test_stopping_a_process_that_outlives_the_wait_still_reports_and_forgets_it(
    tools, supervisor, monkeypatch
):
    started = await tools.run_shell_command("sleep 30", background=True)
    pid = _pid_from(started)
    waits: list[float] = []

    async def _never_exits(awaitable, timeout):
        waits.append(timeout)
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(shell_module.asyncio, "wait_for", _never_exits)

    result = await tools.stop_process(pid)

    assert result.startswith(f"Process {pid} stopped.\nLast output:")
    assert waits == [5.0]
    assert supervisor.registry.get(pid) is None


async def test_cleanup_kills_only_its_own_supervisors_processes(tmp_path):
    first_sup, second_sup = ProcessSupervisor(), ProcessSupervisor()
    first = IgniShellTools(base_dir=tmp_path, supervisor=first_sup)
    second = IgniShellTools(base_dir=tmp_path, supervisor=second_sup)
    try:
        await first.run_shell_command("sleep 30", background=True)
        await second.run_shell_command("sleep 30", background=True)

        killed = first.cleanup()
        await asyncio.sleep(0.3)

        assert killed == 1
        assert await first.list_processes() == "No background processes running."
        assert "sleep 30" in await second.list_processes()
    finally:
        first_sup.registry.kill_all()
        second_sup.registry.kill_all()


def test_cleanup_with_nothing_running_kills_nothing(tools):
    assert tools.cleanup() == 0


def test_a_missing_agno_registry_is_logged_not_swallowed(tools, caplog):
    tools.functions = None

    with caplog.at_level(logging.WARNING, logger=shell_module.logger.name):
        tools._apply_confirmation(["run_shell_command"])

    assert any("has no ``functions`` attribute" in r.getMessage() for r in caplog.records)
    # The registry that does exist is still flagged.
    assert tools.async_functions["run_shell_command"].requires_confirmation is True


def test_a_custom_config_name_is_the_toolkit_name(supervisor):
    tools = IgniShellTools(ShellToolsConfig(name="plugin_shell"), supervisor=supervisor)

    assert tools.name == "plugin_shell"


def test_a_supervisor_is_required():
    with pytest.raises(TypeError):
        IgniShellTools()  # type: ignore[call-arg]
