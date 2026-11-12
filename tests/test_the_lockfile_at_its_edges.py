"""The backend lockfile at its edges: what is stale, what fails softly, what is private.

``test_backend_lockfile.py`` pins the round trip and the four discovery
outcomes; ``test_the_lockfile_is_the_person_s_alone.py`` the owner-only
mode. These pin the rest: a file that is not an object, a read or a
remove that fails for another reason, the token that is written only
when there is one, a temp file left by a crash, and a write that fails
half way.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from igni_core.backend import lockfile
from igni_core.backend.lockfile import BackendDiscovery, Lockfile
from igni_core.backend.schemas_lockfile import LockfilePayload, NoBackend


def _payload(**kw) -> LockfilePayload:
    return LockfilePayload.now(pid=1, port=2, wire_version="1", **kw)


def test_a_file_holding_json_that_is_not_an_object_is_stale(tmp_path: Path) -> None:
    lock = Lockfile(tmp_path)
    lock.path.parent.mkdir(parents=True)

    for text in ("[1, 2, 3]", "42", '"text"', "null"):
        lock.path.write_text(text)
        assert lock.read() is None, text


def test_a_lockfile_that_cannot_be_read_is_none_not_an_error(tmp_path: Path) -> None:
    lock = Lockfile(tmp_path)
    lock.path.mkdir(parents=True)  # a directory where the file should be

    assert lock.read() is None


def test_a_remove_that_fails_for_another_reason_is_reported_not_raised(tmp_path: Path) -> None:
    lock = Lockfile(tmp_path)
    lock.path.mkdir(parents=True)
    (lock.path / "inner").write_text("x")

    result = lock.remove()

    assert result.ok is False
    assert result.existed is True
    assert result.reason
    assert lock.path.exists()


def test_the_token_is_written_only_when_there_is_one(tmp_path: Path) -> None:
    without = Lockfile(tmp_path / "a")
    with_token = Lockfile(tmp_path / "b")

    assert without.write(_payload()).ok
    assert with_token.write(_payload(ws_token="secret")).ok

    assert list(json.loads(without.path.read_text())) == [
        "pid",
        "port",
        "wire_version",
        "created_at",
    ]
    assert list(json.loads(with_token.path.read_text())) == [
        "pid",
        "port",
        "wire_version",
        "created_at",
        "ws_token",
    ]
    assert with_token.read().ws_token == "secret"  # type: ignore[union-attr]
    assert without.read().ws_token is None  # type: ignore[union-attr]


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_temp_file_left_by_a_crash_does_not_break_the_next_write(tmp_path: Path) -> None:
    lock = Lockfile(tmp_path)
    lock.path.parent.mkdir(parents=True)
    stale = lock.path.with_suffix(lock.path.suffix + ".tmp")
    stale.write_text("half a write")
    stale.chmod(0o644)

    result = lock.write(_payload(ws_token="secret"))

    assert result.ok
    assert not stale.exists()
    assert lock.read() == _read_back(lock)
    assert "half a write" not in lock.path.read_text()
    assert stat.S_IMODE(lock.path.stat().st_mode) == 0o600


def _read_back(lock: Lockfile) -> LockfilePayload:
    return LockfilePayload.model_validate(json.loads(lock.path.read_text()))


def test_on_windows_icacls_is_run_by_its_full_system_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def icacls(args: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(lockfile, "_WINDOWS", True)
    monkeypatch.setattr(subprocess, "run", icacls)
    monkeypatch.setattr(lockfile.getpass, "getuser", lambda: "Ada Ünal")
    monkeypatch.setenv("SYSTEMROOT", "D:\\Win")

    assert Lockfile(tmp_path).write(_payload()).ok

    [args] = calls
    assert args[0] == os.path.join("D:\\Win", "System32", "icacls.exe")
    assert args[2:] == ["/inheritance:r", "/grant:r", "Ada Ünal:(F)"]


def test_on_windows_without_systemroot_icacls_is_still_found_by_full_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(lockfile, "_WINDOWS", True)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kw: (
            calls.append(args) or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )
    monkeypatch.delenv("SYSTEMROOT", raising=False)

    assert Lockfile(tmp_path).write(_payload()).ok

    assert calls[0][0] == os.path.join("C:\\Windows", "System32", "icacls.exe")


def test_the_pid_is_checked_before_the_port(tmp_path: Path) -> None:
    Lockfile(tmp_path).write(_payload())
    asked: list[str] = []

    def pid(payload: LockfilePayload) -> bool:
        asked.append("pid")
        return False

    def port(payload: LockfilePayload) -> bool:
        asked.append("port")
        return True

    result = BackendDiscovery(
        tmp_path, expected_wire_version="1", pid_probe=pid, port_probe=port
    ).probe()

    assert isinstance(result, NoBackend)
    assert asked == ["pid"]


def test_the_discovery_exposes_the_lockfile_path(tmp_path: Path) -> None:
    discovery = BackendDiscovery(tmp_path, expected_wire_version="1")

    assert discovery.lockfile_path == Lockfile(tmp_path).path
    assert discovery.lockfile_path == tmp_path.resolve() / lockfile.CONFIG_DIR / "backend.lock"
