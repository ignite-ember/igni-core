"""The ``@`` picker: project files matching what was typed, from a warm index.

The index walks the project tree once and the service keeps it for the
life of the process, so a second keystroke does not walk it again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.backend.file_completion_service import FileCompletionService
from igni_core.core.utils import file_index as file_index_module


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("")
    (tmp_path / "src" / "apple.py").write_text("")
    (tmp_path / "README.md").write_text("")
    return tmp_path


@pytest.fixture(autouse=True)
def _no_git(monkeypatch):
    """A temporary folder is no repository: walk the tree."""
    monkeypatch.setattr(file_index_module, "_git_ls_files", lambda _dir: [])


@pytest.mark.asyncio
async def test_the_files_matching_the_query_come_back_with_their_count(project: Path):
    result = await FileCompletionService(project).complete("app", limit=10)

    assert sorted(result.matches) == ["src/app.py", "src/apple.py"]
    assert result.total == 2


@pytest.mark.asyncio
async def test_the_limit_cuts_the_list_but_not_the_total(project: Path):
    result = await FileCompletionService(project).complete("app", limit=1)

    assert len(result.matches) == 1
    assert result.total == 2


@pytest.mark.asyncio
async def test_an_empty_query_lists_the_files_in_order(project: Path):
    result = await FileCompletionService(project).complete("", limit=2)

    assert result.matches == ["README.md", "src/app.py"]
    assert result.total == 3


@pytest.mark.asyncio
async def test_a_query_matching_nothing_is_empty_with_a_zero_total(project: Path):
    result = await FileCompletionService(project).complete("zzzz", limit=10)

    assert result.matches == []
    assert result.total == 0


@pytest.mark.asyncio
async def test_the_index_is_built_once_and_reused(project: Path, monkeypatch):
    walks: list[Path] = []
    real = file_index_module._fallback_list

    def counting(project_dir: Path) -> list[str]:
        walks.append(project_dir)
        return real(project_dir)

    monkeypatch.setattr(file_index_module, "_fallback_list", counting)
    service = FileCompletionService(project)

    await service.complete("app", limit=5)
    (project / "later.py").write_text("")
    again = await service.complete("later", limit=5)

    assert walks == [project]
    assert again.matches == [], (
        "a file added after the index is warm is not seen until it is rebuilt"
    )
