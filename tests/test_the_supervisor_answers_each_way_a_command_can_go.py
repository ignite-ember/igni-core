"""What the shell tool tells the model for each way a command can end,
and how the supervisor holds the one foreground slot.

Driven through the real shell toolkit on a real subprocess, because the
strings are the contract: the model reads them and decides whether to
retry, watch or stop. The 3 second startup window is real too.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from igni_core.core.tools.process_supervisor import ProcessSupervisor
from igni_core.core.tools.shell import IgniShellTools


@pytest.fixture
def supervisor() -> ProcessSupervisor:
    sup = ProcessSupervisor()
    yield sup
    sup.registry.kill_all()
    sup.reset()


@pytest.fixture
def tools(tmp_path, supervisor):
    return IgniShellTools(base_dir=tmp_path, supervisor=supervisor)


def _pid(text: str) -> int:
    match = re.search(r"PID (\d+)", text)
    assert match, f"no pid in: {text!r}"
    return int(match.group(1))


class TestABackgroundedCommand:
    async def test_one_that_crashes_in_the_startup_window_says_its_code_and_is_forgotten(
        self, tools, supervisor
    ):
        result = await tools.run_shell_command("echo boom; exit 3", background=True)

        assert result == "Background process exited with code 3:\nboom"
        assert supervisor.registry.all_running() == []
        assert supervisor.registry.all_known() == []

    async def test_one_that_finishes_cleanly_is_not_called_a_crash(self, tools):
        result = await tools.run_shell_command("echo done", background=True)

        assert result == "Background process finished during startup window (code 0):\ndone"

    async def test_one_still_running_shows_its_startup_output_and_how_to_watch_or_stop(
        self, tools, supervisor
    ):
        result = await tools.run_shell_command("echo up; sleep 30", background=True)

        pid = _pid(result)
        assert result == (
            f"Background process running (PID {pid}): echo up; sleep 30\n"
            "\nStartup output:\nup\n"
            f"\nUse watch_process({pid}) to monitor, stop_process({pid}) to stop."
        )
        entry = supervisor.registry.get(pid)
        assert entry is not None and entry.is_running()
        assert entry.was_backgrounded

    async def test_a_silent_one_says_so(self, tools):
        result = await tools.run_shell_command("sleep 30", background=True)

        assert "\nNo output yet (process is running silently).\n" in result
        assert "Startup output:" not in result


class TestAForegroundCommandThatOutlivesItsTimeout:
    async def test_is_promoted_to_the_background_and_says_how_to_follow_it(self, tools, supervisor):
        result = await tools.run_shell_command("echo partial; sleep 30", timeout=1)

        pid = _pid(result)
        assert result.startswith(
            f"Command still running after 1s — backgrounded as PID {pid}.\n"
            f"Use read_process_output({pid}) to check output.\n"
            f"Use stop_process({pid}) to stop it.\n\n"
            "Output so far:\npartial"
        )
        entry = supervisor.registry.get(pid)
        assert entry is not None and entry.is_running()
        assert entry.was_backgrounded
        assert supervisor.foreground_process is None, "a promoted command no longer holds the slot"


class TestAForegroundCommandThatFinishes:
    async def test_a_failure_with_output_carries_both(self, tools, supervisor):
        result = await tools.run_shell_command("echo oops; exit 4")

        assert result == "Command exited with code 4:\noops"
        assert supervisor.registry.all_known() == []

    async def test_a_long_output_keeps_the_last_lines_and_says_how_to_run_it_narrower(self, tools):
        result = await tools.run_shell_command("seq 1 20000")

        assert result.startswith("[Output truncated: 20000 lines; the first 19900 are not shown")
        assert ProcessSupervisor.FINISHED_SEE_MORE in result
        assert result.endswith("19999\n20000")

    async def test_a_result_past_the_character_cap_is_cut_in_the_middle(self, tools):
        result = await tools.run_shell_command("head -c 60000 /dev/zero | tr '\\0' x")

        assert "... (30000 characters truncated) ..." in result
        assert result.startswith("x" * 100)
        assert result.endswith("x" * 100)


class TestTheForegroundSlot:
    def test_it_holds_the_process_inside_and_empties_after(self, supervisor):
        mp = object()
        with supervisor.foreground(mp):  # type: ignore[arg-type]
            assert supervisor.foreground_process is mp
        assert supervisor.foreground_process is None

    def test_it_empties_when_the_block_raises(self, supervisor):
        with (
            pytest.raises(RuntimeError),
            supervisor.foreground(object()),  # type: ignore[arg-type]
        ):
            raise RuntimeError("boom")
        assert supervisor.foreground_process is None

    def test_a_first_process_leaving_does_not_empty_a_second_one_that_replaced_it(self, supervisor):
        first, second = object(), object()
        with supervisor.foreground(first):  # type: ignore[arg-type]
            with supervisor.foreground(second):  # type: ignore[arg-type]
                pass
            # the inner block ended and emptied the slot it owned
            assert supervisor.foreground_process is None

        entering = supervisor.foreground(first)  # type: ignore[arg-type]
        entering.__enter__()
        replacing = supervisor.foreground(second)  # type: ignore[arg-type]
        replacing.__enter__()
        entering.__exit__(None, None, None)
        assert supervisor.foreground_process is second
        replacing.__exit__(None, None, None)
        assert supervisor.foreground_process is None

    def test_cancelling_with_nothing_running_is_false(self, supervisor):
        assert supervisor.cancel_foreground() is False

    def test_cancelling_a_process_that_already_exited_is_false_and_leaves_the_slot(
        self, supervisor
    ):
        class Done:
            killed = False

            def is_running(self):
                return False

            def kill(self):
                self.killed = True

        done = Done()
        with supervisor.foreground(done):  # type: ignore[arg-type]
            assert supervisor.cancel_foreground() is False
            assert supervisor.foreground_process is done
        assert not done.killed

    def test_reset_empties_the_slot(self, supervisor):
        entering = supervisor.foreground(object())  # type: ignore[arg-type]
        entering.__enter__()

        supervisor.reset()

        assert supervisor.foreground_process is None

    async def test_cancelling_kills_the_running_command_and_empties_the_slot(
        self, tools, supervisor
    ):
        task = asyncio.create_task(tools.run_shell_command("sleep 30", timeout=25))
        for _ in range(100):
            if supervisor.foreground_process is not None:
                break
            await asyncio.sleep(0.05)
        assert supervisor.foreground_process is not None

        assert supervisor.cancel_foreground() is True

        assert supervisor.foreground_process is None
        result = await asyncio.wait_for(task, timeout=10)
        assert result.startswith("Command exited with code -")
        assert supervisor.cancel_foreground() is False


def test_configuring_the_log_store_moves_the_one_the_readers_hold(supervisor, tmp_path):
    held = supervisor.log_store

    supervisor.configure_log_store(tmp_path)

    assert supervisor.log_store is held
    assert held.project_dir == tmp_path


def test_the_module_offers_only_the_supervisor_and_its_placeholder():
    """The registry lives in its own module; this one does not re-offer it."""
    import igni_core.core.tools.process_supervisor as module

    assert sorted(module.__all__) == ["NO_OUTPUT_PLACEHOLDER", "ProcessSupervisor"]
    assert module.NO_OUTPUT_PLACEHOLDER == "(no output)"
