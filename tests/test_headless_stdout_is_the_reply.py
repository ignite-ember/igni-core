"""``igni -m`` / ``igni -p`` piped: stdout carries the reply and nothing else.

QUICKSTART calls single-message mode "Good for scripts and CI", and `-p`
"writes to stdout". Piped, stdout got a ``── 2.1s · minimax ──`` footer
every run, the reply padded with spaces into an 80-column box, and `-c`
wrote "Resuming last session: 9d719ef9" there too
(that one is pinned in test_the_cli_invocation_runs_its_startup_phases).
"""

from __future__ import annotations

import pytest

from igni_core.core.utils.display import DisplayManager, RunStats

LONG = "word " * 40


def test_a_piped_reply_is_written_as_the_model_wrote_it(capsys: pytest.CaptureFixture[str]):
    DisplayManager().print_response(LONG.strip() + "\n\n**done**")

    out = capsys.readouterr().out
    assert out == LONG.strip() + "\n\n**done**\n"


def test_the_run_footer_errors_and_warnings_go_to_stderr(capsys: pytest.CaptureFixture[str]):
    display = DisplayManager()
    display.print_run_stats(RunStats(elapsed_seconds=2.1, model="minimax"))
    display.print_error("boom")
    display.print_warning("careful")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "2.1s · minimax" in captured.err
    assert "Error: boom" in captured.err
    assert "Warning: careful" in captured.err
