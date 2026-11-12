"""``python -m igni_core.backend --help`` names the product igni."""

from __future__ import annotations

from click.testing import CliRunner

from igni_core.backend.__main__ import main


def test_help_names_the_product_igni():
    result = CliRunner().invoke(main, ["--help"])

    assert "Start the igni backend server." in result.output
    assert "mber" not in result.output
