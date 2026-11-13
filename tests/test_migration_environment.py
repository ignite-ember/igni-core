"""What alembic's environment script does when a migration runs.

``igni_core/migrations/env.py`` runs inside every ``igni migrate``, every
startup upgrade and every autogenerate. These tests run it for real
against throwaway sqlite files, each in a fresh interpreter where the
state of ``logging`` or ``Base.metadata`` matters.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import igni_core

PACKAGE = Path(igni_core.__file__).resolve().parent


def _run(script: str, tmp_path: Path) -> list[str]:
    done = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=tmp_path,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


_PRELUDE = f"""
import json, logging
from alembic import command
from alembic.config import Config
config = Config({str(PACKAGE / "alembic.ini")!r})
"""


def test_every_migrated_table_is_registered_in_the_metadata(tmp_path: Path):
    db = tmp_path / "state.db"
    registered = _run(
        _PRELUDE
        + f"""
config.set_main_option("sqlalchemy.url", "sqlite:///" + {str(db)!r})
config.attributes["configure_logging"] = False
from igni_core.core.db.base import Base
assert not Base.metadata.tables
command.upgrade(config, "head")
print(json.dumps(sorted(Base.metadata.tables)))
""",
        tmp_path,
    )
    with sqlite3.connect(db) as conn:
        migrated = sorted(
            row[0]
            for row in conn.execute("select name from sqlite_master where type='table'")
            if row[0] != "alembic_version" and not row[0].startswith("sqlite_")
        )
    assert registered == migrated


def test_a_programmatic_upgrade_leaves_the_apps_log_handlers_alone(tmp_path: Path):
    handler_kept = _run(
        _PRELUDE
        + f"""
config.set_main_option("sqlalchemy.url", "sqlite:///" + {str(tmp_path / "a.db")!r})
config.attributes["configure_logging"] = False
mine = logging.NullHandler()
logging.getLogger().addHandler(mine)
logging.getLogger().setLevel(logging.DEBUG)
command.upgrade(config, "head")
print(json.dumps([mine in logging.getLogger().handlers, logging.getLogger().level]))
""",
        tmp_path,
    )
    assert handler_kept == [True, 10]


def test_the_alembic_cli_path_reconfigures_logging_from_the_ini(tmp_path: Path):
    handler_kept = _run(
        _PRELUDE
        + f"""
config.set_main_option("sqlalchemy.url", "sqlite:///" + {str(tmp_path / "b.db")!r})
mine = logging.NullHandler()
logging.getLogger().addHandler(mine)
command.upgrade(config, "head")
print(json.dumps([mine in logging.getLogger().handlers]))
""",
        tmp_path,
    )
    assert handler_kept == [False]


def test_offline_mode_writes_the_sql_without_connecting(tmp_path: Path):
    db = tmp_path / "never.db"
    sql = _run(
        _PRELUDE
        + f"""
import io
config.set_main_option("sqlalchemy.url", "sqlite:///" + {str(db)!r})
config.attributes["configure_logging"] = False
out = io.StringIO()
config.output_buffer = out
command.upgrade(config, "base:9318ebdb0db5", sql=True)
print(json.dumps(out.getvalue().splitlines()))
""",
        tmp_path,
    )
    text = "\n".join(sql)
    assert "CREATE TABLE" in text
    assert "INSERT INTO alembic_version" in text
    assert "?" not in text.split("INSERT INTO alembic_version")[1]
    assert not db.exists()
