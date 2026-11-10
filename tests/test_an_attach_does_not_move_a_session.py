"""An attach with a folder does not move an existing session to it.

With ``project_dir``, the cross-project check was skipped and the
session's folder was rewritten to the one asked for: its history read
from one project, its tools run in another — and a live runtime kept
answering with its old folder while the record said the new one.
"""

from __future__ import annotations

from pathlib import Path

from igni_core.backend.pool_directories import PoolDirectories
from igni_core.backend.schemas_pool import AttachRefused, ProjectDirs


class _Store:
    def __init__(self) -> None:
        self.dirs: dict[str, str] = {}

    def get_dir(self, session_id: str) -> str | None:
        return self.dirs.get(session_id)

    def set_dir(self, session_id: str, project_dir: str | Path) -> None:
        self.dirs[session_id] = str(project_dir)


def _dirs(tmp_path: Path) -> tuple[PoolDirectories, _Store, Path, Path]:
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    store = _Store()
    return PoolDirectories(store, ProjectDirs(project_dir=one)), store, one, two  # type: ignore[arg-type]


def test_a_session_of_another_folder_is_refused_not_moved(tmp_path: Path) -> None:
    dirs, store, one, two = _dirs(tmp_path)
    store.set_dir("s1", one)

    refused = dirs.claim("s1", str(two))

    assert isinstance(refused, AttachRefused)
    assert "belongs to" in refused.reason
    assert store.get_dir("s1") == str(one)


def test_a_session_in_its_own_folder_is_resumed(tmp_path: Path) -> None:
    dirs, store, one, _ = _dirs(tmp_path)
    store.set_dir("s1", one.resolve())

    assert dirs.claim("s1", str(one)) == "s1"


def test_a_new_session_in_a_folder_is_recorded_there(tmp_path: Path) -> None:
    dirs, store, _, two = _dirs(tmp_path)

    claimed = dirs.claim("", str(two))

    assert isinstance(claimed, str)
    assert store.get_dir(claimed) == str(two.resolve())
