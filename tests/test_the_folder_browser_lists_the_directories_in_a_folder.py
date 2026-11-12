"""What the GUI's folder browser is told about one folder.

The browser picks project roots: it lists the sub-directories of a path,
hides the dot-directories unless asked, and says where "up" is and where
home is. A path it cannot list comes back as an error text in the result
rather than an exception, so one bad click does not break the window.
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

import pytest

from igni_core.backend.dir_scanner import DirScanner


def _scan(path: Path, show_hidden: bool = False):
    return DirScanner(path, show_hidden).scan()


def test_only_the_directories_are_listed_and_files_are_not(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("x")

    result = _scan(tmp_path)

    assert result.dirs == ["docs", "src"]
    assert result.error == ""


def test_they_are_sorted_without_regard_to_case(tmp_path: Path):
    for name in ("beta", "Alpha", "gamma", "Delta", "épsilon"):
        (tmp_path / name).mkdir()

    assert _scan(tmp_path).dirs == ["Alpha", "beta", "Delta", "gamma", "épsilon"]


def test_dot_directories_are_hidden_unless_asked_for(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "app").mkdir()

    assert _scan(tmp_path, show_hidden=False).dirs == ["app"]
    assert _scan(tmp_path, show_hidden=True).dirs == [".git", "app"]


def test_the_result_names_the_resolved_path_its_parent_and_home(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    folder = tmp_path / "a" / "b"
    folder.mkdir(parents=True)

    result = _scan(folder)

    assert result.path == str(folder.resolve())
    assert result.parent == str(folder.resolve().parent)
    assert result.home == str(tmp_path / "home")


def test_a_tilde_is_the_home_directory(tmp_path: Path, monkeypatch):
    (tmp_path / "projects").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    result = _scan(Path("~"))

    assert result.path == str(tmp_path.resolve())
    assert result.dirs == ["projects"]


def test_a_link_to_a_directory_is_listed_and_a_broken_one_is_not(tmp_path: Path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    (tmp_path / "dangling").symlink_to(tmp_path / "nowhere")

    assert _scan(tmp_path).dirs == ["link", "real"]


def test_an_empty_folder_lists_nothing_and_is_not_an_error(tmp_path: Path):
    result = _scan(tmp_path)

    assert result.dirs == []
    assert result.error == ""


def test_the_root_has_no_parent():
    result = _scan(Path(Path.home().anchor))

    assert result.parent == ""
    assert result.error == ""


def test_a_missing_path_is_an_error_text_and_no_directories(tmp_path: Path):
    missing = tmp_path / "gone"

    result = _scan(missing)

    assert result.dirs == []
    assert "gone" in result.error or "No such file" in result.error
    assert result.path == str(missing.resolve())
    assert result.parent == str(tmp_path.resolve())


def test_a_file_is_an_error_text_and_no_directories(tmp_path: Path):
    target = tmp_path / "notes.txt"
    target.write_text("x")

    result = _scan(target)

    assert result.dirs == []
    assert result.error != ""


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs a user who can be denied")
def test_a_folder_that_may_not_be_read_is_an_error_text(tmp_path: Path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        result = _scan(locked)
    finally:
        locked.chmod(0o700)

    assert result.dirs == []
    assert "denied" in result.error.lower()


@pytest.mark.asyncio
async def test_the_async_scan_gives_the_same_answer_off_the_event_loop(tmp_path: Path, monkeypatch):
    (tmp_path / "one").mkdir()
    ran_on: list[int] = []
    real_iterdir = Path.iterdir

    def spy(self):
        ran_on.append(threading.get_ident())
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", spy)

    result = await DirScanner(tmp_path, False).scan_async()

    assert result == _scan(tmp_path)
    assert ran_on and ran_on[0] != threading.get_ident()
    assert asyncio.get_running_loop() is not None
