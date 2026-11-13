"""``ProjectRoot``: a path an agent touched, as the session's project sees it, or not at all."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from igni_core.extensions.host.project_paths import ProjectPath, ProjectRoot


class Dirs:
    """Where each session's project is; asked every time."""

    def __init__(self, projects: dict[str, Path], default: str) -> None:
        self.projects = projects
        self.default = default
        self.asked: list[str] = []

    def project_dir_of(self, session_id: str) -> Path:
        self.asked.append(session_id)
        return self.projects[session_id]

    def default_session(self) -> str:
        return self.default


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    return root


def place(project: Path, path: str, session: str = "s1") -> ProjectPath:
    return ProjectRoot(Dirs({"s1": project}, "s1")).relative(session, path)


def test_a_relative_path_inside_the_project_is_itself() -> None:
    assert ProjectRoot(Dirs({"s1": Path("/nowhere/project")}, "s1")).relative(
        "s1", "src/app.py"
    ) == ProjectPath(ok=True, relative="src/app.py")


def test_an_absolute_path_inside_the_project_is_made_relative(project: Path) -> None:
    assert place(project, str(project / "src" / "app.py")) == ProjectPath(
        ok=True, relative="src/app.py"
    )


def test_a_path_that_goes_up_and_comes_back_is_normalised(project: Path) -> None:
    assert place(project, "src/../src/./app.py").relative == "src/app.py"


def test_the_project_root_itself_is_the_dot(project: Path) -> None:
    assert place(project, str(project)) == ProjectPath(ok=True, relative=".")
    assert place(project, ".").relative == "."


def test_a_name_with_unicode_is_kept(project: Path) -> None:
    assert place(project, "src/ünï cödé.py").relative == "src/ünï cödé.py"


def test_an_empty_path_is_no_path(project: Path) -> None:
    assert place(project, "") == ProjectPath(ok=False, reason="no path")


@pytest.mark.parametrize("path", ["../elsewhere/x.py", "src/../../x.py", "/etc/passwd"])
def test_a_path_outside_the_project_is_nothing_to_tell(project: Path, path: str) -> None:
    assert place(project, path) == ProjectPath(ok=False, reason="outside the project")


def test_a_sibling_that_only_shares_the_prefix_is_outside(project: Path) -> None:
    sibling = project.parent / "project-two"
    sibling.mkdir()

    assert place(project, str(sibling / "x.py")).ok is False


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_a_symlink_that_leaves_the_project_is_outside(project: Path, tmp_path: Path) -> None:
    (tmp_path / "secret").mkdir()
    (project / "link").symlink_to(tmp_path / "secret")

    assert place(project, "link/key.pem") == ProjectPath(ok=False, reason="outside the project")


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_a_symlink_that_stays_in_the_project_is_followed(project: Path) -> None:
    (project / "alias").symlink_to(project / "src")

    assert place(project, "alias/app.py").relative == "src/app.py"


def test_a_path_with_a_null_byte_is_not_ok_and_says_why(project: Path) -> None:
    placed = place(project, "src/a\0b.py")

    assert placed.ok is False
    assert "null" in placed.reason


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_a_symlink_loop_is_not_ok_and_says_why(project: Path) -> None:
    (project / "a").symlink_to(project / "b")
    (project / "b").symlink_to(project / "a")

    placed = place(project, "a/x.py")

    assert placed.ok is False
    assert "loop" in placed.reason.lower()


def test_an_empty_session_is_the_default_sessions_project(tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    dirs = Dirs({"s1": one, "s2": two}, "s2")
    root = ProjectRoot(dirs)

    assert root.relative("", str(two / "x.py")).relative == "x.py"
    assert root.relative("", str(one / "x.py")).ok is False
    assert root.relative("s1", str(one / "x.py")).relative == "x.py"


def test_each_session_is_held_to_its_own_project(tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    root = ProjectRoot(Dirs({"s1": one, "s2": two}, "s1"))

    assert root.relative("s1", str(two / "x.py")).reason == "outside the project"


def test_the_dirs_are_asked_on_every_call(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    dirs = Dirs({"s1": first}, "s1")
    root = ProjectRoot(dirs)

    assert root.relative("s1", "x.py").ok is True
    dirs.projects["s1"] = second

    assert root.relative("s1", str(first / "x.py")).ok is False
    assert dirs.asked == ["s1", "s1"]
