"""Opening and closing a terminal each write an audit line; what was typed or shown never does."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from igni_core.core.utils.audit import AuditEntry, AuditLogger, AuditStatus, TerminalDetails
from igni_core.protocol.rpc import RpcMethod
from tests.terminal_helpers import Rig, ScriptedSpawn, calm_bash, wait_until


def _logger(tmp_path: Path) -> AuditLogger:
    settings = MagicMock()
    settings.storage.audit_log = str(tmp_path / "audit.log")
    return AuditLogger(settings)


def _lines(tmp_path: Path) -> list[dict]:
    path = tmp_path / "audit.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_a_terminal_entry_is_the_person_s_and_names_no_session():
    entry = AuditEntry.terminal(
        TerminalDetails(action="opened", terminal_id="term-1", shell="/bin/zsh", cwd="/p")  # noqa: S604 — a field
    )
    line = json.loads(entry.to_jsonl_line())

    assert (line["session_id"], line["agent"], line["tool"], line["status"]) == (
        "",
        "person",
        "terminal",
        "success",
    )
    assert entry.status is AuditStatus.SUCCESS
    assert line["details"] == {
        "action": "opened",
        "terminal_id": "term-1",
        "shell": "/bin/zsh",
        "cwd": "/p",
    }


def test_a_closed_entry_carries_the_exit_code_and_how_long():
    entry = AuditEntry.terminal(
        TerminalDetails(  # noqa: S604 — a field named shell
            action="closed",
            terminal_id="term-1",
            shell="/bin/zsh",
            cwd="/p",
            exit_code=-1,
            duration_s=1.5,
        )
    )
    assert json.loads(entry.to_jsonl_line())["details"] == {
        "action": "closed",
        "terminal_id": "term-1",
        "shell": "/bin/zsh",
        "cwd": "/p",
        "exit_code": -1,
        "duration_s": 1.5,
    }


def test_the_details_round_trip_through_the_union():
    entry = AuditEntry.terminal(
        TerminalDetails(action="opened", terminal_id="t", shell="s", cwd="c")  # noqa: S604 — a field
    )
    again = AuditEntry.model_validate(entry.model_dump())
    assert isinstance(again.details, TerminalDetails)


async def test_open_and_close_each_write_one_line(tmp_path):
    spawn = ScriptedSpawn()
    rig = Rig(tmp_path, spawn=spawn, audit=_logger(tmp_path))

    terminal_id = await rig.open()
    (opened,) = _lines(tmp_path)
    assert opened["details"] == {
        "action": "opened",
        "terminal_id": terminal_id,
        "shell": "/bin/scripted",
        "cwd": str(tmp_path),
    }

    await rig.type(terminal_id, "secret-typed\r")
    spawn.made[0].prints(b"secret-shown")
    await rig.call(RpcMethod.TERMINAL_CLOSE, {"terminal_id": terminal_id})

    lines = _lines(tmp_path)
    assert len(lines) == 2
    closed = lines[1]["details"]
    assert closed["action"] == "closed"
    assert closed["terminal_id"] == terminal_id
    assert closed["exit_code"] == -1
    assert closed["duration_s"] >= 0
    text = (tmp_path / "audit.log").read_text()
    assert "secret" not in text


async def test_a_refused_open_writes_nothing(tmp_path):
    spawn = ScriptedSpawn()
    rig = Rig(tmp_path, spawn=spawn, audit=_logger(tmp_path))
    for _ in range(8):
        await rig.open()
    await rig.call(RpcMethod.TERMINAL_OPEN, {"cols": 80, "rows": 24})
    assert len(_lines(tmp_path)) == 8
    await rig.terminals.close_all()
    assert [line["details"]["action"] for line in _lines(tmp_path)].count("closed") == 8


async def test_a_window_going_away_writes_the_close(tmp_path):
    rig = Rig(tmp_path, spawn=ScriptedSpawn(), audit=_logger(tmp_path))
    terminal_id = await rig.open()

    rig.terminals.connection_watcher("detached", "window-1")

    await wait_until(lambda: len(_lines(tmp_path)) == 2)
    assert _lines(tmp_path)[1]["details"]["terminal_id"] == terminal_id


async def test_with_no_audit_log_nothing_is_written(tmp_path):
    rig = Rig(tmp_path, spawn=ScriptedSpawn())
    terminal_id = await rig.open()
    await rig.call(RpcMethod.TERMINAL_CLOSE, {"terminal_id": terminal_id})
    assert not (tmp_path / "audit.log").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX PTY")
async def test_a_real_shell_s_output_never_reaches_the_log(tmp_path, monkeypatch):
    calm_bash(tmp_path / "home", monkeypatch)
    project = tmp_path / "project"
    project.mkdir()
    rig = Rig(project, audit=_logger(tmp_path))
    terminal_id = await rig.open()
    await rig.type(terminal_id, "printf 'TOP%sSECRET\\n' -; exit 3\r")
    await rig.wire.wait_for_output(terminal_id, "TOP-SECRET")
    await wait_until(lambda: len(_lines(tmp_path)) == 2)

    opened, closed = _lines(tmp_path)
    assert opened["details"]["shell"] == "/bin/bash"
    assert opened["details"]["cwd"] == str(project)
    assert closed["details"]["exit_code"] == 3
    text = (tmp_path / "audit.log").read_text()
    assert "SECRET" not in text and "printf" not in text
