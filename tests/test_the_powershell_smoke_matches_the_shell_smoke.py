"""``scripts/onboarding-smoke.ps1`` is the Windows counterpart of ``onboarding-smoke.sh``.

PowerShell is not on the machines that run this suite, so the script
cannot be run here; what is pinned is that the two scripts still agree on
every decision the shell script's tests pin by running it: the package
and extra installed, the module imported and launched, the uv release, the
exit code for an unpublished version, the log directory and the file
names in it, and the failing branches of step 6. When ``pwsh`` is
present the script is also parsed, so a syntax error cannot ship.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
PS1 = (_SCRIPTS / "onboarding-smoke.ps1").read_text()
SH = (_SCRIPTS / "onboarding-smoke.sh").read_text()


def _assigned(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.MULTILINE)
    assert match, pattern
    return match.group(1)


def test_both_install_the_same_package_with_the_same_extra():
    assert (
        _assigned(PS1, r'^\$Package = "([^"]+)"')
        == _assigned(SH, r'^PACKAGE="([^"]+)"')
        == "igni-core"
    )
    assert (
        _assigned(PS1, r'^\$Extras = "([^"]+)"')
        == _assigned(SH, r'^EXTRAS="([^"]+)"')
        == "decisions"
    )


def test_both_install_that_package_in_both_modes_from_the_same_places():
    assert '-e "$($RepoRoot)[$Extras]"' in PS1
    assert '"$($Package)[$Extras]==$Version"' in PS1
    assert "https://pypi.org/pypi/$Package/$Version/json" in PS1


def test_both_import_and_launch_igni_core_and_nothing_else():
    for text in (PS1, SH):
        assert "import igni_core;" in text
        assert "-m igni_core.prefetch_models" in text or '"igni_core.prefetch_models"' in text
        assert "igni_core.backend" in text
        assert "ember" not in text.lower()
    assert '"-m", "igni_core.backend", "--ws-port", "0", "--project-dir"' in PS1


def test_both_fetch_the_same_uv_release_and_python():
    assert _assigned(PS1, r'^\$UvVersion = "([^"]+)"') == _assigned(SH, r'^UV_VERSION="([^"]+)"')
    assert "python install 3.12" in PS1 and "python install 3.12" in SH
    assert "x86_64-pc-windows-msvc" in PS1


def test_an_unpublished_version_exits_77_as_a_success_so_the_logs_are_swept():
    skip = PS1[PS1.index("is not on PyPI") :]
    skip = skip[: skip.index("exit 77") + len("exit 77")]

    assert "$script:Succeeded = $true" in skip
    assert "exiting 77: skipped, not failed" in PS1


def test_both_keep_logs_outside_the_cache_under_the_same_names_and_report_the_dir():
    for text in (PS1, SH):
        assert "SMOKE_LOG_DIR" in text and ".smoke-logs" in text
        assert "be-stdout.log" in text and "be-stderr.log" in text
        assert "log-dir=" in text and "GITHUB_OUTPUT" in text
    assert 'Filter "be-*.log"' in PS1


def test_both_read_the_ready_timeout_from_the_same_variable_with_the_same_default():
    assert "${SMOKE_READY_TIMEOUT:-60}" in SH
    assert "if ($env:SMOKE_READY_TIMEOUT) { [int]$env:SMOKE_READY_TIMEOUT } else { 60 }" in PS1
    assert "$i -lt $ReadyTimeout" in PS1
    assert "$i -lt 60" not in PS1


def test_step_six_fails_on_an_early_exit_and_on_never_ready_and_stops_the_backend():
    assert 'throw "backend exited early"' in PS1
    assert 'throw "backend never reported ready within ${ReadyTimeout}s"' in PS1
    assert "Stop-Process -Id $BeProc.Id -Force" in PS1
    assert PS1.index("never reported ready") < PS1.index("OK Onboarding smoke passed")


def test_cleanup_runs_from_the_trap_and_the_finally_and_keeps_logs_after_a_failure():
    assert "trap { Cleanup; break }" in PS1
    assert re.search(r"\} finally \{\s*Cleanup\s*\}", PS1)
    body = PS1[PS1.index("function Cleanup") : PS1.index("trap { Cleanup")]
    assert "if (-not $script:Succeeded)" in body
    assert "Backend logs kept for diagnosis" in body
    assert PS1.rstrip().endswith("}")
    assert PS1.index("$script:Succeeded = $true\n\n} finally") > PS1.index(
        "OK Onboarding smoke passed"
    )


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh required")
def test_the_script_parses():
    proc = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-Command",
            f"$e=$null; [System.Management.Automation.Language.Parser]::ParseFile('{_SCRIPTS / 'onboarding-smoke.ps1'}', [ref]$null, [ref]$e); "
            "if ($e) { $e | ForEach-Object { $_.Message }; exit 1 }",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
