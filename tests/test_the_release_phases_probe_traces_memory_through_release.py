"""``scripts/profile_release_phases.py`` traces the backend's RSS from boot to eviction.

It boots the backend with INFO logging, runs the BOOT, ATTACH, LOAD,
QUIESCE and DROP phases, forces a sweep with SIGUSR1, and judges what
was retained against the attached baseline.
"""

from __future__ import annotations

import importlib.util
import re
import sys

import pytest

from tests.profile_script_rig import OLD_NAME, REPO, Rig

SCRIPT = "profile_release_phases.py"
SHORT = ("--sessions", "2", "--load-seconds", "10", "--quiesce-seconds", "6")


@pytest.fixture
def rig(tmp_path):
    rig = Rig(tmp_path)
    yield rig
    rig.reap()


def _phases(rig, net_mib):
    rig.env.update(
        RIG_PS_BOOT_KB=str(100 * 1024),
        RIG_PS_ATTACHED_KB=str(200 * 1024),
        RIG_PS_GC_DELTA_KB=str(int(net_mib * 1024)),
    )


def test_it_starts_igni_core_with_info_logging_bound_to_its_parent(rig):
    result = rig.run(SCRIPT, *SHORT)
    assert result.returncode == 0, result.stderr
    started = rig.started()
    code = started["argv"][1]
    assert started["argv"][0] == "-c"
    compile(code, "<probe>", "exec")
    assert "from igni_core.backend.__main__ import main" in code
    assert "logging.getLogger('igni_core').setLevel(logging.INFO)" in code
    assert OLD_NAME not in code.lower()
    assert started["argv"][2:4] == ["--ws-port", "0"]
    assert started["env"]["PYTHONUNBUFFERED"] == "1"
    assert started["env"]["IGNI_PARENT_PID"] == str(started["ppid"])
    assert not any(key.upper().startswith(OLD_NAME.upper() + "_") for key in started["env"])
    # the entry point the probe imports exists
    module = importlib.util.find_spec("igni_core.backend.__main__")
    assert module is not None


def test_the_project_dir_is_named_for_igni_not_the_old_product(rig):
    result = rig.run(SCRIPT, *SHORT)
    shown = rig.started()["argv"][-1]
    assert OLD_NAME not in shown.rsplit("/", 1)[-1].lower()
    assert "igni-release-" in shown
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("--idle-timeout", "7"), "7.0"),
        ((), None),
        (("--idle-timeout", "0"), None),
        (("--idle-timeout", "-3"), None),
    ],
)
def test_the_idle_timeout_is_passed_only_when_positive(rig, args, expected):
    rig.run(SCRIPT, *SHORT, *args)
    assert rig.started()["env"].get("IGNI_SESSION_IDLE_TIMEOUT") == expected


def test_it_prints_each_phase_with_its_rss(rig):
    _phases(rig, 0)
    result = rig.run(SCRIPT, *SHORT)
    out = result.stdout
    assert re.search(r"\[\s*[\d.]+s\]  BOOT\s+RSS =   100\.0 MiB", out)
    assert re.search(r"\[\s*[\d.]+s\]  ATTACHED\s+RSS =   200\.0 MiB", out)
    assert re.search(r"LOAD\s+start RSS =   200\.0 MiB", out)
    assert re.search(r"QUIESCE\s+start RSS =   200\.0 MiB", out)
    assert re.search(r"DROP\s+start RSS =   200\.0 MiB", out)
    assert re.search(r"AFTER GC\+EVICT\s+RSS =   200\.0 MiB", out)
    assert "#   ATTACHED       200.0 MiB  (+100.0)" in out


def test_each_phase_is_sampled_at_its_own_pace_and_the_sweep_for_twenty_seconds(rig):
    result = rig.run(SCRIPT, *SHORT)
    samples = re.findall(r"^# \[.*\]\s+sample\s+RSS", result.stdout, re.M)
    # LOAD every 5 s for 10 s, QUIESCE every 3 s for 6 s, DROP every 2 s for 10 s
    assert len(samples) == 2 + 2 + 5
    sweep = re.findall(
        r"post-USR1 sample\s+RSS =\s+[\d.]+ MiB  \(\+(\d+)s after signal\)", result.stdout
    )
    assert sweep == [str(n) for n in range(2, 22, 2)]


def test_a_phase_reports_its_first_and_its_last_sample(rig):
    rig.env["RIG_PS_STEP"] = "1024"  # one more MiB on every reading
    result = rig.run(SCRIPT, *SHORT)
    # LOAD: a start reading and two samples, so the end is two MiB past the start
    assert re.search(r"#   LOAD end\s+\d+\.\d MiB\s+\(\+2\.0\)", result.stdout)
    assert re.search(r"RSS = \s*[\d.]+ MiB  \(\+1\.0\)", result.stdout)


def test_only_the_backend_itself_is_counted_not_its_children(rig):
    rig.env["RIG_PS_BASE"] = "204800"  # the table also lists a 100 KiB child
    result = rig.run(SCRIPT, *SHORT)
    assert re.search(r"BOOT\s+RSS =   200\.0 MiB", result.stdout)


@pytest.mark.parametrize(
    ("net", "verdict"),
    [
        (-50, "HEALTHY"),
        (4, "HEALTHY"),
        (6, "OK"),
        (19, "OK"),
        (21, "WARN"),
    ],
)
def test_the_verdict_is_what_the_sweep_left_above_the_attached_baseline(rig, net, verdict):
    _phases(rig, net)
    result = rig.run(SCRIPT, *SHORT)
    assert result.returncode == 0, result.stderr
    assert f"# verdict: {verdict}" in result.stdout


def test_one_sigusr1_forces_the_eviction_sweep(rig):
    rig.run(SCRIPT, *SHORT)
    assert rig.usr1_count() == 1


def test_no_sessions_still_run_the_sweep(rig):
    result = rig.run(SCRIPT, "--sessions", "0", "--load-seconds", "5", "--quiesce-seconds", "3")
    assert result.returncode == 0, result.stderr
    assert rig.usr1_count() == 1
    assert [r for r in rig.rpcs() if r["method"] == "get_status"] == []


@pytest.mark.parametrize("rate", ["0", "-10"])
def test_a_rate_that_is_not_positive_does_not_crash(rig, rate):
    result = rig.run(SCRIPT, *SHORT, "--rate", rate)
    assert result.returncode == 0, result.stderr
    assert "ZeroDivisionError" not in result.stderr


def test_the_last_thirty_eviction_log_lines_are_shown_and_nothing_else(rig):
    lines = [f"2026 igni_core INFO session pool: evicted idle s-{n}" for n in range(35)]
    lines += [
        "unrelated line",
        "got SIGUSR1: sweeping",
        "Gc.Collect() freed 3",
        "an EVICTION notice",
    ]
    rig.env["RIG_STDERR"] = "\n".join(lines)
    result = rig.run(SCRIPT, *SHORT)
    shown = result.stdout.split("# BE-side eviction/GC log lines:")[1].splitlines()
    shown = [line for line in shown if line.startswith("#   ")]
    assert len(shown) == 30
    assert shown[-1] == "#   an EVICTION notice"
    assert "#   got SIGUSR1: sweeping" in shown
    assert not any("unrelated" in line for line in shown)
    assert shown[0].endswith("evicted idle s-8")  # 38 matched, the first eight are cut


def test_without_matching_log_lines_no_log_section_is_printed(rig):
    rig.env["RIG_STDERR"] = "just noise"
    result = rig.run(SCRIPT, *SHORT)
    assert "BE-side eviction/GC log lines" not in result.stdout


def test_a_backend_that_exits_before_it_is_ready_aborts_the_run(rig):
    rig.env["RIG_MODE"] = "die"
    result = rig.run(SCRIPT)
    assert result.returncode != 0
    assert "RuntimeError: BE exited before ready" in result.stderr


def test_a_backend_that_dies_before_ready_shows_the_tail_of_its_output(rig):
    rig.env["RIG_MODE"] = "die"
    rig.env["RIG_STDERR"] = "\n".join(f"line {n}" for n in range(30)) + "\nboom: no port"
    result = rig.run(SCRIPT)
    assert result.returncode != 0
    assert "RuntimeError: BE exited before ready" in result.stderr
    assert "boom: no port" in result.stderr and "line 29" in result.stderr
    assert "line 5\n" not in result.stderr  # only the tail


def test_a_chatty_backend_does_not_block_on_its_output(rig):
    rig.env["RIG_CHATTY_KB"] = "400"
    result = rig.run(SCRIPT, *SHORT)
    assert result.returncode == 0, result.stderr
    log = re.search(r"# Backend log: (\S+)", result.stdout).group(1)
    assert "chatter 399 " in open(log).read()


def test_without_ps_every_reading_is_zero_and_the_run_still_ends(rig):
    rig.system_dirs = []
    (rig.bin / "ps").unlink()
    result = rig.run(SCRIPT, *SHORT)
    assert result.returncode == 0, result.stderr
    assert "FileNotFoundError" not in result.stderr
    assert "RSS =     0.0 MiB" in result.stdout


def test_the_backend_is_stopped_when_the_run_ends(rig):
    result = rig.run(SCRIPT, *SHORT)
    assert result.returncode == 0, result.stderr
    assert not rig.backend_alive()


def test_the_command_line_defaults():
    spec = importlib.util.spec_from_file_location(
        "release_probe_defaults", REPO / "scripts" / SCRIPT
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    seen = {}

    async def drive(args):
        seen.update(vars(args))

    mod._drive = drive
    old = sys.argv
    sys.argv = [SCRIPT]
    try:
        assert mod.main() == 0
    finally:
        sys.argv = old
    assert seen == {
        "sessions": 4,
        "rate": 300.0,
        "load_seconds": 20.0,
        "quiesce_seconds": 15.0,
        "idle_timeout": 0,
    }
