"""``WorkspaceScope``: a path held to the project and the grant, and listings cut to the grant."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.workspace_scope import WorkspaceScope
from igni_core.extensions.permissions import WorkspaceAccess
from tests.host_model_helpers import manifest


def scope(root: Path, *paths: str, mode: str = "read") -> WorkspaceScope:
    permissions: dict = {"workspace": mode}
    if paths:
        permissions["workspacePaths"] = list(paths)
    return WorkspaceScope(root, WorkspaceAccess(manifest("acme.x", **permissions)), "acme.x")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "docs" / "deep").mkdir(parents=True)
    (root / "src").mkdir()
    return root


def code_of(call, *args, **kwargs) -> tuple[ErrorCode, str]:
    with pytest.raises(HostModelError) as caught:
        call(*args, **kwargs)
    return caught.value.code, str(caught.value)


def test_a_relative_and_an_absolute_path_inside_are_one_posix_path(project: Path) -> None:
    s = scope(project)

    assert s.relative("docs/a b/é.md") == "docs/a b/é.md"
    assert s.relative(str(project / "docs" / "a.md")) == "docs/a.md"
    assert s.relative("docs/../src/x.py") == "src/x.py"


@pytest.mark.parametrize("path", ["..", "../x", "docs/../../x", "/etc/passwd"])
def test_a_path_outside_the_project_is_refused(project: Path, path: str) -> None:
    code, message = code_of(scope(project).relative, path)

    assert code == ErrorCode.REFUSED
    assert "outside the project" in message


def test_a_link_to_outside_is_refused_though_it_sits_inside(project: Path, tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("s")
    (project / "docs" / "link.txt").symlink_to(tmp_path / "secret.txt")

    assert code_of(scope(project).relative, "docs/link.txt")[0] == ErrorCode.REFUSED


@pytest.mark.parametrize("path", ["", ".", "docs/.."])
def test_the_project_itself_is_no_file_but_is_the_empty_directory(project: Path, path: str) -> None:
    s = scope(project)

    assert code_of(s.relative, path) == (ErrorCode.INVALID, "a file path is required")
    assert s.relative(path, directory=True) == ""
    assert s.relative(str(project), directory=True) == ""


def test_a_file_outside_the_grant_is_not_granted_but_a_directory_is_not_held_to_it(
    project: Path,
) -> None:
    s = scope(project, "docs/*")

    assert s.relative("docs/a.md") == "docs/a.md"
    code, message = code_of(s.relative, "src/x.py")
    assert code == ErrorCode.NOT_GRANTED
    assert "'src/x.py'" in message and "'acme.x'" in message
    assert s.relative("src", directory=True) == "src"


def test_a_listing_keeps_only_the_directories_the_grant_reaches_into(project: Path) -> None:
    s = scope(project, "docs/*")

    kept = s.visible_dirs("", {"dirs": ["docs", "src"], "other": 1})
    assert kept == {"dirs": ["docs"], "other": 1}
    assert s.visible_dirs("docs", {"dirs": ["deep"]}) == {"dirs": ["deep"]}
    assert s.visible_dirs("src", {"dirs": ["a"]}) == {"dirs": []}


def test_a_listing_that_is_not_a_listing_is_returned_untouched(project: Path) -> None:
    s = scope(project, "docs/*")
    odd = {"dirs": "docs"}

    assert s.visible_dirs("", odd) is odd
    assert s.visible_dirs("", None) is None
    assert s.visible_dirs("", ["src"]) == ["src"]
    assert s.visible_completions("text") == "text"
    assert s.visible_hits(None) is None


def test_completions_keep_the_granted_paths_and_recount_the_total(project: Path) -> None:
    s = scope(project, "docs/*")

    found = s.visible_completions(
        {"matches": ["docs/a.md", "src/b.py", "docs/deep/c.md"], "total": 3}
    )

    assert found == {"matches": ["docs/a.md", "docs/deep/c.md"], "total": 2}
    assert s.visible_completions({"total": 9}) == {"matches": [], "total": 0}


def test_search_hits_keep_only_files_the_grant_reaches(project: Path) -> None:
    s = scope(project, "docs/*")

    found = s.visible_hits(
        {
            "matches": [
                {"path": "docs/a.md", "line": 1},
                {"path": "src/b.py", "line": 2},
                {"line": 3},
            ],
            "query": "q",
        }
    )

    assert found == {"matches": [{"path": "docs/a.md", "line": 1}], "query": "q"}
    assert s.visible_hits({}) == {"matches": []}


def test_without_a_path_grant_the_whole_project_is_visible(project: Path) -> None:
    s = scope(project)

    assert s.visible_completions({"matches": ["src/b.py"], "total": 5}) == {
        "matches": ["src/b.py"],
        "total": 1,
    }


def test_a_document_is_a_real_file_of_a_kind_igni_opens(project: Path) -> None:
    s = scope(project)
    (project / "docs" / "a.md").write_text("x")
    (project / "docs" / "SHOUT.PDF").write_text("x")

    assert s.document("docs/a.md") == (project / "docs" / "a.md").resolve()
    assert s.document("docs/SHOUT.PDF").name == "SHOUT.PDF"


def test_a_missing_path_or_a_directory_is_not_a_document(project: Path) -> None:
    s = scope(project)

    assert code_of(s.document, "docs/none.md")[0] == ErrorCode.NOT_FOUND
    assert code_of(s.document, "docs") == (ErrorCode.NOT_FOUND, "'docs' is not a file")


@pytest.mark.parametrize("name", ["run.sh", "noext", "app.app", "x.html.bak"])
def test_a_file_of_another_kind_is_not_a_document(project: Path, name: str) -> None:
    (project / name).write_text("x")

    code, message = code_of(scope(project).document, name)

    assert code == ErrorCode.INVALID
    assert "not a document" in message


def test_an_executable_document_is_refused(project: Path) -> None:
    path = project / "tool.html"
    path.write_text("x")
    path.chmod(0o755)

    assert code_of(scope(project).document, "tool.html") == (
        ErrorCode.INVALID,
        "'tool.html' is executable",
    )
    path.chmod(0o644)
    assert os.access(path, os.X_OK) is False
    assert scope(project).document("tool.html") == path.resolve()
