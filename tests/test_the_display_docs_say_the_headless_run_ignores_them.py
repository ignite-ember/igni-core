"""The display block and --verbose/--quiet are documented as they act.

CONFIGURATION.md said ``markdown`` renders Markdown "in terminal" and
``show_tool_calls`` shows tools; QUICKSTART said ``--verbose`` shows
routing and reasoning and ``--quiet`` gives minimal output. The headless
run reads none of ``settings.display``: ``igni -m`` output is the same
whichever way they are set (campaign R017, R086).
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src/igni_core"


def test_the_terminal_path_reads_no_display_setting():
    for path in [*(_SRC / "cli").rglob("*.py"), _SRC / "core/utils/display.py"]:
        text = path.read_text()
        assert "display.markdown" not in text and "show_tool_calls" not in text, path


def test_the_docs_do_not_promise_terminal_effects():
    config = (_ROOT / "docs/CONFIGURATION.md").read_text()
    quickstart = (_ROOT / "QUICKSTART.md").read_text()
    assert "# Render markdown in terminal" not in config
    assert "does not apply them" in config
    assert "| `--verbose` | Show routing decisions and reasoning |" not in quickstart
    assert "| `--quiet` | Minimal output |" not in quickstart
    assert "Use `--verbose` to see which agents" not in quickstart


def test_the_help_text_does_not_promise_terminal_effects():
    """`igni --help` said --verbose "Show routing and reasoning" and --quiet
    "Minimal output"; neither changes what ``igni -m`` prints (campaign R017)."""
    from click.testing import CliRunner

    from igni_core.cli import cli

    text = " ".join(CliRunner().invoke(cli, ["--help"], terminal_width=400).output.split())
    assert "Show routing and reasoning" not in text
    assert "--quiet Minimal output" not in text
    assert "display.show_routing" in text
