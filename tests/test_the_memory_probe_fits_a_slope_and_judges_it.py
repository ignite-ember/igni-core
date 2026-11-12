"""``scripts/profile_memory_over_time.py`` samples the backend's memory under load and judges the slope.

It boots ``igni_core.backend``, attaches N sessions, drives ``get_status``
at the target rate, prints a row per interval, then the RSS start / end /
peak, the regression slope in MiB/min and a verdict.
"""

from __future__ import annotations

import importlib.util
import re

import pytest

from tests.profile_script_rig import OLD_NAME, REPO, Rig

SCRIPT = "profile_memory_over_time.py"
SHORT = ("--sessions", "2", "--duration", "12", "--interval", "3")


@pytest.fixture
def rig(tmp_path):
    rig = Rig(tmp_path)
    yield rig
    rig.reap()


def _slope(result):
    return float(re.search(r"# Slope: ([+-][\d.]+) MiB/min", result.stdout).group(1))


def test_it_spawns_igni_core_bound_to_its_parent(rig):
    result = rig.run(SCRIPT, *SHORT)
    assert result.returncode == 0, result.stderr
    started = rig.started()
    assert started["argv"][:2] == ["-m", "igni_core.backend"]
    assert importlib.util.find_spec("igni_core.backend.__main__") is not None
    assert started["env"]["IGNI_PARENT_PID"] == str(started["ppid"])
    assert not any(key.upper().startswith(OLD_NAME.upper() + "_") for key in started["env"])


def test_the_project_dir_is_named_for_igni_not_the_old_product(rig):
    result = rig.run(SCRIPT, *SHORT)
    shown = re.search(r"# Project dir: (\S+)", result.stdout).group(1)
    assert OLD_NAME not in shown.rsplit("/", 1)[-1].lower()


def test_rss_is_the_backend_plus_its_descendants_and_nothing_else(rig):
    # backend 204800 KiB + a child (100) + a grandchild (50); the unrelated
    # process in the table (999999) and the malformed lines are left out.
    rig.env["RIG_PS_BASE"] = "204800"
    result = rig.run(SCRIPT, *SHORT)
    assert "RSS at ready: 200.1 MiB" in result.stdout
    assert "After 2 sessions attached: RSS 200.1 MiB (+0.0 MiB vs boot)" in result.stdout


def test_each_row_shows_time_rss_delta_and_descendants(rig):
    rig.env["RIG_PS_STEP"] = "1024"
    result = rig.run(SCRIPT, *SHORT)
    rows = re.findall(
        r"^#\s+(\d+\.\d)\s+(\d+\.\d)\s+\+\s*(\d+\.\d)\s+\d+\s+[\d.]+\s+(\d+)$", result.stdout, re.M
    )
    assert [round(float(r[0])) for r in rows] == [3, 6, 9, 12]
    # one more MiB at every call; the delta is against the attached reading
    assert [float(r[2]) for r in rows] == [1.0, 2.0, 3.0, 4.0]
    assert {r[3] for r in rows} == {"2"}  # the child and the grandchild


def test_a_shrinking_rss_shows_a_minus_sign(rig):
    rig.env["RIG_PS_STEP"] = "-1024"
    result = rig.run(SCRIPT, *SHORT)
    assert re.search(r"\s-\s*1\.0\s", result.stdout.split("kids")[1])


@pytest.mark.parametrize(
    ("step_kib", "verdict"),
    [
        (0, "HEALTHY"),
        (245, "HEALTHY"),
        (270, "MILD"),
        (1000, "MILD"),
        (1060, "WARN"),
        (-245, "HEALTHY"),
        (-270, "RSS shrinking"),
    ],
)
def test_the_verdict_follows_the_slope_in_mib_per_minute(rig, step_kib, verdict):
    rig.env["RIG_PS_STEP"] = str(step_kib)
    result = rig.run(SCRIPT, *SHORT)
    assert result.returncode == 0, result.stderr
    assert f"# verdict: {verdict}" in result.stdout


def test_the_slope_is_the_regression_of_rss_over_time(rig):
    rig.env["RIG_PS_STEP"] = "1536"  # 1.5 MiB per 3 s sample = 30 MiB/min
    result = rig.run(SCRIPT, *SHORT)
    assert 28.0 < _slope(result) < 30.5
    assert re.search(r"# RSS: start=\d+\.\d MiB  end=\d+\.\d MiB  peak=\d+\.\d MiB", result.stdout)
    peak = float(re.search(r"peak=([\d.]+)", result.stdout).group(1))
    end = float(re.search(r"end=([\d.]+)", result.stdout).group(1))
    assert peak == end  # growing, so the last sample is the peak


def test_the_throughput_is_the_completed_rpcs_over_the_configured_duration(rig):
    result = rig.run(SCRIPT, *SHORT)
    done = re.search(r"# Throughput: (\d+) RPCs in 12s = (\d+) req/s sustained", result.stdout)
    assert done
    # the rate is the count over the duration asked for, not the time measured
    assert done.group(2) == f"{int(done.group(1)) / 12:.0f}"
    served = [r for r in rig.rpcs() if r["method"] == "get_status"]
    assert len(served) >= int(done.group(1))


def test_one_sample_is_not_enough_for_a_summary(rig):
    result = rig.run(SCRIPT, "--sessions", "1", "--duration", "1", "--interval", "3")
    assert result.returncode == 0, result.stderr
    assert len(re.findall(r"^#\s+3\.\d\s", result.stdout, re.M)) == 1
    assert "# Slope" not in result.stdout and "verdict" not in result.stdout


def test_the_target_rate_sets_the_pause_between_a_sessions_requests(rig):
    result = rig.run(
        SCRIPT, "--sessions", "4", "--duration", "3", "--interval", "3", "--rate", "400"
    )
    assert "# Target rate: 400 req/s total  (10ms/req/session)" in result.stdout


@pytest.mark.parametrize("rate", ["0", "-5"])
def test_a_rate_that_is_not_positive_falls_back_to_ten_milliseconds(rig, rate):
    result = rig.run(
        SCRIPT, "--sessions", "4", "--duration", "3", "--interval", "3", "--rate", rate
    )
    assert f"# Target rate: {rate} req/s total  (10ms/req/session)" in result.stdout


def test_no_sessions_drive_nothing(rig):
    result = rig.run(SCRIPT, "--sessions", "0", "--duration", "6", "--interval", "3")
    assert result.returncode == 0, result.stderr
    assert [r for r in rig.rpcs() if r["method"] == "get_status"] == []
    assert "# Throughput: 0 RPCs in 6s = 0 req/s sustained" in result.stdout


def test_ps_failing_reads_as_zero_memory(rig):
    rig.env["RIG_PS_FAIL"] = "1"
    result = rig.run(SCRIPT, *SHORT)
    assert result.returncode == 0, result.stderr
    assert "RSS at ready: 0.0 MiB" in result.stdout


def test_a_backend_that_exits_before_it_is_ready_reports_its_stderr(rig):
    rig.env["RIG_MODE"] = "die"
    rig.env["RIG_STDERR"] = "boom: no port"
    result = rig.run(SCRIPT)
    assert result.returncode != 0
    assert "RuntimeError: BE exited before ready." in result.stderr
    assert "boom: no port" in result.stderr


def test_a_welcome_that_is_not_a_welcome_aborts_and_still_stops_the_backend(rig):
    rig.env["RIG_MODE"] = "badwelcome"
    result = rig.run(SCRIPT, *SHORT)
    assert result.returncode != 0
    assert "AssertionError" in result.stderr
    assert not rig.backend_alive()


def test_the_backend_is_stopped_when_the_run_ends(rig):
    result = rig.run(SCRIPT, *SHORT)
    assert result.returncode == 0, result.stderr
    assert not rig.backend_alive()


def test_a_backend_that_hangs_up_does_not_starve_the_sampler(rig):
    rig.env["RIG_MODE"] = "hangup"
    result = rig.run(SCRIPT, *SHORT, timeout=40)
    assert result.returncode == 0, result.stderr
    assert "# verdict" in result.stdout


def test_the_command_line_defaults():
    spec = importlib.util.spec_from_file_location(
        "memory_probe_defaults", REPO / "scripts" / SCRIPT
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    seen = {}

    async def run(**kwargs):
        seen.update(kwargs)

    mod._run = run
    import sys

    old = sys.argv
    sys.argv = [SCRIPT]
    try:
        assert mod.main() == 0
    finally:
        sys.argv = old
    assert seen == {"sessions": 4, "duration": 60.0, "interval": 3.0, "rate": 400.0}


@pytest.mark.parametrize(
    ("count", "p", "rank"),
    [(15, 0.95, 15), (20, 0.95, 19), (20, 0.5, 10), (10, 0.99, 10), (1, 0.95, 1), (100, 0.99, 99)],
)
def test_a_percentile_is_the_nearest_rank_value(count, p, rank):
    spec = importlib.util.spec_from_file_location("memory_percentile", REPO / "scripts" / SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._percentile(list(range(1, count + 1))[::-1], p) == rank
