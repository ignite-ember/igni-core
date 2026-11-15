"""The gates that guard a release: the onboarding smoke test, the
Makefile's documented one-command setup, and the release workflow's
version/packaging assertions.

Every test here pins a failure that shipped green once. The smoke-test
ones run the real step-6 block out of ``scripts/onboarding-smoke.sh``
against a stub interpreter rather than asserting on its source, so a
rewrite that keeps the behaviour keeps passing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_SH = REPO_ROOT / "scripts" / "onboarding-smoke.sh"
RELEASE_YML = REPO_ROOT / ".github" / "workflows" / "release.yml"
MAKEFILE = REPO_ROOT / "Makefile"
PYPROJECT = REPO_ROOT / "pyproject.toml"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


# ── onboarding-smoke.sh harness ─────────────────────────────────────


def _section(text: str, start: str, end: str | None) -> str:
    """Slice the script from the line containing ``start`` to the line
    containing ``end`` (inclusive), or to EOF when ``end`` is None."""
    lines = text.splitlines(keepends=True)
    first = next(i for i, line in enumerate(lines) if start in line)
    if end is None:
        return "".join(lines[first:])
    last = next(i for i, line in enumerate(lines) if i >= first and end in line)
    return "".join(lines[first : last + 1])


def _run_step6(
    tmp_path: Path, stub_body: str, *, keep: bool = False
) -> subprocess.CompletedProcess:
    """Run the script's cleanup handler + step 6 verbatim, with a stub
    standing in for the backend interpreter."""
    text = SMOKE_SH.read_text()
    cleanup_block = _section(text, 'PROJECT_DIR=""', "trap cleanup EXIT")
    step6_block = _section(text, "# ── Step 6", None)

    stub = tmp_path / "stub-python"
    stub.write_text(stub_body)
    stub.chmod(0o755)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    # Backend logs moved out of CACHE_DIR into their own LOG_DIR so a failing
    # run can keep them for the release workflow to upload. The harness stands
    # in for the script's preamble, so it has to supply the variable the
    # preamble sets — without it step 6 dies on `set -u` before it reaches the
    # behaviour under test.
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    # Step 6's ``mktemp -d`` is redirected here so the test can see
    # whether the project dir survived the run. TMPDIR is not enough:
    # BSD mktemp resolves ``-t`` against the system temp dir regardless.
    mktemp_dir = tmp_path / "mktemp"
    mktemp_dir.mkdir()

    harness = tmp_path / "harness.sh"
    harness.write_text(
        "set -euo pipefail\n"
        f"KEEP={'true' if keep else 'false'}\n"
        f'CACHE_DIR="{cache_dir}"\n'
        f'LOG_DIR="{log_dir}"\n'
        f'VENV_PYTHON="{stub}"\n'
        "mktemp() {\n"
        f'    local d="{mktemp_dir}/project-$$-$RANDOM"\n'
        '    command mkdir -p "$d"\n'
        '    echo "$d"\n'
        "}\n" + cleanup_block + step6_block
    )

    env = {**os.environ, "SMOKE_READY_TIMEOUT": "3"}
    proc = subprocess.run(
        ["bash", str(harness)], capture_output=True, text=True, env=env, timeout=120
    )
    proc.mktemp_dir = mktemp_dir  # type: ignore[attr-defined]
    proc.log_dir = log_dir  # type: ignore[attr-defined]
    return proc


NEVER_READY = '#!/usr/bin/env bash\necho "booting, no ready line"\nexec sleep 120\n'
READY = '#!/usr/bin/env bash\necho \'{"status": "ready", "ws_port": 54321}\'\nexec sleep 120\n'


def test_step6_fails_when_backend_never_signals_ready(tmp_path):
    """A backend that starts and then hangs must fail the smoke test.

    It used to pass: the poll loop's only exits were the ready branch
    and the process-died branch, so a live-but-silent backend fell
    straight through to "Onboarding smoke passed" and exit 0.
    """
    proc = _run_step6(tmp_path, NEVER_READY)

    assert proc.returncode != 0, f"hung backend reported success:\n{proc.stdout}"
    assert "never signalled ready" in proc.stderr
    assert "Onboarding smoke passed" not in proc.stdout
    # The failure has to be diagnosable — dump what the backend said.
    assert "booting, no ready line" in proc.stderr


def test_step6_passes_when_backend_signals_ready(tmp_path):
    proc = _run_step6(tmp_path, READY)

    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "backend ready on ws://127.0.0.1:54321" in proc.stdout
    assert "Onboarding smoke passed" in proc.stdout


def test_step6_leaves_no_temp_dir_behind(tmp_path):
    """Step 6's own ``trap … EXIT`` used to replace cleanup()'s, so the
    cache dir — uv, CPython, the venv, the HF model, the BE logs —
    leaked on every run that got this far."""
    proc = _run_step6(tmp_path, READY)

    assert list(proc.mktemp_dir.iterdir()) == []
    assert not (tmp_path / "cache").exists()


def test_keep_flag_still_reports_after_step_six(tmp_path):
    """``--keep`` was inert on any run that reached step 6."""
    proc = _run_step6(tmp_path, READY, keep=True)

    assert "Keeping cache at" in proc.stdout
    assert (tmp_path / "cache").exists()
    assert len(list(proc.mktemp_dir.iterdir())) == 1


def test_a_failing_run_keeps_the_backend_logs(tmp_path):
    """The failure artifact has to exist when the workflow goes to upload it.

    ``release-readiness.yml`` uploads backend logs on failure, and could never
    find one: the logs were written inside ``CACHE_DIR`` and the EXIT trap
    removes that on every exit, failures included. So the one run whose output
    anyone wanted was the one run that deleted it.
    """
    proc = _run_step6(tmp_path, NEVER_READY)

    assert proc.returncode != 0
    assert proc.log_dir.exists(), "the failing run removed the logs it was meant to keep"
    assert "booting, no ready line" in (proc.log_dir / "be-stdout.log").read_text()
    assert "Backend logs kept for diagnosis" in proc.stderr
    # The cache still goes — keeping logs is not an excuse to leak the
    # multi-hundred-MB cache dir alongside them.
    assert not (tmp_path / "cache").exists()


def test_a_passing_run_removes_the_backend_logs(tmp_path):
    proc = _run_step6(tmp_path, READY)

    assert proc.returncode == 0
    assert not proc.log_dir.exists()


# ── Makefile ────────────────────────────────────────────────────────


def _make_recipe(target: str) -> list[str]:
    lines = MAKEFILE.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{target}:"))
    body = []
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        body.append(line.strip())
    return body


def test_make_install_does_not_depend_on_pip_being_in_the_venv():
    """``uv venv``/``uv sync`` seed no pip, so ``.venv/bin/pip`` — and
    ``.venv/bin/python -m pip`` — exit 127 against this repo's own
    checked-in venv."""
    recipe = " ".join(_make_recipe("install"))

    assert "uv pip install" in recipe
    assert ".venv/bin/pip" not in recipe
    assert "$(VENV)/pip" not in recipe
    assert "-m pip" not in recipe


def test_make_install_requests_only_extras_that_exist():
    recipe = " ".join(_make_recipe("install"))
    declared = set(tomllib.loads(PYPROJECT.read_text())["project"]["optional-dependencies"])

    requested = re.search(r'\[([^\]]+)\]"', recipe)
    assert requested, f"no extras group found in: {recipe}"
    asked = {name.strip() for name in requested.group(1).split(",")}

    assert asked <= declared, f"undeclared extras: {sorted(asked - declared)}"


def test_coverage_plugin_is_installed_by_the_dev_extra():
    """``make test-cov`` passes ``--cov``; without pytest-cov pytest
    rejects the unrecognised argument and exits 4."""
    assert "--cov" in " ".join(_make_recipe("test-cov"))
    dev = tomllib.loads(PYPROJECT.read_text())["project"]["optional-dependencies"]["dev"]

    assert any(spec.startswith("pytest-cov") for spec in dev), dev


# ── release.yml ─────────────────────────────────────────────────────


def _release_steps(job: str) -> list[dict]:
    return yaml.safe_load(RELEASE_YML.read_text())["jobs"][job]["steps"]


def _brew_formula() -> str:
    text = RELEASE_YML.read_text()
    body = text.split("cat > Formula/igni-core.rb << 'FORMULA'\n", 1)[1]
    body = body.split("\n          FORMULA\n", 1)[0]
    return "\n".join(line.removeprefix(" " * 10) for line in body.splitlines())


def test_homebrew_formula_symlinks_every_console_script():
    """``igni`` is the command README and QUICKSTART tell brew users to
    run, and a formula once linked only another one."""
    formula = _brew_formula()
    scripts = tomllib.loads(PYPROJECT.read_text())["project"]["scripts"]

    for name in scripts:
        assert f'bin.install_symlink libexec/"bin/{name}"' in formula, name


def _tap_run() -> str:
    return next(
        step["run"]
        for step in _release_steps("update-homebrew")
        if step.get("name") == "Update Homebrew tap"
    )


def test_the_formula_is_igni_core_and_the_only_one():
    """The tap has one formula, ``igni-core``: the retired ``ignite-ember``
    name is neither a formula nor an alias, and the release writes and
    commits nothing else."""
    run = _tap_run()

    assert _brew_formula().startswith("class IgniCore < Formula\n")
    assert "git add Formula/igni-core.rb\n" in run
    assert "ignite-ember.rb" not in run
    assert "Aliases" not in run
    assert 'bin/ignite-ember"' not in _brew_formula()


def test_the_formula_pins_the_version_homebrew_reads_off_its_url():
    """Only the URL and the sha256 change per release: the version comes
    from the sdist's file name, and pip installs exactly that."""
    formula = _brew_formula()

    assert '"--prefer-binary", "igni-core==#{version}"' in formula
    assert 'url "PLACEHOLDER_URL"' in formula
    assert 'sha256 "PLACEHOLDER_SHA"' in formula
    assert 'shell_output("#{bin}/igni --version")' in formula
    run = _tap_run()
    assert 'sed -i "s|PLACEHOLDER_URL|${URL}|" Formula/igni-core.rb' in run
    assert 'sed -i "s|PLACEHOLDER_SHA|${SHA}|" Formula/igni-core.rb' in run
    assert 'URL="${{ steps.sdist.outputs.url }}"' in run
    assert 'SHA="${{ steps.sdist.outputs.sha256 }}"' in run


_HASHED = (
    "https://files.pythonhosted.org/packages/ea/24/"
    "8a44eb66f592359d85b64aa956f714dd044512bab0b221c527a851cc57a4/igni_core-1.0.4.tar.gz"
)
_SDIST_BYTES = b"the igni-core 1.0.4 sdist"
_SDIST_SHA = hashlib.sha256(_SDIST_BYTES).hexdigest()


def _pypi_json(sdist_url: str, sha: str) -> dict:
    """What ``https://pypi.org/pypi/igni-core/<version>/json`` answers:
    the wheel and the sdist under ``urls``."""
    return {
        "urls": [
            {
                "packagetype": "bdist_wheel",
                "url": _HASHED.replace(".tar.gz", "-py3-none-any.whl"),
                "digests": {"sha256": "0" * 64},
            },
            {"packagetype": "sdist", "url": sdist_url, "digests": {"sha256": sha}},
        ]
    }


def _run_the_sdist_step(tmp_path: Path, pypi: dict, served: bytes = _SDIST_BYTES):
    """Run the ``Find the sdist PyPI serves`` step as Actions would: a fake
    ``curl`` on PATH serves ``served`` for any URL, and ``sha256sum`` is
    stood in for where the host has none (macOS)."""
    step = next(
        s
        for s in _release_steps("update-homebrew")
        if s.get("name") == "Find the sdist PyPI serves"
    )
    script = step["run"].replace("${{ steps.version.outputs.pypi_version }}", "1.0.4")
    (tmp_path / "pypi.json").write_text(json.dumps(pypi))
    (tmp_path / "served").write_bytes(served)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(
        "#!/bin/sh\n"
        'while [ $# -gt 0 ]; do [ "$1" = -o ] && { shift; out="$1"; }; last="$1"; shift; done\n'
        'echo "$last" >> "$CURL_LOG"\n'
        'cp "$SERVED" "$out"\n'
    )
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    if shutil.which("sha256sum") is None:
        (bin_dir / "sha256sum").write_text(
            f"#!/bin/sh\nexec {sys.executable} -c "
            "'import hashlib,sys; p=sys.argv[1]; "
            'print(hashlib.sha256(open(p,"rb").read()).hexdigest(), p)\' "$1"\n'
        )
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    (tmp_path / "step.sh").write_text(script)
    output = tmp_path / "github_output"
    output.write_text("")
    env = dict(os.environ)
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{env['PATH']}",
        GITHUB_OUTPUT=str(output),
        SERVED=str(tmp_path / "served"),
        CURL_LOG=str(tmp_path / "curl.log"),
    )
    proc = subprocess.run(
        ["bash", str(tmp_path / "step.sh")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
    fetched = (
        (tmp_path / "curl.log").read_text().split() if (tmp_path / "curl.log").exists() else []
    )
    return proc, outputs, fetched


def test_the_formula_gets_the_hashed_sdist_url_and_digest_pypi_states(tmp_path):
    """Homebrew's style check refuses the ``/packages/source/`` short URL,
    so the formula takes the sdist's full URL from PyPI's JSON API, and
    the sha256 PyPI gives for it — checked against the bytes it serves."""
    proc, outputs, fetched = _run_the_sdist_step(tmp_path, _pypi_json(_HASHED, _SDIST_SHA))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert outputs == {"url": _HASHED, "sha256": _SDIST_SHA}
    assert fetched == [_HASHED]


def test_an_sdist_that_does_not_match_its_stated_digest_stops_the_tap_update(tmp_path):
    proc, outputs, _ = _run_the_sdist_step(
        tmp_path, _pypi_json(_HASHED, _SDIST_SHA), served=b"something else"
    )

    assert proc.returncode != 0
    assert "PyPI says" in proc.stdout + proc.stderr
    assert outputs == {}


def test_an_sdist_url_of_another_version_stops_the_tap_update(tmp_path):
    """Homebrew reads the version off the URL and the install line pins
    ``igni-core==#{version}``: a URL of another version would install it."""
    other = _HASHED.replace("1.0.4", "1.0.3")
    proc, outputs, fetched = _run_the_sdist_step(tmp_path, _pypi_json(other, _SDIST_SHA))

    assert proc.returncode != 0
    assert "is not igni_core-1.0.4.tar.gz" in proc.stdout + proc.stderr
    assert outputs == {}
    assert fetched == []


def test_homebrew_formula_test_exercises_the_canonical_command():
    """The formula's own ``brew test`` is the only thing that would
    catch a dropped symlink — so it has to run ``igni``."""
    assert 'shell_output("#{bin}/igni --version")' in _brew_formula()


def _version_gate_run() -> str:
    for step in _release_steps("publish-pypi"):
        if "run" in step and "pyproject.toml" in step["run"] and "GITHUB_REF" in step["run"]:
            return step["run"]
    pytest.fail("publish-pypi has no step reconciling the git tag with pyproject.toml")


@pytest.mark.parametrize(
    ("tag", "pyproject_version", "should_pass"),
    [
        ("refs/tags/v1.0.3", "1.0.3", True),
        ("refs/tags/v1.0.4", "1.0.3", False),
        ("refs/tags/v1.0.3", "1.0.30", False),
    ],
)
def test_release_refuses_a_tag_that_disagrees_with_pyproject(
    tmp_path, tag, pyproject_version, should_pass
):
    """Tagging v1.0.4 on a tree still at 1.0.3 shipped latest.json
    advertising 1.0.4 over a bundle reporting 1.0.3 — an update the
    desktop app re-offers on every launch, forever."""
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "igni-core"\nversion = "{pyproject_version}"\n'
    )
    script = tmp_path / "gate.sh"
    script.write_text(_version_gate_run())

    proc = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "GITHUB_REF": tag},
        timeout=60,
    )

    if should_pass:
        assert proc.returncode == 0, proc.stdout + proc.stderr
    else:
        assert proc.returncode != 0, f"drift accepted:\n{proc.stdout}{proc.stderr}"
