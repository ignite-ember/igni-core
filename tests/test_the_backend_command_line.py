"""``python -m igni_core.backend``: the flags, and the launch they become.

The desktop app, the web client and the cloud all start the backend by
this command. What it must do before anything is served: refuse to start
without a transport, keep a log without being asked, take the launch
token out of the environment, and hand the app one canonical launch.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from igni_core.backend import __main__ as entry
from igni_core.backend import logging_setup
from igni_core.backend.schemas_lifecycle import BackendLaunch
from igni_core.transport.ws_auth import TOKEN_ENV


class _RecordingApp:
    """Stands in for the composition root: records how it was launched."""

    launches: list[BackendLaunch] = []

    def __init__(self, launch: BackendLaunch) -> None:
        type(self).launches.append(launch)

    async def run(self) -> None:
        return None


@pytest.fixture
def launches(monkeypatch, tmp_path):
    _RecordingApp.launches = []
    monkeypatch.setattr(entry, "BackendApp", _RecordingApp)
    monkeypatch.setenv("IGNI_DEBUG_LOG_PATH", str(tmp_path / "logs" / "backend.log"))
    monkeypatch.delenv("IGNI_DEBUG_LOG", raising=False)
    pkg = logging.getLogger("igni_core")
    level = pkg.level
    try:
        yield _RecordingApp.launches
    finally:
        for h in list(pkg.handlers):
            if getattr(h, logging_setup._HANDLER_MARK, False):
                h.close()
                pkg.removeHandler(h)
        pkg.setLevel(level)


def _run(*args: str):
    return CliRunner().invoke(entry.main, list(args))


def test_it_refuses_to_start_without_a_transport(launches, tmp_path):
    result = _run("--project-dir", str(tmp_path))

    assert result.exit_code == 2
    assert "at least one of --socket or --ws-port is required" in result.output
    assert launches == []


def test_the_launch_carries_the_flags_and_the_canonical_project_dir(launches, tmp_path):
    real = tmp_path / "project"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    extra = tmp_path / "extra"
    extra.mkdir()

    result = _run(
        "--ws-port",
        "0",
        "--socket",
        "/tmp/igni.sock",
        "--project-dir",
        str(link),
        "--resume-session",
        "sess-1",
        "--additional-dirs",
        str(extra),
    )

    assert result.exit_code == 0, result.output
    (launch,) = launches
    assert launch.ws_port == 0
    assert launch.socket_path == "/tmp/igni.sock"
    assert launch.project_dir == real.resolve()
    assert launch.resume_session_id == "sess-1"
    assert launch.additional_dirs == [extra]


def test_no_additional_dirs_is_none_not_an_empty_list(launches, tmp_path):
    _run("--socket", "/tmp/igni.sock", "--project-dir", str(tmp_path))

    (launch,) = launches
    assert launch.additional_dirs is None
    assert launch.ws_token is None


def test_the_launch_token_leaves_the_environment_for_the_launch(launches, tmp_path, monkeypatch):
    """Shell tools, hooks and MCP servers inherit the environment."""
    monkeypatch.setenv(TOKEN_ENV, "  s3cret  ")
    seen: dict[str, str | None] = {}

    class _Spy(_RecordingApp):
        def __init__(self, launch: BackendLaunch) -> None:
            super().__init__(launch)
            import os

            seen["env"] = os.environ.get(TOKEN_ENV)

    monkeypatch.setattr(entry, "BackendApp", _Spy)

    _run("--ws-port", "0", "--project-dir", str(tmp_path))

    (launch,) = launches
    assert launch.ws_token == "s3cret"
    assert seen["env"] is None


def test_a_log_is_kept_without_the_debug_flag(launches, tmp_path):
    """The shipped app builds the argv and never passes ``--debug``."""
    log = tmp_path / "logs" / "backend.log"

    _run("--ws-port", "0", "--project-dir", str(tmp_path))

    assert f"backend log: {log}" in log.read_text()
    assert logging.getLogger("igni_core").level == logging.INFO


def test_the_debug_flag_chooses_the_level(launches, tmp_path):
    _run("--ws-port", "0", "--project-dir", str(tmp_path), "--debug")

    assert logging.getLogger("igni_core").level == logging.DEBUG


def test_a_project_dir_that_does_not_exist_is_refused(launches, tmp_path):
    result = _run("--ws-port", "0", "--project-dir", str(Path(tmp_path) / "nope"))

    assert result.exit_code == 2
    assert launches == []


# ── A backend started by hand takes its own window token ──────────


@pytest.fixture
def by_hand(monkeypatch):
    """No launcher: no token in the environment, no opt-out, a desktop layer."""
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.delenv("IGNI_DEV_NO_WS_TOKEN", raising=False)


def test_a_backend_started_by_hand_mints_its_window_token_and_prints_it_once(
    launches, tmp_path, by_hand
):
    result = CliRunner().invoke(entry.main, ["--ws-port", "0", "--project-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    (launch,) = launches
    assert launch.ws_token
    assert result.output.count(launch.ws_token) == 1
    assert "window token" in result.output
    assert "Local(url=..., token=...)" in result.output
    # Never in the log file either (the log is on disk).
    log = tmp_path / "logs" / "backend.log"
    assert launch.ws_token not in log.read_text()


def test_each_hand_started_backend_mints_a_different_token(launches, tmp_path, by_hand):
    _run("--ws-port", "0", "--project-dir", str(tmp_path))
    _run("--ws-port", "0", "--project-dir", str(tmp_path))

    first, second = launches
    assert first.ws_token and second.ws_token and first.ws_token != second.ws_token


def test_the_launchers_token_is_used_and_nothing_is_printed(
    launches, tmp_path, by_hand, monkeypatch
):
    monkeypatch.setenv(TOKEN_ENV, "from-the-launcher")

    result = _run("--ws-port", "0", "--project-dir", str(tmp_path))

    (launch,) = launches
    assert launch.ws_token == "from-the-launcher"
    assert "from-the-launcher" not in result.output
    assert "window token" not in result.output


def test_the_development_opt_out_still_means_no_token_at_all(
    launches, tmp_path, by_hand, monkeypatch
):
    monkeypatch.setenv("IGNI_DEV_NO_WS_TOKEN", "1")

    result = _run("--ws-port", "0", "--project-dir", str(tmp_path))

    (launch,) = launches
    assert launch.ws_token is None
    assert "window token" not in result.output


def test_a_socket_only_backend_has_no_websocket_and_mints_nothing(launches, tmp_path, by_hand):
    result = _run("--socket", "/tmp/igni.sock", "--project-dir", str(tmp_path))

    (launch,) = launches
    assert launch.ws_token is None
    assert "window token" not in result.output


def test_in_the_cloud_the_gateway_authenticates_so_the_backend_takes_no_token(
    launches, tmp_path, by_hand
):
    """The cloud's behaviour is exactly what it was: no token, only the Origin check."""
    from igni_core.core.layer import Layer, layers

    class _Cloud(Layer):
        name = "cloud"

    previous = layers.current()
    layers.install(_Cloud())
    try:
        result = _run("--ws-port", "0", "--project-dir", str(tmp_path))
    finally:
        layers.install(previous)

    (launch,) = launches
    assert launch.ws_token is None
    assert "window token" not in result.output
