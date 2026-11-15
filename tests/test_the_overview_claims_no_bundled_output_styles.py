"""igni ships no output styles, and the overview does not say it does.

IGNI_PROJECT_OVERVIEW.md named ``default`` / ``explanatory`` / ``learning``
styles. igni ships none: ``/output-style explanatory`` answers "unknown
output style ... (none configured)" unless the project adds one
(campaign R155). Styles come from ``.igni/output-styles/``,
``~/.igni/output-styles/``, Claude Code's directories and plugins.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def test_the_package_ships_no_style_files():
    assert not list((_ROOT / "src/igni_core").rglob("output-styles/*.md"))
    assert not list((_ROOT / "src/igni_core/core/output_styles").rglob("*.md"))


def test_the_overview_does_not_name_bundled_styles():
    text = (_ROOT / "docs/IGNI_PROJECT_OVERVIEW.md").read_text()
    assert "explanatory" not in text
