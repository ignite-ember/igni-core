"""``igni-cloud``: the platform's own command line.

``docs/CLOUD.md`` §9. The node agent and igni server run ``igni-cloud db
grant-producer`` on a producer's VM before it can bind, so the group's
name, the commands it lists, its help and its exit codes are what those
callers see. The import is on that path too: a heavy import there costs
every VM's boot.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

from click.testing import CliRunner
from igni_cloud.cli import main
from igni_cloud.db.admin_cli import db

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_the_command_is_called_igni_cloud():
    assert main.name == "igni-cloud", "the name the node agent runs"


def test_its_help_lists_the_dbs_producer_commands():
    result = CliRunner().invoke(main, ["db", "--help"])
    assert result.exit_code == 0, result.output
    for command in ("grant-producer", "revoke-producer", "export-producer", "erase-producer"):
        assert command in result.output, command


def test_without_a_command_it_prints_its_help():
    """No command is a usage error: the help is printed and the exit code says so."""
    result = CliRunner().invoke(main, [])
    assert result.exit_code == 2, result.output
    assert "Usage: igni-cloud" in result.output
    assert "db" in result.output


def test_a_command_it_does_not_have_is_refused():
    result = CliRunner().invoke(main, ["grant-producer"])
    assert result.exit_code != 0
    assert "grant-producer" in result.output, "the name it refused is said"


def test_a_command_reaches_the_dbs_own_commands(monkeypatch):
    """``igni-cloud db grant-producer`` is the db group's command, not a copy."""
    monkeypatch.delenv("IGNI_DATABASE_URL", raising=False)
    result = CliRunner().invoke(
        main, ["db", "grant-producer", "--org", "o", "--producer", "person:p", "--incarnation", "m"]
    )
    assert result.exit_code != 0
    assert "IGNI_DATABASE_URL" in result.output, result.output


def test_a_producer_it_does_not_know_is_refused_through_the_group():
    result = CliRunner().invoke(
        main,
        [
            "db",
            "grant-producer",
            "--org",
            "o",
            "--producer",
            "robot:x",
            "--incarnation",
            "x",
            "--database-url",
            "postgresql://a@b/c",
        ],
    )
    assert result.exit_code != 0
    assert "kind" in result.output, result.output


def test_the_console_script_runs_this_group():
    scripts = tomllib.loads(PYPROJECT.read_text())["project"]["scripts"]
    assert scripts["igni-cloud"] == "igni_cloud.cli:main"


def test_the_group_imports_none_of_the_rest_of_igni():
    probe = (
        "import sys, igni_cloud.cli\n"
        "heavy = [m for m in ('agno', 'igni_core.cli', 'igni_core.core.config.settings', "
        "'igni_core.backend', 'openai', 'mcp') if m in sys.modules]\n"
        "print(heavy)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "[]", "the VM's first start pays for this import"


def test_the_db_group_is_the_one_the_producer_commands_live_in():
    assert main.commands["db"] is db
