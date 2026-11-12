"""``scripts/profile_multi_session.py`` measures a backend as sessions stack up.

It prints the cold boot and RSS at ready, the single-session RPC round
trip, the cost of each added session, the round trip with every session
busy and a verdict on its p95.
"""

from __future__ import annotations

import importlib.util
import re
import sys

import pytest

from tests.profile_script_rig import REPO, Rig

SCRIPT = "profile_multi_session.py"


@pytest.fixture
def rig(tmp_path):
    rig = Rig(tmp_path)
    yield rig
    rig.reap()


def test_it_spawns_igni_core_bound_to_its_parent(rig):
    result = rig.run(SCRIPT, "--sessions", "1")
    assert result.returncode == 0, result.stderr
    started = rig.started()
    assert started["argv"][:2] == ["-m", "igni_core.backend"]
    assert importlib.util.find_spec("igni_core.backend.__main__") is not None
    assert started["env"]["IGNI_PARENT_PID"] == str(started["ppid"])
    assert not any(key.startswith("EMBER_") for key in started["env"])


def test_the_project_dir_is_named_for_igni_not_the_old_product(rig):
    result = rig.run(SCRIPT, "--sessions", "1")
    shown = re.search(r"# Project dir: (\S+)", result.stdout).group(1)
    assert "ember" not in shown.rsplit("/", 1)[-1].lower()


def test_rss_at_ready_is_the_backend_tree_in_mib(rig):
    rig.env["RIG_PS_BASE"] = "204800"  # plus a child and a grandchild; the stranger is left out
    result = rig.run(SCRIPT, "--sessions", "1")
    assert re.search(r"# Cold boot: \d+\.\d\ds  ·  RSS at ready: 200\.1 MiB", result.stdout)


def test_ps_failing_reads_as_zero_memory(rig):
    rig.env["RIG_PS_FAIL"] = "1"
    result = rig.run(SCRIPT, "--sessions", "2")
    assert "RSS at ready: 0.0 MiB" in result.stdout
    assert (
        "session  2 create:" in result.stdout and "+0.0 MiB  ·  total RSS 0.0 MiB" in result.stdout
    )


def test_every_session_is_attached_under_its_own_id(rig):
    rig.run(SCRIPT, "--sessions", "3")
    attached = [r["args"]["session_id"] for r in rig.rpcs() if r["method"] == "attach_session"]
    assert attached == ["session-1", "session-2", "session-3"]


def test_the_single_session_round_trip_is_fifteen_calls(rig):
    rig.env["RIG_RPC_DELAY"] = "0.03"
    result = rig.run(SCRIPT, "--sessions", "1")
    line = re.search(
        r"# 1 session  ·  RPC RTT  p50=([\d.]+)ms  p95=([\d.]+)ms  max=([\d.]+)ms", result.stdout
    )
    assert line and 30 <= float(line.group(1)) < 200
    assert float(line.group(1)) <= float(line.group(2)) <= float(line.group(3))
    # 15 timed calls on top of the attach
    assert len([r for r in rig.rpcs() if r["method"] == "get_status"]) == 15 + 10


def test_each_added_session_reports_its_create_time_and_memory_delta(rig):
    rig.env["RIG_PS_STEP"] = "512"
    result = rig.run(SCRIPT, "--sessions", "3")
    creates = re.findall(r"# session\s+(\d) create:\s+\d+ms  ·  \+(\d\.\d) MiB", result.stdout)
    assert creates == [("2", "0.5"), ("3", "0.5")]
    assert "# Per-session RSS delta  median=0.5MiB  max=0.5MiB" in result.stdout


def test_a_session_that_shrinks_the_backend_shows_a_minus_sign(rig):
    rig.env["RIG_PS_STEP"] = "-1024"
    result = rig.run(SCRIPT, "--sessions", "2")
    assert re.search(r"# session  2 create:\s+\d+ms  ·  -1\.0 MiB", result.stdout)
    assert "median=-1.0MiB  max=-1.0MiB" in result.stdout


def test_one_session_has_no_per_session_overhead_to_report(rig):
    result = rig.run(SCRIPT, "--sessions", "1")
    assert "create:" not in result.stdout
    assert "Per-session RSS delta" not in result.stdout
    assert "# 1 sessions, concurrent RPC RTT" in result.stdout


@pytest.mark.parametrize(
    ("delay", "verdict"),
    [
        ("0", "HEALTHY (event loop stays responsive)"),
        ("0.05", "OK — some scheduler noise, no clear block"),
        ("0.15", "WARN — RTT > 100 ms p95, look for a sync block"),
    ],
)
def test_the_verdict_follows_the_p95_under_load(rig, delay, verdict):
    rig.env["RIG_RPC_DELAY"] = delay
    result = rig.run(SCRIPT, "--sessions", "2")
    assert result.returncode == 0, result.stderr
    assert f"# verdict: {verdict}  (p95=" in result.stdout
    assert "# Under load (2× 10 RPCs in " in result.stdout
    assert "p50=" in result.stdout and "p99=" in result.stdout and "max=" in result.stdout


def test_a_backend_that_exits_before_it_is_ready_reports_its_stderr(rig):
    rig.env["RIG_MODE"] = "die"
    rig.env["RIG_STDERR"] = "boom: no port"
    result = rig.run(SCRIPT)
    assert result.returncode != 0
    assert "RuntimeError: BE exited before ready." in result.stderr
    assert "boom: no port" in result.stderr


def test_a_welcome_that_is_not_a_welcome_aborts_and_still_stops_the_backend(rig):
    rig.env["RIG_MODE"] = "badwelcome"
    result = rig.run(SCRIPT, "--sessions", "2")
    assert result.returncode != 0
    assert "AssertionError" in result.stderr
    assert not rig.backend_alive()


def test_a_silent_backend_times_the_rpc_out_and_still_stops_it(rig):
    rig.env["RIG_MODE"] = "mute"
    result = rig.run(SCRIPT, "--sessions", "1")
    assert result.returncode != 0
    assert "TimeoutError" in result.stderr
    assert not rig.backend_alive()


def test_the_backend_is_stopped_when_the_run_ends(rig):
    result = rig.run(SCRIPT, "--sessions", "2")
    assert result.returncode == 0, result.stderr
    assert not rig.backend_alive()


def test_the_command_line_defaults():
    spec = importlib.util.spec_from_file_location("multi_probe_defaults", REPO / "scripts" / SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    seen = []

    async def drive(sessions):
        seen.append(sessions)

    mod._drive = drive
    old = sys.argv
    sys.argv = [SCRIPT]
    try:
        assert mod.main() == 0
    finally:
        sys.argv = old
    assert seen == [6]
