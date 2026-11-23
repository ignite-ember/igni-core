"""The headless output is not hard-wrapped at 80 columns.

Rich wraps at the console's width, and a console whose file is not a
terminal is 80 columns wide: a path or a message came out split across
two lines, and a reader had to join them by hand. A stream that is
not a terminal gets the text as it is — no wrapping, no cropping, no
markup.

The terminal keeps its rendering: a console passed in is rendered as
Rich renders it (see ``test_display_edges.py``).
"""

from __future__ import annotations

from igni_core.core.utils.display import DisplayManager, RunStats
from igni_core.core.utils.display_schemas import ToolCallDisplay

#: Long enough to be split at 80 columns, and long enough to matter:
#: a path a reader has to copy.
LONG = (
    "storage: /Users/someone/Library/Application Support/igni/projects/"
    "9f2c1d0a-4b7e-4c1a-9f3d-6b2e8a5c7d10/state.db"
)


def test_an_info_line_is_not_wrapped_when_stdout_is_not_a_terminal(capsys) -> None:
    DisplayManager().print_info(LONG)
    assert capsys.readouterr().out == LONG + "\n"


def test_an_error_is_not_wrapped_when_stderr_is_not_a_terminal(capsys) -> None:
    DisplayManager().print_error(LONG)
    captured = capsys.readouterr()
    assert captured.err == "Error: " + LONG + "\n"
    assert captured.out == ""


def test_a_warning_is_not_wrapped_when_stderr_is_not_a_terminal(capsys) -> None:
    DisplayManager().print_warning(LONG)
    assert capsys.readouterr().err == "Warning: " + LONG + "\n"


def test_the_run_summary_is_not_wrapped_when_stderr_is_not_a_terminal(capsys) -> None:
    DisplayManager().print_run_stats(RunStats(elapsed_seconds=1.0, model=LONG))
    assert capsys.readouterr().err == f"  ── 1.0s · {LONG} ──\n"


def test_text_that_looks_like_markup_is_printed_as_it_is(capsys) -> None:
    """A provider's text is not the display's markup — and in a pipe the
    escape Rich would need is not printed either."""
    DisplayManager().print_info("the model replied [/INST] and stopped")
    assert capsys.readouterr().out == "the model replied [/INST] and stopped\n"


def test_a_tool_call_is_not_wrapped(capsys) -> None:
    DisplayManager().print_tool_call(
        ToolCallDisplay(tool_name="read_file", args={"path": "/tmp/" + "x" * 60})
    )
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert out.startswith("  > read_file (path=")
