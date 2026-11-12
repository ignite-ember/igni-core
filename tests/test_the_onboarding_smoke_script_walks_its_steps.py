"""``scripts/onboarding-smoke.sh`` run whole, against stubs for the network and the interpreter.

``test_release_packaging_gates`` runs the script's step-6 block on its own.
These run the real script from its first line: its arguments, the version
it targets, the platforms it accepts, the package it installs, the exit
code 77 for a release PyPI does not have yet, the log directory it
reports to CI, and how it ends the backend it started.

``curl``, ``uname`` and ``uv`` are stand-ins on ``PATH``; the venv's
``python`` is a stub that records how it was called and plays the backend.
Nothing is downloaded or installed.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import signal
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_SH = REPO_ROOT / "scripts" / "onboarding-smoke.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")

CURL = """#!/usr/bin/env bash
out=""; url=""
while [[ $# -gt 0 ]]; do
    case "$1" in -o) out="$2"; shift 2 ;; -*) shift ;; *) url="$1"; shift ;; esac
done
echo "curl $url" >> "$CALLS"
case "$url" in
    *github.com*) cp "$UV_TARBALL" "$out" ;;
    *pypi.org*) [[ "$PYPI_HAS_IT" == "yes" ]] || exit 22 ;;
esac
"""

UNAME = """#!/usr/bin/env bash
case "$1" in -s) echo "$FAKE_OS" ;; -m) echo "$FAKE_ARCH" ;; esac
"""

UV = """#!/usr/bin/env bash
echo "uv $*" >> "$CALLS"
if [[ "$1" == "venv" ]]; then
    mkdir -p "$4/bin"
    cp "$PY_STUB" "$4/bin/python"
    chmod +x "$4/bin/python"
fi
"""

# The venv's python: records its calls, then plays the backend per BACKEND.
PYTHON = """#!/usr/bin/env bash
echo "py $*" >> "$CALLS"
if [[ "$1" == "-c" ]]; then echo "  igni_core v0.0.0"; exit 0; fi
if [[ "$2" == "igni_core.prefetch_models" ]]; then echo "hf=$HF_HOME" >> "$CALLS"; exit 0; fi
echo $$ > "$BACKEND_PID"
case "$BACKEND" in
    ready) echo '{"status": "ready", "ws_port": 4242}'; exec sleep 120 ;;
    ignores-term)
        trap '' TERM
        echo '{"status": "ready", "ws_port": 4242}'
        while :; do sleep 0.2; done ;;
    dies-after-ready) echo '{"status": "ready", "ws_port": 4242}'; exit 0 ;;
    exits-early) echo "boot failed on stdout"; echo "boom on stderr" >&2; exit 3 ;;
esac
"""


class Run:
    def __init__(self, proc, tmp: Path):
        self.proc, self.tmp = proc, tmp
        self.code = proc.returncode
        self.out, self.err = proc.stdout, proc.stderr
        self.calls = (tmp / "calls").read_text().splitlines() if (tmp / "calls").exists() else []
        self.log_dir = tmp / "logs"

    def cache_dir(self) -> Path:
        return Path(re.search(r"Using temp cache: (.+)", self.out).group(1))


@pytest.fixture
def smoke(tmp_path):
    """Run a copy of the script, beside a ``pyproject.toml`` of the test's choosing."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(SMOKE_SH, repo / "scripts" / "onboarding-smoke.sh")
    (repo / "pyproject.toml").write_text('[project]\nname = "igni-core"\nversion = "9.8.7"\n')

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("curl", CURL), ("uname", UNAME), ("uv", UV)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    (tmp_path / "python-stub").write_text(PYTHON)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = (bin_dir / "uv").read_bytes()
        info = tarfile.TarInfo("uv-x86_64-unknown-linux-gnu/uv")
        info.size, info.mode = len(data), 0o755
        tar.addfile(info, io.BytesIO(data))
    (tmp_path / "uv.tar.gz").write_bytes(buf.getvalue())
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()

    def run(*args, backend="ready", pypi=True, os_name="Linux", arch="x86_64", **extra_env):
        # The inherited environment with the stand-ins first on its PATH.
        inherited = dict(os.environ)
        env = {
            **inherited,
            "PATH": f"{bin_dir}{os.pathsep}{inherited['PATH']}",
            "TMPDIR": str(tmpdir),
            "CALLS": str(tmp_path / "calls"),
            "UV_TARBALL": str(tmp_path / "uv.tar.gz"),
            "PY_STUB": str(tmp_path / "python-stub"),
            "BACKEND": backend,
            "BACKEND_PID": str(tmp_path / "backend.pid"),
            "PYPI_HAS_IT": "yes" if pypi else "no",
            "FAKE_OS": os_name,
            "FAKE_ARCH": arch,
            "SMOKE_LOG_DIR": str(tmp_path / "logs"),
            "SMOKE_READY_TIMEOUT": "4",
            **{k: str(v) for k, v in extra_env.items()},
        }
        env.pop("GITHUB_OUTPUT", None) if "GITHUB_OUTPUT" not in extra_env else None
        proc = subprocess.run(
            ["bash", str(repo / "scripts" / "onboarding-smoke.sh"), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=90,
        )
        return Run(proc, tmp_path)

    run.repo = repo
    return run


class TestArguments:
    def test_help_prints_the_header_and_exits_zero(self, smoke):
        r = smoke("--help")

        assert r.code == 0
        assert "Onboarding smoke test" in r.out
        assert "--local" in r.out and "SMOKE_READY_TIMEOUT" in r.out
        assert r.calls == []

    def test_short_help_is_the_same(self, smoke):
        assert smoke("-h").out == smoke("--help").out

    def test_an_unknown_argument_exits_two_and_names_it(self, smoke):
        r = smoke("--frobnicate")

        assert r.code == 2
        assert "unknown arg: --frobnicate" in r.err
        assert r.calls == []

    def test_the_version_comes_from_pyproject_toml(self, smoke):
        r = smoke()

        assert "Targeting igni-core version: 9.8.7 (local source: false)" in r.out
        assert "curl https://pypi.org/pypi/igni-core/9.8.7/json" in r.calls

    def test_an_explicit_version_wins(self, smoke):
        r = smoke("--version", "1.2.3")

        assert "Targeting igni-core version: 1.2.3" in r.out
        assert (
            "uv pip install --python "
            + str(r.cache_dir() / "venv/bin/python")
            + " igni-core[decisions]==1.2.3"
            in r.calls
        )


class TestPlatforms:
    @pytest.mark.parametrize(
        "os_name, arch, triple",
        [
            ("Darwin", "arm64", "aarch64-apple-darwin"),
            ("Linux", "x86_64", "x86_64-unknown-linux-gnu"),
            ("Linux", "aarch64", "aarch64-unknown-linux-gnu"),
        ],
    )
    def test_uv_is_fetched_for_the_supported_platforms(self, smoke, os_name, arch, triple):
        r = smoke(os_name=os_name, arch=arch)

        assert r.code == 0, r.err
        assert (
            f"curl https://github.com/astral-sh/uv/releases/download/0.5.7/uv-{triple}.tar.gz"
            in r.calls
        )

    @pytest.mark.parametrize(
        "os_name, arch", [("Darwin", "x86_64"), ("Windows_NT", "x86_64"), ("Linux", "riscv64")]
    )
    def test_any_other_platform_exits_one_before_downloading_anything(self, smoke, os_name, arch):
        r = smoke(os_name=os_name, arch=arch)

        assert r.code == 1
        assert f"Unsupported platform: {os_name} {arch}" in r.err
        assert r.calls == []


class TestInstall:
    def test_pypi_install_is_igni_core_with_the_decisions_extra_at_the_version(self, smoke):
        r = smoke("--version", "0.3.0")

        venv_python = r.cache_dir() / "venv" / "bin" / "python"
        assert f"uv pip install --python {venv_python} igni-core[decisions]==0.3.0" in r.calls
        assert not any("ignite" in c for c in r.calls)

    def test_local_install_is_editable_from_the_tree_and_never_asks_pypi(self, smoke):
        r = smoke("--local")

        assert r.code == 0, r.err
        installs = [c for c in r.calls if c.startswith("uv pip install")]
        assert len(installs) == 1
        assert installs[0].endswith(f" -e {smoke.repo.resolve()}[decisions]")
        assert not any("pypi.org" in c for c in r.calls)

    def test_a_version_pypi_does_not_have_is_skipped_with_exit_77(self, smoke):
        r = smoke(pypi=False)

        assert r.code == 77
        assert "igni-core==9.8.7 is not on PyPI" in r.err
        assert "exiting 77: skipped, not failed" in r.err
        assert not any(c.startswith("uv pip") for c in r.calls)
        assert not any(c.startswith("py ") for c in r.calls)

    def test_a_skip_is_not_reported_as_a_failure_and_sweeps_the_logs(self, smoke):
        r = smoke(pypi=False)

        assert "Backend logs kept for diagnosis" not in r.err
        assert not r.log_dir.exists()
        assert not r.cache_dir().exists()

    def test_the_interpreter_imports_and_launches_igni_core(self, smoke):
        r = smoke()

        assert any(c.startswith("py -c import igni_core;") for c in r.calls)
        assert "  igni_core v0.0.0" in r.out
        assert any(c.endswith("-m igni_core.prefetch_models") for c in r.calls)
        launch = next(c for c in r.calls if "-m igni_core.backend" in c)
        assert " --ws-port 0 --project-dir " in launch
        assert not any("ember" in c for c in r.calls)

    def test_the_model_is_prefetched_into_a_private_hf_home(self, smoke):
        r = smoke()

        assert f"hf={r.cache_dir()}/hf" in r.calls


class TestTheLogDirectory:
    def test_it_is_reported_to_ci_before_anything_can_fail(self, smoke, tmp_path):
        gh = tmp_path / "github-output"

        r = smoke(os_name="Windows_NT", GITHUB_OUTPUT=gh)

        assert r.code == 1
        assert gh.read_text() == f"log-dir={tmp_path / 'logs'}\n"

    def test_without_github_output_nothing_is_reported_and_nothing_breaks(self, smoke):
        r = smoke()

        assert r.code == 0, r.err

    def test_logs_of_an_earlier_run_are_cleared_first_and_other_files_are_left(
        self, smoke, tmp_path
    ):
        (tmp_path / "logs").mkdir()
        stale = tmp_path / "logs" / "be-earlier-run.log"
        stale.write_text("from a run that failed last week\n")
        other = tmp_path / "logs" / "notes.txt"
        other.write_text("mine")

        r = smoke(backend="exits-early")

        assert r.code == 1
        assert not stale.exists()
        assert other.read_text() == "mine"
        assert (r.log_dir / "be-stdout.log").read_text() == "boot failed on stdout\n"

    def test_the_default_is_a_dot_smoke_logs_beside_the_scripts_repo(self, smoke, tmp_path):
        r = smoke(SMOKE_LOG_DIR="")
        # An empty override falls back to the default, which a clean pass then sweeps.

        assert r.code == 0, r.err
        assert f"Backend logs: {smoke.repo.resolve()}/.smoke-logs" in r.out


class TestTheBackend:
    def test_a_ready_backend_passes_and_reports_its_port(self, smoke):
        r = smoke()

        assert r.code == 0, r.err
        assert "backend ready on ws://127.0.0.1:4242" in r.out
        assert "Onboarding smoke passed" in r.out

    def test_a_backend_that_exits_before_ready_fails_with_both_streams(self, smoke):
        r = smoke(backend="exits-early")

        assert r.code == 1
        assert "backend exited before signalling ready" in r.err
        assert "boot failed on stdout" in r.err and "boom on stderr" in r.err
        assert "Backend logs kept for diagnosis" in r.err
        assert (r.log_dir / "be-stdout.log").exists()

    def test_a_backend_that_dies_right_after_ready_fails(self, smoke):
        r = smoke(backend="dies-after-ready")

        assert r.code == 1
        assert "backend died after ready signal" in r.err
        assert "Onboarding smoke passed" not in r.out
        assert r.log_dir.exists()

    def test_a_backend_that_stops_on_sigterm_is_gone_when_the_script_ends(self, smoke, tmp_path):
        started = time.monotonic()

        r = smoke()

        assert r.code == 0, r.err
        assert time.monotonic() - started < 15
        assert not _alive(tmp_path / "backend.pid")

    def test_a_backend_that_ignores_sigterm_is_killed_after_five_seconds(self, smoke, tmp_path):
        started = time.monotonic()

        r = smoke(backend="ignores-term")

        assert r.code == 0, r.err
        assert time.monotonic() - started >= 5
        assert not _alive(tmp_path / "backend.pid")


def _alive(pid_file: Path) -> bool:
    pid = int(pid_file.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    return True
