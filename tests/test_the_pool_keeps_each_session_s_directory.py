"""The pool's record of which directory each session works in.

``PoolDirectories`` is what an attach is held to (a folder that is not a
directory, a session of another project) and what a resumed session opens in
(its recorded folder, or the backend's own when that folder is gone). The
cross-folder refusal has its own file; the rest of the rules are here, over a
small in-memory store and a real :class:`SessionRuntime`.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from igni_core.backend.pool_directories import PoolDirectories
from igni_core.backend.schemas_pool import AttachRefused, ProjectDirs
from igni_core.backend.session_pool import SessionRuntime


class _Store:
    def __init__(self) -> None:
        self.dirs: dict[str, str] = {}
        self.writes: list[tuple[str, str]] = []

    def get_dir(self, session_id: str) -> str | None:
        return self.dirs.get(session_id)

    def set_dir(self, session_id: str, project_dir: str | Path) -> None:
        self.writes.append((session_id, str(project_dir)))
        self.dirs[session_id] = str(project_dir)


def _pool(tmp_path: Path, **kw) -> tuple[PoolDirectories, _Store, Path]:
    home = tmp_path / "home"
    home.mkdir()
    store = _Store()
    return PoolDirectories(store, ProjectDirs(project_dir=home, **kw)), store, home  # type: ignore[arg-type]


def test_the_backends_own_directories_are_reported_as_given(tmp_path: Path):
    extra = [tmp_path / "x"]
    pool, _, home = _pool(tmp_path, additional_dirs=extra)

    assert pool.project_dir == home
    assert pool.additional_dirs == extra


def test_a_backend_started_without_extra_directories_reports_none(tmp_path: Path):
    pool, _, _ = _pool(tmp_path)

    assert pool.additional_dirs is None


# ── dir_for ─────────────────────────────────────────────────────────


def test_a_session_opens_in_the_directory_it_was_recorded_in(tmp_path: Path):
    pool, store, _ = _pool(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    store.set_dir("s1", other)

    assert pool.dir_for("s1") == other


def test_a_session_never_recorded_opens_in_the_backends_directory(tmp_path: Path, caplog):
    pool, _, home = _pool(tmp_path)

    with caplog.at_level(logging.WARNING):
        assert pool.dir_for("never") == home
    assert caplog.records == []


def test_a_recorded_directory_that_is_gone_falls_back_and_says_so(tmp_path: Path, caplog):
    pool, store, home = _pool(tmp_path)
    store.set_dir("s1", tmp_path / "deleted")

    with caplog.at_level(logging.WARNING):
        assert pool.dir_for("s1") == home
    assert "s1" in caplog.text and "deleted" in caplog.text and "falling back" in caplog.text


def test_an_empty_record_is_the_same_as_none(tmp_path: Path):
    pool, store, home = _pool(tmp_path)
    store.dirs["s1"] = ""

    assert pool.dir_for("s1") == home


def test_a_recorded_file_is_not_a_directory_to_open_in(tmp_path: Path):
    pool, store, home = _pool(tmp_path)
    afile = tmp_path / "file.txt"
    afile.write_text("x")
    store.set_dir("s1", afile)

    assert pool.dir_for("s1") == home


def test_record_writes_the_session_to_the_store(tmp_path: Path):
    pool, store, home = _pool(tmp_path)

    pool.record("s1", home)

    assert store.writes == [("s1", str(home))]


# ── record_if_changed ───────────────────────────────────────────────


def _runtime(session_id: str, project_dir: Path) -> SessionRuntime:
    backend = SimpleNamespace(session_id=session_id, project_dir=project_dir)
    return SessionRuntime(
        backend=backend,  # type: ignore[arg-type]
        rpc_table={},
        queue=[],
        transport=SimpleNamespace(),  # type: ignore[arg-type]
    )


def test_a_runtime_is_recorded_once_until_its_id_or_directory_moves(tmp_path: Path):
    pool, store, home = _pool(tmp_path)
    runtime = _runtime("s1", home)

    pool.record_if_changed(runtime)
    pool.record_if_changed(runtime)
    pool.record_if_changed(runtime)
    assert store.writes == [("s1", str(home))]

    runtime.backend.session_id = "s2"
    pool.record_if_changed(runtime)
    assert store.writes[-1] == ("s2", str(home))

    moved = tmp_path / "moved"
    runtime.backend.project_dir = moved
    pool.record_if_changed(runtime)
    assert store.writes[-1] == ("s2", str(moved))
    assert len(store.writes) == 3


# ── claim ───────────────────────────────────────────────────────────


def test_a_folder_that_is_not_a_directory_is_refused_and_nothing_is_recorded(tmp_path: Path):
    pool, store, _ = _pool(tmp_path)
    afile = tmp_path / "f.txt"
    afile.write_text("x")

    for wanted in (afile, tmp_path / "missing"):
        refused = pool.claim("", str(wanted))
        assert isinstance(refused, AttachRefused)
        assert refused.reason == f"not a directory: {wanted}"
    assert store.writes == []


def test_a_known_id_with_a_new_folder_is_recorded_at_the_resolved_folder(tmp_path: Path):
    pool, store, _ = _pool(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    assert pool.claim("s9", str(link)) == "s9"
    assert store.dirs["s9"] == str(real.resolve())


def test_a_folder_reached_another_way_is_still_the_sessions_own_folder(tmp_path: Path):
    pool, store, _ = _pool(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    store.set_dir("s1", link)

    assert pool.claim("s1", str(real)) == "s1"
    assert pool.claim("s1", str(real / ".." / "real")) == "s1"


def test_a_tilde_folder_is_expanded_before_it_is_checked(tmp_path: Path, monkeypatch):
    pool, store, _ = _pool(tmp_path)
    (tmp_path / "proj").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))

    claimed = pool.claim("", "~/proj")

    assert isinstance(claimed, str)
    assert store.dirs[claimed] == str((tmp_path / "proj").resolve())


def test_an_id_alone_for_a_session_of_the_backends_project_is_returned(tmp_path: Path):
    pool, store, home = _pool(tmp_path)
    store.set_dir("s1", home)

    assert pool.claim("s1", None) == "s1"
    assert pool.claim("s1", "") == "s1"
    assert store.writes == [("s1", str(home))], "claiming records nothing"


def test_an_id_alone_nobody_recorded_is_returned(tmp_path: Path):
    pool, store, _ = _pool(tmp_path)

    assert pool.claim("unseen", None) == "unseen"
    assert store.writes == []


def test_an_id_alone_for_a_session_of_another_project_is_refused(tmp_path: Path):
    pool, store, home = _pool(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    store.set_dir("s1", other)

    refused = pool.claim("s1", None)

    assert isinstance(refused, AttachRefused)
    assert "belongs to a different project" in refused.reason
    assert str(other) in refused.reason and str(home) in refused.reason


def test_neither_id_nor_folder_is_a_fresh_eight_character_session(tmp_path: Path):
    pool, store, _ = _pool(tmp_path)

    ids = {pool.claim("", None) for _ in range(20)}

    assert len(ids) == 20
    assert all(isinstance(i, str) and re.fullmatch(r"[0-9a-f]{8}", i) for i in ids)
    assert store.writes == []


def test_a_new_session_in_a_folder_gets_an_eight_character_id_recorded_there(tmp_path: Path):
    pool, store, _ = _pool(tmp_path)
    folder = tmp_path / "f"
    folder.mkdir()

    claimed = pool.claim("", str(folder))

    assert isinstance(claimed, str) and re.fullmatch(r"[0-9a-f]{8}", claimed)
    assert store.writes == [(claimed, str(folder.resolve()))]


@pytest.mark.parametrize("project_dir", [None, ""])
def test_an_empty_folder_counts_as_no_folder(tmp_path: Path, project_dir):
    pool, store, _ = _pool(tmp_path)

    assert pool.claim("", project_dir) != ""
    assert store.writes == []
