"""``igni migrate`` against a PostgreSQL server it cannot reach: a message, not a traceback.

With the cloud layer installed the driver is there, so the failure is the
connection's: the command names the database (its password redacted) and
why, and exits 1.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from igni_core.cli.migrate import migrate


def test_a_database_that_cannot_be_reached_is_a_message_and_exit_one(tmp_path: Path):
    unreachable = "postgresql+psycopg://nobody:hunter2@127.0.0.1:1/none"

    result = CliRunner().invoke(
        migrate, ["--database-url", unreachable, "--project-dir", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), "a message, not a traceback"
    assert "could not migrate postgresql+psycopg://nobody:***@127.0.0.1:1/none" in result.output
    assert "Connection refused" in result.output
    assert "hunter2" not in result.output
