"""``scripts/profile_fd_count.py`` counts the igni backend's descriptors across session churn.

It spawns ``igni_core.backend`` with a one second idle timeout, prints
the descriptors by kind at boot and at each step of each cycle, and
ends with a verdict on how far the post-eviction baseline drifted.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from tests.profile_script_rig import OLD_NAME, REPO, Rig

SCRIPT = "profile_fd_count.py"
FAST = ("--evict-wait", "0.1")


@pytest.fixture
def rig(tmp_path):
    rig = Rig(tmp_path)
    yield rig
    rig.reap()


def _lines(result):
    return [line for line in result.stdout.splitlines() if line.startswith("# [")]


def test_it_spawns_igni_core_with_a_one_second_idle_timeout(rig):
    result = rig.run(SCRIPT, "--sessions", "1", "--cycles", "1", "--requests-per-cycle", "1", *FAST)
    assert result.returncode == 0, result.stderr
    started = rig.started()
    assert started["argv"][:2] == ["-m", "igni_core.backend"]
    assert importlib.util.find_spec("igni_core.backend.__main__") is not None
    assert started["argv"][2] == "--ws-port" and started["argv"][3] == "0"
    # the backend reads IGNI_* names; the old names would be ignored by it
    assert started["env"]["IGNI_SESSION_IDLE_TIMEOUT"] == "1"
    assert started["env"]["IGNI_PARENT_PID"] == str(started["ppid"])
    assert not any(key.upper().startswith(OLD_NAME.upper() + "_") for key in started["env"])


def test_the_project_dir_is_named_for_igni_not_the_old_product(rig):
    result = rig.run(SCRIPT, "--sessions", "1", "--cycles", "1", "--requests-per-cycle", "1", *FAST)
    shown = re.search(r"# Project dir: (\S+)", result.stdout).group(1)
    assert OLD_NAME not in shown.rsplit("/", 1)[-1].lower()
    assert shown == rig.started()["argv"][-1]


def test_it_reports_boot_then_each_step_of_each_cycle(rig):
    rig.env["RIG_LSOF_EXTRA"] = "10"
    result = rig.run(SCRIPT, "--sessions", "2", "--cycles", "2", "--requests-per-cycle", "4", *FAST)
    assert result.returncode == 0, result.stderr
    labels = [re.match(r"# \[(.*?)\s*\]", line).group(1) for line in _lines(result)]
    assert labels == [
        "BOOT",
        "cycle1 ATTACHED",
        "cycle1 AFTER LOAD",
        "cycle1 DISCONNECTED",
        "cycle1 AFTER EVICT",
        "cycle2 ATTACHED",
        "cycle2 AFTER LOAD",
        "cycle2 DISCONNECTED",
        "cycle2 AFTER EVICT",
    ]


def test_the_breakdown_counts_real_descriptors_by_kind_largest_first(rig):
    rig.env["RIG_LSOF_EXTRA"] = "3"
    result = rig.run(SCRIPT, "--sessions", "1", "--cycles", "1", "--requests-per-cycle", "1", *FAST)
    boot = _lines(result)[0]
    # cwd / txt / mem / NOFD / short rows are not descriptors; 7 are, plus 3 more pipes.
    assert "FDs =   10" in boot
    assert boot.endswith("pipe=4  socket=2  chr=1  dir=1  regular=1  unix-socket=1")


def test_without_lsof_the_profile_reports_no_descriptors(rig, tmp_path):
    (rig.bin / "lsof").unlink()
    result = rig.run(SCRIPT, "--sessions", "1", "--cycles", "1", "--requests-per-cycle", "1", *FAST)
    assert result.returncode == 0, result.stderr
    assert "FDs =    0   (none)" in _lines(result)[0]


def test_a_hung_lsof_is_given_up_on_after_five_seconds(rig):
    rig.env["RIG_LSOF_HANG"] = "1"
    result = rig.run(SCRIPT, "--sessions", "1", "--cycles", "1", "--requests-per-cycle", "1", *FAST)
    assert result.returncode == 0, result.stderr
    assert all("FDs =    0   (none)" in line for line in _lines(result))


def test_each_cycle_attaches_its_own_sessions_and_spreads_requests_round_robin(rig):
    result = rig.run(SCRIPT, "--sessions", "3", "--cycles", "2", "--requests-per-cycle", "5", *FAST)
    assert result.returncode == 0, result.stderr
    rpcs = rig.rpcs()
    attached = [r["args"]["session_id"] for r in rpcs if r["method"] == "attach_session"]
    assert attached == ["c1-s0", "c1-s1", "c1-s2", "c2-s0", "c2-s1", "c2-s2"]
    assert sum(1 for r in rpcs if r["method"] == "get_status") == 10


def test_every_cycle_forces_an_eviction_with_sigusr1(rig):
    rig.run(SCRIPT, "--sessions", "1", "--cycles", "3", "--requests-per-cycle", "1", *FAST)
    assert rig.usr1_count() == 3


@pytest.mark.parametrize(
    ("drift", "verdict"),
    [
        (0, "HEALTHY"),
        (5, "HEALTHY"),
        (6, "MILD"),
        (20, "MILD"),
        (21, "WARN"),
    ],
)
def test_the_verdict_follows_the_drift_between_first_and_last_cycle(rig, drift, verdict):
    rig.env["RIG_LSOF_DRIFT"] = str(drift)
    result = rig.run(SCRIPT, "--sessions", "1", "--cycles", "2", "--requests-per-cycle", "1", *FAST)
    assert result.returncode == 0, result.stderr
    assert f"# verdict: {verdict}" in result.stdout
    # the table shows each baseline against boot: the second is one drift above the first
    assert f"({drift:+d} vs boot)" in result.stdout.split("cycle 1:")[1].splitlines()[0]
    assert f"({2 * drift:+d} vs boot)" in result.stdout.split("cycle 2:")[1].splitlines()[0]


def test_a_single_cycle_has_no_verdict(rig):
    result = rig.run(SCRIPT, "--sessions", "1", "--cycles", "1", "--requests-per-cycle", "1", *FAST)
    assert "# Cycle baselines" in result.stdout
    assert "verdict" not in result.stdout


def test_no_sessions_is_a_run_with_nothing_attached_not_a_crash(rig):
    result = rig.run(SCRIPT, "--sessions", "0", "--cycles", "1", "--requests-per-cycle", "5", *FAST)
    assert result.returncode == 0, result.stderr
    assert "ZeroDivisionError" not in result.stderr
    assert not [r for r in rig.rpcs() if r["method"] == "get_status"]


def test_a_chatty_backend_does_not_block_on_its_output(rig):
    # 400 kB after ready: well past what a pipe holds when nothing reads it.
    rig.env["RIG_CHATTY_KB"] = "400"
    result = rig.run(SCRIPT, "--sessions", "1", "--cycles", "1", "--requests-per-cycle", "1", *FAST)
    assert result.returncode == 0, result.stderr
    shown = re.search(r"# Project dir: (\S+)", result.stdout).group(1)
    log = Path(shown) / "backend.log"
    assert f"# Backend log: {log}" in result.stdout
    text = log.read_text()
    assert "chatter 399 " in text and "chatter 398 " in text  # both stdout and stderr land there


def test_a_backend_that_exits_before_it_is_ready_aborts_the_run(rig):
    rig.env["RIG_MODE"] = "die"
    result = rig.run(SCRIPT)
    assert result.returncode != 0
    assert "RuntimeError: BE exited before ready" in result.stderr


def test_a_silent_backend_times_the_rpc_out_and_still_stops_it(rig):
    rig.env["RIG_MODE"] = "mute"
    result = rig.run(SCRIPT, "--sessions", "1", "--cycles", "1", "--requests-per-cycle", "1", *FAST)
    assert result.returncode != 0
    assert "TimeoutError" in result.stderr
    assert not rig.backend_alive()


def test_the_backend_is_stopped_when_the_run_ends(rig):
    result = rig.run(SCRIPT, "--sessions", "1", "--cycles", "1", "--requests-per-cycle", "1", *FAST)
    assert result.returncode == 0, result.stderr
    assert not rig.backend_alive()


def test_the_command_line_defaults():
    spec = importlib.util.spec_from_file_location("fd_probe_defaults", REPO / "scripts" / SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    captured = {}

    async def drive(args):
        captured.update(vars(args))

    mod._drive = drive
    old = sys.argv
    sys.argv = [SCRIPT]
    try:
        assert mod.main() == 0
    finally:
        sys.argv = old
    assert captured == {
        "sessions": 6,
        "cycles": 4,
        "requests_per_cycle": 20,
        "evict_wait": 8.0,
    }
