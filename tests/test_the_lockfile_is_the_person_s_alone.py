"""The lockfile, which can carry a token, is readable by its owner alone.

On POSIX it is created 0600. On Windows the mode bits mean nothing: the
file inherited its folder's access list, and a project folder outside the
user's profile commonly lets every signed-in user read what is in it.
There, its access list is made owner-only before the token is written,
and a lockfile is not written at all when that cannot be done.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from igni_core.backend import lockfile
from igni_core.backend.lockfile import Lockfile
from igni_core.backend.schemas_lockfile import LockfilePayload


def _payload() -> LockfilePayload:
    return LockfilePayload.now(pid=1, port=2, wire_version="1", ws_token="secret")


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_on_posix_the_lockfile_is_0600(tmp_path: Path) -> None:
    lock = Lockfile(tmp_path)
    assert lock.write(_payload()).ok
    assert stat.S_IMODE(lock.path.stat().st_mode) == 0o600


def test_on_windows_the_lockfile_is_made_owner_only_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def icacls(args: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(args)
        assert Path(args[1]).read_bytes() == b"", "set before the token is written"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(lockfile, "_WINDOWS", True)
    monkeypatch.setattr(subprocess, "run", icacls)
    monkeypatch.setattr(lockfile.getpass, "getuser", lambda: "ada")

    assert Lockfile(tmp_path).write(_payload()).ok
    [args] = calls
    assert args[0].endswith("icacls.exe") and "/inheritance:r" in args and "ada:(F)" in args


def test_on_windows_no_lockfile_is_written_when_it_cannot_be_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lockfile, "_WINDOWS", True)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kw: SimpleNamespace(returncode=5, stdout="", stderr="Access is denied."),
    )

    lock = Lockfile(tmp_path)
    result = lock.write(_payload())

    assert not result.ok and "owner-only" in (result.reason or "")
    assert not lock.path.exists()
    assert not list(lock.path.parent.glob("*.tmp"))
