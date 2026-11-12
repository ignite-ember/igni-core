"""The before-suspend hook asks igni to trim and never holds a suspend up.

``hooks/before-suspend.d/50-igni`` writes a token request, waits half a
second for igni to echo that token, and exits 0 whatever happens.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

HOOK = Path(__file__).parents[1] / "hooks" / "before-suspend.d" / "50-igni"


def run_hook(env: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], float]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("IGNI_")}
    start = time.monotonic()
    done = subprocess.run(
        [str(HOOK)], env={**clean, **env}, capture_output=True, text=True, timeout=10
    )
    return done, time.monotonic() - start


def respond(request: Path, ready: Path, answer: Callable[[str], str]) -> threading.Thread:
    """Answer the request the way igni would, with ``answer(token)`` as the file."""

    def loop() -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                token = json.loads(request.read_text())["token"]
            except (OSError, ValueError):
                time.sleep(0.001)
                continue
            ready.write_text(answer(token))
            return

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


def test_no_run_dir_is_a_silent_success_and_writes_nothing(tmp_path: Path) -> None:
    done, _ = run_hook({"IGNI_RUN_DIR": str(tmp_path / "absent")})
    assert done.returncode == 0
    assert done.stdout == "" and done.stderr == ""
    assert list(tmp_path.iterdir()) == []


def test_the_request_and_ready_paths_can_be_overridden_even_with_spaces(tmp_path: Path) -> None:
    where = tmp_path / "a dir"
    where.mkdir()
    request, ready = where / "ask me", where / "answer me"
    thread = respond(request, ready, lambda t: json.dumps({"token": t}))
    done, elapsed = run_hook(
        {
            "IGNI_RUN_DIR": str(tmp_path / "ignored"),
            "IGNI_SUSPENDING_FILE": str(request),
            "IGNI_SUSPEND_READY_FILE": str(ready),
        }
    )
    thread.join()
    assert done.returncode == 0 and done.stderr == ""
    assert elapsed < 0.4, "answered: it did not wait out the half second"
    assert not (tmp_path / "ignored").exists()
    token = json.loads(request.read_text())["token"]
    assert json.loads(ready.read_text())["token"] == token


def test_the_request_is_whole_json_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    done, _ = run_hook({"IGNI_RUN_DIR": str(tmp_path)})
    assert done.returncode == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["suspending"]
    token = json.loads((tmp_path / "suspending").read_text())["token"]
    assert token.rsplit("-", 1)[1].isdigit()


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(lambda t: json.dumps({"token": "stale-1"}), id="stale-token"),
        pytest.param(lambda t: json.dumps({"token": t + "x"}), id="longer-token"),
        pytest.param(lambda t: json.dumps({"token": t[1:]}), id="shorter-token"),
        pytest.param(lambda t: t, id="token-without-quotes"),
    ],
)
def test_an_answer_for_another_token_does_not_end_the_wait(
    tmp_path: Path, answer: Callable[[str], str]
) -> None:
    thread = respond(tmp_path / "suspending", tmp_path / "suspend-ready", answer)
    done, elapsed = run_hook({"IGNI_RUN_DIR": str(tmp_path)})
    thread.join()
    assert done.returncode == 0
    assert "did not answer" in done.stderr
    assert elapsed >= 0.45


def test_no_answer_takes_about_half_a_second_and_still_succeeds(tmp_path: Path) -> None:
    done, elapsed = run_hook({"IGNI_RUN_DIR": str(tmp_path)})
    assert done.returncode == 0
    assert done.stderr == f"igni did not answer {tmp_path}/suspending within 0.5 s\n"
    assert 0.45 <= elapsed < 3


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_an_unwritable_run_dir_still_succeeds_with_the_message(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    run.chmod(0o500)
    try:
        done, _ = run_hook({"IGNI_RUN_DIR": str(run)})
    finally:
        run.chmod(0o700)
    assert done.returncode == 0
    assert "did not answer" in done.stderr
    assert list(run.iterdir()) == []
