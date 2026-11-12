"""``scripts/measure_resume.py`` starts the igni backend the way a warm base starts.

``Igni.start`` runs ``python -m igni_core.backend`` in the cloud profile
with its own run directory, home and project, and without the caller's
``IGNI_*`` variables. ``hand_over`` binds it as the node agent does, and
``wait_ready`` / ``stop`` bound how long the probe waits.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "measure_resume.py"


def _load():
    spec = importlib.util.spec_from_file_location("measure_resume_igni", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Child:
    """The process the probe launches: records how it was asked to run."""

    def __init__(self, args, **kwargs):
        self.args, self.env = args, kwargs["env"]
        self.returncode = None
        self.terminated = self.killed = False
        self.ignores_term = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout):
        if self.ignores_term and not self.killed:
            raise subprocess.TimeoutExpired("igni", timeout)
        return 0


@pytest.fixture
def launched(monkeypatch):
    mod = _load()
    made: list[_Child] = []

    real_popen = subprocess.Popen

    def popen(args, **kwargs):
        if args[0] == "git":  # a clone is a real git run; only the backend is stood in for
            return real_popen(args, **kwargs)
        made.append(_Child(args, **kwargs))
        return made[-1]

    monkeypatch.setattr(mod.subprocess, "Popen", popen)
    return mod, made


def _fake(mod, tmp_path):
    return mod.ModelChoice(fake_url="http://127.0.0.1:1/v1")


def test_the_backend_module_is_igni_cores_in_the_cloud_profile(launched, monkeypatch):
    mod, made = launched
    monkeypatch.setenv("IGNI_API_KEY", "secret")
    monkeypatch.setenv("IGNI_RESUME_PAGES", "1")
    monkeypatch.setenv("UNRELATED", "kept")
    igni = mod.Igni(database_url=None, port=18999, env={"MALLOC_ARENA_MAX": "2"})
    igni.start(_fake(mod, None))
    child = made[0]
    assert child.args[:3] == [sys.executable, "-m", "igni_core.backend"]
    # ... and that module is runnable, so the probe starts a real igni.
    assert importlib.util.find_spec("igni_core.backend.__main__") is not None
    assert child.args[3:] == ["--ws-port", "18999", "--project-dir", str(igni.project)]
    assert child.env["IGNI_PROFILE"] == "cloud"
    assert child.env["IGNI_RUN_DIR"] == str(igni.run_dir)
    assert child.env["HOME"] == str(igni.tmp / "home")
    assert child.env["MALLOC_ARENA_MAX"] == "2"
    assert child.env["UNRELATED"] == "kept"
    # Inherited IGNI_* is dropped, except the one the probe passes on.
    assert "IGNI_API_KEY" not in child.env
    assert child.env["IGNI_RESUME_PAGES"] == "1"


def test_the_managed_settings_are_written_for_the_run(launched):
    mod, _ = launched
    igni = mod.Igni(database_url=None, port=1)
    igni.start(_fake(mod, None))
    written = json.loads((igni.run_dir / "managed-settings.yaml").read_text())
    assert written["models"]["registry"]["probe"]["url"] == "http://127.0.0.1:1/v1"


def test_without_a_database_url_the_shared_database_is_sqlite_in_the_run_dir(launched):
    mod, _ = launched
    assert mod.Igni(database_url=None, port=1).database_url.startswith("sqlite:///")
    assert mod.Igni(database_url="postgresql://x/y", port=1).database_url == "postgresql://x/y"


def test_a_repository_url_is_cloned_as_the_project(launched, monkeypatch, tmp_path):
    mod, _ = launched
    source = tmp_path / "src"
    source.mkdir()
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
    ):
        subprocess.run(cmd, cwd=source, check=True)
    (source / "hello.txt").write_text("hi")
    subprocess.run(["git", "add", "hello.txt"], cwd=source, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "f"],
        cwd=source,
        check=True,
    )
    igni = mod.Igni(database_url=None, port=1)
    igni.project.rmdir()
    igni.start(mod.ModelChoice(fake_url="x", repo_url=f"file://{source}"))
    assert (igni.project / "hello.txt").read_text() == "hi"


def test_hand_over_writes_the_token_and_publishes_producer_env_whole(launched):
    mod, _ = launched
    igni = mod.Igni(database_url="postgresql://db/x", port=1)
    igni.hand_over()
    header, claims, signature = (igni.run_dir / "token").read_text().split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    assert json.loads(base64.urlsafe_b64decode(pad(header))) == {}
    decoded = json.loads(base64.urlsafe_b64decode(pad(claims)))
    assert decoded["sub"] == "u-ada" and decoded["org"] == "org-acme"
    assert decoded["exp"] > 0 and signature == "sig"
    env = dict(
        line.split("=", 1) for line in (igni.run_dir / "producer.env").read_text().splitlines()
    )
    assert env == {
        "IGNI_ACCESS_TOKEN_FILE": str(igni.run_dir / "token"),
        "IGNI_ORG": "org-acme",
        "IGNI_PRODUCER": "person:u-ada",
        "IGNI_PRODUCER_INCARNATION": "m-1",
        "IGNI_DATABASE_URL": "postgresql://db/x",
    }
    # Published by rename: no staging file is left behind.
    assert not (igni.run_dir / ".producer.env").exists()


def test_wait_ready_returns_once_the_ready_file_exists(launched):
    mod, made = launched
    igni = mod.Igni(database_url=None, port=1)
    igni.start(_fake(mod, None))
    (igni.run_dir / "ready").write_text("")
    igni.wait_ready(timeout=1)


def test_wait_ready_fails_with_the_log_when_igni_died(launched):
    mod, made = launched
    igni = mod.Igni(database_url=None, port=1)
    igni.start(_fake(mod, None))
    igni.log.write_text("Traceback: boom")
    made[0].returncode = 1
    with pytest.raises(AssertionError, match="Traceback: boom"):
        igni.wait_ready(timeout=5)


def test_wait_ready_times_out_when_igni_never_says_ready(launched):
    mod, _ = launched
    igni = mod.Igni(database_url=None, port=1)
    igni.start(_fake(mod, None))
    with pytest.raises(TimeoutError, match="never said ready"):
        igni.wait_ready(timeout=0.05)


def test_stop_terminates_and_kills_only_when_terminate_is_ignored(launched):
    mod, made = launched
    polite = mod.Igni(database_url=None, port=1)
    polite.start(_fake(mod, None))
    polite.stop()
    assert made[0].terminated and not made[0].killed
    stubborn = mod.Igni(database_url=None, port=1)
    stubborn.start(_fake(mod, None))
    made[1].ignores_term = True
    stubborn.stop()
    assert made[1].terminated and made[1].killed


def test_stop_leaves_an_igni_that_already_exited_alone(launched):
    mod, made = launched
    igni = mod.Igni(database_url=None, port=1)
    igni.start(_fake(mod, None))
    made[0].returncode = 0
    igni.stop()
    assert not made[0].terminated
