"""The edges of the private Python environments that the install path does not reach.

``test_a_python_extension_installs_offline`` drives the happy and the
refused installs. Here: what a development install's digest counts,
what a failed command says, a marker that cannot be trusted, and the
command runner itself on a real process.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from igni_core.core.paths import CONFIG_DIR
from igni_core.extensions import python_env
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.python_env import (
    OUTPUT_TAIL,
    READY_MARKER,
    CommandOutcome,
    CommandRunner,
    ContentDigest,
    PythonEnvironmentError,
    PythonEnvironments,
)
from tests.extension_distribution_helpers import manifest
from tests.host_model_helpers import manifest as dev_command_manifest
from tests.test_a_python_extension_installs_offline import Pretending, bundle_with_wheels, load


def source_tree(tmp_path: Path) -> Path:
    bundle = tmp_path / "source"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps(manifest()))
    (bundle / "pyproject.toml").write_text("[project]\nname='acme-notes'\n")
    return bundle


# ── What a development install is made of ────────────────────────────


def test_the_digest_follows_each_file_s_bytes_and_path(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.py").write_text("one")
    first = ContentDigest.of(root)

    assert ContentDigest.of(root) == first

    (root / "a.py").write_text("two")
    changed = ContentDigest.of(root)
    assert changed != first

    (root / "a.py").rename(root / "b.py")
    assert ContentDigest.of(root) not in {first, changed}


def test_the_digest_does_not_depend_on_the_order_files_were_made(tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    for root, names in ((one, ["a", "b", "c"]), (two, ["c", "a", "b"])):
        (root / "sub").mkdir(parents=True)
        for name in names:
            (root / "sub" / name).write_text(name)

    assert ContentDigest.of(one) == ContentDigest.of(two)


@pytest.mark.parametrize(
    "ignored",
    [
        ".git/HEAD",
        ".hg/store",
        "pkg/__pycache__/m.cpython-312.pyc",
        ".venv/bin/python",
        "venv/bin/python",
        "node_modules/x/index.js",
        ".mypy_cache/x",
        ".pytest_cache/x",
        ".ruff_cache/x",
        "build/lib/m.py",
        "dist/m.whl",
        "acme_notes.egg-info/PKG-INFO",
        f"{CONFIG_DIR}/state.json",
        "pkg/module.pyc",
    ],
)
def test_what_a_checkout_accumulates_is_not_part_of_the_digest(
    tmp_path: Path, ignored: str
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "m.py").write_text("code")
    before = ContentDigest.of(root)

    target = root / ignored
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("noise")

    assert ContentDigest.of(root) == before


def test_a_file_that_cannot_be_read_does_not_stop_the_digest(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "m.py").write_text("code")
    (root / "dangling").symlink_to(root / "nowhere")

    assert len(ContentDigest.of(root)) == 64


# ── A failed command says why ────────────────────────────────────────


class Failing(Pretending):
    """Fails the step whose argv holds ``at``, with ``output``."""

    def __init__(self, at: str, output: str = "", error: OSError | None = None) -> None:
        super().__init__()
        self.at = at
        self.output = output
        self.error = error

    async def run(self, argv, *, env=None, cwd=None) -> CommandOutcome:
        if self.at in argv:
            if self.error is not None:
                raise self.error
            return CommandOutcome(returncode=1, output=self.output)
        return await super().run(argv, env=env, cwd=cwd)


async def test_a_command_that_cannot_start_is_a_clear_refusal_not_a_raw_oserror(tmp_path) -> None:
    bundle = bundle_with_wheels(tmp_path)
    runner = Failing("venv", error=FileNotFoundError("no such interpreter"))

    with pytest.raises(PythonEnvironmentError) as caught:
        await PythonEnvironments(tmp_path / "envs", runner=runner).ensure(
            load(bundle), bundle, development=False
        )

    assert str(caught.value) == "creating acme.notes's environment failed: no such interpreter"


async def test_a_failed_environment_step_quotes_the_command_output(tmp_path) -> None:
    bundle = bundle_with_wheels(tmp_path)
    runner = Failing("venv", output="ensurepip is missing")

    with pytest.raises(PythonEnvironmentError) as caught:
        await PythonEnvironments(tmp_path / "envs", runner=runner).ensure(
            load(bundle), bundle, development=False
        )

    assert str(caught.value) == "creating acme.notes's environment failed:\nensurepip is missing"


async def test_only_the_tail_of_a_long_output_is_quoted(tmp_path) -> None:
    bundle = bundle_with_wheels(tmp_path)
    runner = Failing("install", output="HEAD" + "x" * (OUTPUT_TAIL + 500) + "THE-END")

    with pytest.raises(PythonEnvironmentError) as caught:
        await PythonEnvironments(tmp_path / "envs", runner=runner).ensure(
            load(bundle), bundle, development=False
        )

    quoted = str(caught.value).split(":\n", 1)[1]
    assert len(quoted) == OUTPUT_TAIL
    assert quoted.endswith("THE-END") and "HEAD" not in quoted


async def test_a_failed_source_build_is_quoted_and_leaves_no_marker(tmp_path) -> None:
    bundle = source_tree(tmp_path)
    runner = Failing("wheel", output="no build backend")
    environments = PythonEnvironments(tmp_path / "envs", runner=runner)

    with pytest.raises(
        PythonEnvironmentError, match=r"(?s)building acme.notes from .*no build backend"
    ):
        await environments.ensure(load(bundle), bundle, development=True)

    assert not (environments.path_for(load(bundle)) / READY_MARKER).exists()


async def test_a_manifest_that_is_not_python_packaged_is_refused(tmp_path) -> None:
    other: ExtensionManifest = dev_command_manifest("acme.reviewer")

    with pytest.raises(PythonEnvironmentError, match="acme.reviewer is not packaged as 'python'"):
        await PythonEnvironments(tmp_path / "envs", runner=Pretending()).ensure(
            other, tmp_path, development=True
        )


# ── What is trusted as finished ──────────────────────────────────────


async def test_a_marker_that_cannot_be_read_is_rebuilt_not_trusted(tmp_path) -> None:
    bundle = bundle_with_wheels(tmp_path)
    runner = Pretending()
    environments = PythonEnvironments(tmp_path / "envs", runner=runner)
    manifest_ = load(bundle)
    await environments.ensure(manifest_, bundle, development=False)
    runner.calls.clear()

    (environments.path_for(manifest_) / READY_MARKER).write_text("{ not json")
    await environments.ensure(manifest_, bundle, development=False)

    assert runner.calls, "a marker that does not read means the environment is built again"


async def test_a_marker_in_the_wrong_shape_is_rebuilt_not_trusted(tmp_path) -> None:
    bundle = bundle_with_wheels(tmp_path)
    runner = Pretending()
    environments = PythonEnvironments(tmp_path / "envs", runner=runner)
    manifest_ = load(bundle)
    await environments.ensure(manifest_, bundle, development=False)
    runner.calls.clear()

    (environments.path_for(manifest_) / READY_MARKER).write_bytes(b"\xff\xfe")
    await environments.ensure(manifest_, bundle, development=False)

    assert runner.calls


async def test_an_environment_whose_entry_script_was_deleted_is_rebuilt(tmp_path) -> None:
    bundle = bundle_with_wheels(tmp_path)
    runner = Pretending()
    environments = PythonEnvironments(tmp_path / "envs", runner=runner)
    manifest_ = load(bundle)
    entry = await environments.ensure(manifest_, bundle, development=False)
    runner.calls.clear()

    entry.unlink()
    await environments.ensure(manifest_, bundle, development=False)

    assert runner.calls and entry.exists()


async def test_a_bundle_installed_both_ways_is_rebuilt_when_its_kind_changes(tmp_path) -> None:
    """A distributed marker has no digest; a development install must not reuse it."""
    bundle = bundle_with_wheels(tmp_path)
    runner = Pretending()
    environments = PythonEnvironments(tmp_path / "envs", runner=runner)
    manifest_ = load(bundle)
    await environments.ensure(manifest_, bundle, development=False)
    runner.calls.clear()

    await environments.ensure(manifest_, bundle, development=True)

    assert runner.calls


async def test_an_empty_wheelhouse_is_no_wheelhouse(tmp_path) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "wheels").mkdir(parents=True)
    (bundle / "manifest.json").write_text(json.dumps(manifest()))
    environments = PythonEnvironments(tmp_path / "envs", runner=Pretending())

    with pytest.raises(PythonEnvironmentError, match="has no wheelhouse at 'wheels'"):
        await environments.ensure(load(bundle), bundle, development=False)

    runner = Pretending()
    await PythonEnvironments(tmp_path / "envs", runner=runner).ensure(
        load(bundle), bundle, development=True
    )
    assert any(call[1:4] == ["-m", "pip", "wheel"] for call, _ in runner.calls)


async def test_an_environment_is_made_for_a_first_install_with_nothing_to_prune(tmp_path) -> None:
    bundle = bundle_with_wheels(tmp_path)

    entry = await PythonEnvironments(tmp_path / "new" / "root", runner=Pretending()).ensure(
        load(bundle), bundle, development=False
    )

    assert entry.exists()


# ── The runner, on a real process ────────────────────────────────────


async def test_the_runner_merges_stderr_and_returns_the_exit_code() -> None:
    outcome = await CommandRunner().run(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)",
        ]
    )

    assert outcome.returncode == 3
    assert sorted(outcome.output.split()) == ["err", "out"]


async def test_the_runner_decodes_what_is_not_utf8_with_a_replacement() -> None:
    outcome = await CommandRunner().run(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'ok \\xff done')"]
    )

    assert outcome.returncode == 0
    assert outcome.output == "ok � done"


async def test_the_runner_passes_the_environment_and_the_directory(tmp_path: Path) -> None:
    outcome = await CommandRunner().run(
        [sys.executable, "-c", "import os; print(os.environ['IGNI_PROBE'], os.getcwd())"],
        env={"IGNI_PROBE": "seen"},
        cwd=tmp_path,
    )

    assert outcome.output.split()[0] == "seen"
    assert Path(outcome.output.split(maxsplit=1)[1].strip()).resolve() == tmp_path.resolve()


# ── Where the console script is ──────────────────────────────────────


def test_a_console_script_is_in_bin_here_and_in_scripts_with_an_exe_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = tmp_path / "env"
    monkeypatch.setattr(python_env, "os", SimpleNamespace(name="posix"))
    assert PythonEnvironments.executable(environment, "acme") == environment / "bin" / "acme"

    monkeypatch.setattr(python_env, "os", SimpleNamespace(name="nt"))
    assert (
        PythonEnvironments.executable(environment, "acme") == environment / "Scripts" / "acme.exe"
    )
