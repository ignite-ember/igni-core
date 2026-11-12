"""``igni_cloud/guest/files.py``: the files igni and the guest agent share.

A reader sees the old content or the new, never half; what a failed
write leaves is the old file and no temporary beside it.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from igni_cloud.guest.files import AtomicFile, ReadyFile


class Cancelled(BaseException):
    """What a cancellation is to ``except``: not an ``Exception``."""


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_a_file_is_readable_by_the_guest_agent_unless_a_mode_is_asked_for(tmp_path: Path):
    AtomicFile(tmp_path / "open").write(b"x")
    AtomicFile(tmp_path / "secret", mode=0o600).write(b"x")
    assert _mode(tmp_path / "open") == 0o644
    assert _mode(tmp_path / "secret") == 0o600


def test_a_write_makes_the_directory_it_needs(tmp_path: Path):
    target = tmp_path / "run" / "igni" / "activity.json"
    AtomicFile(target).write(b"{}")
    assert target.read_bytes() == b"{}"


def test_an_empty_write_leaves_an_empty_file(tmp_path: Path):
    target = tmp_path / "empty"
    AtomicFile(target).write(b"data")
    AtomicFile(target).write(b"")
    assert target.read_bytes() == b""


@pytest.mark.parametrize("failure", [OSError("disk full"), Cancelled()])
def test_a_failed_write_keeps_the_old_content_and_leaves_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
):
    target = tmp_path / "activity.json"
    AtomicFile(target).write(b"old")

    def broken(_source: object, _target: object) -> None:
        raise failure

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(type(failure)):
        AtomicFile(target).write(b"new")
    assert target.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["activity.json"]


def test_removing_deletes_the_file_and_a_missing_one_is_fine(tmp_path: Path):
    target = tmp_path / "token"
    file = AtomicFile(target)
    file.write(b"x")
    file.remove()
    assert not target.exists()
    file.remove()
    assert not target.exists()


def test_the_ready_file_names_where_it_is_and_says_who_is_listening(tmp_path: Path):
    ready = ReadyFile(tmp_path / "run" / "ready")
    assert ready.path == tmp_path / "run" / "ready"
    ready.mark()
    assert ready.path.read_bytes() == f"{os.getpid()}\n".encode()
    assert _mode(ready.path) == 0o644
