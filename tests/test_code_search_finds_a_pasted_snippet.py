"""``SearchController.search_code`` — where a pasted snippet occurs in the project.

Run over a real project directory with both real strategies (``rg`` when it
is installed, and the pure-Python walk), and with a counting strategy
where the question is whether the strategy was asked at all.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from igni_core.backend.schemas_search import SearchCodeResult
from igni_core.backend.search_cache import LruSearchCache
from igni_core.backend.search_strategies import (
    PythonWalkSearchStrategy,
    RgSearchStrategy,
    SearchStrategy,
)
from igni_core.backend.server_search import SearchController

STRATEGIES = [
    pytest.param(PythonWalkSearchStrategy, id="python-walk"),
    pytest.param(
        lambda: RgSearchStrategy(shutil.which("rg") or "rg"),
        id="rg",
        marks=pytest.mark.skipif(shutil.which("rg") is None, reason="needs rg"),
    ),
]


class _Counting(SearchStrategy):
    """Asks a real strategy, and counts how often it was asked."""

    def __init__(self, inner: SearchStrategy | None = None) -> None:
        self._inner = inner or PythonWalkSearchStrategy()
        self.calls: list[tuple[str, int, int]] = []

    def search(self, project_root, snippet, snippet_lines, max_results):
        self.calls.append((snippet, snippet_lines, max_results))
        return self._inner.search(project_root, snippet, snippet_lines, max_results)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(
        "import os\n\ndef greet(name):\n    print('hello', name)\n    return name\n"
    )
    (tmp_path / "README.md").write_text("nothing to see\n")
    return tmp_path


def _controller(project: Path, strategy=None, cache=None) -> SearchController:
    return SearchController(
        SimpleNamespace(project_dir=project),  # type: ignore[arg-type]
        strategy=strategy,
        cache=cache,
    )


@pytest.mark.parametrize("make", STRATEGIES)
async def test_a_single_line_snippet_is_found_with_its_file_and_line(project, make) -> None:
    result = await _controller(project, make()).search_code("def greet(name):")

    [match] = result.matches
    assert (match.path, match.line, match.end_line) == ("pkg/mod.py", 3, 3)
    assert match.preview == "def greet(name):"
    assert (result.truncated, result.error) == (False, "")


@pytest.mark.parametrize("make", STRATEGIES)
async def test_the_snippet_is_stripped_before_it_is_matched(project, make) -> None:
    result = await _controller(project, make()).search_code("\n   def greet(name):   \n\n")

    assert [(m.path, m.line) for m in result.matches] == [("pkg/mod.py", 3)]


@pytest.mark.parametrize("make", STRATEGIES)
async def test_a_snippet_found_nowhere_gives_no_matches(project, make) -> None:
    result = await _controller(project, make()).search_code("def nobody_wrote_this():")

    assert result == SearchCodeResult()


@pytest.mark.parametrize("snippet", [None, "", "    ", "abcd", "  abcd  \n"])
async def test_a_snippet_under_five_characters_is_not_searched(project, snippet) -> None:
    strategy = _Counting()

    result = await _controller(project, strategy).search_code(snippet)  # type: ignore[arg-type]

    assert result == SearchCodeResult()
    assert strategy.calls == []


async def test_exactly_five_characters_is_searched(project) -> None:
    strategy = _Counting()

    result = await _controller(project, strategy).search_code("  print \n")

    assert len(strategy.calls) == 1
    assert [m.line for m in result.matches] == [4]


async def test_the_strategy_is_told_the_number_of_snippet_lines(project) -> None:
    strategy = _Counting()

    await _controller(project, strategy).search_code("def greet(name):\n    print('hello', name)")

    [(snippet, lines, max_results)] = strategy.calls
    assert (lines, max_results) == (2, 20)
    assert snippet == "def greet(name):\n    print('hello', name)"


async def test_an_identical_search_is_answered_from_the_cache(project) -> None:
    strategy = _Counting()
    controller = _controller(project, strategy)

    first = await controller.search_code("def greet(name):")
    second = await controller.search_code("  def greet(name):  ")

    assert second is first
    assert len(strategy.calls) == 1


async def test_a_different_limit_or_project_is_a_different_search(
    project, tmp_path_factory
) -> None:
    strategy = _Counting()
    cache = LruSearchCache()
    await _controller(project, strategy, cache).search_code("def greet(name):", max_results=5)
    await _controller(project, strategy, cache).search_code("def greet(name):", max_results=6)
    other = tmp_path_factory.mktemp("other")
    (other / "x.py").write_text("def greet(name):\n")
    result = await _controller(other, strategy, cache).search_code(
        "def greet(name):", max_results=6
    )

    assert len(strategy.calls) == 3
    assert [m.path for m in result.matches] == ["x.py"]


async def test_the_limit_truncates_the_result(project) -> None:
    for i in range(3):
        (project / f"copy{i}.py").write_text("def greet(name):\n")

    result = await _controller(project, PythonWalkSearchStrategy()).search_code(
        "def greet(name):", max_results=2
    )

    assert len(result.matches) == 2
    assert result.truncated is True


async def test_a_strategy_that_fails_fails_the_search_and_nothing_is_cached(project) -> None:
    class Broken(SearchStrategy):
        def search(self, *args):
            raise RuntimeError("walk failed")

    cache = LruSearchCache()
    with pytest.raises(RuntimeError, match="walk failed"):
        await _controller(project, Broken(), cache).search_code("def greet(name):")

    strategy = _Counting()
    result = await _controller(project, strategy, cache).search_code("def greet(name):")
    assert len(result.matches) == 1


async def test_the_default_strategy_is_the_one_picked_for_the_host(project) -> None:
    result = await _controller(project).search_code("def greet(name):")

    assert [(m.path, m.line) for m in result.matches] == [("pkg/mod.py", 3)]


@pytest.mark.parametrize("make", STRATEGIES)
async def test_a_multi_line_snippet_is_one_match_spanning_its_lines(project, make) -> None:
    result = await _controller(project, make()).search_code(
        "def greet(name):\n    print('hello', name)\n    return name"
    )

    [match] = result.matches
    assert (match.path, match.line, match.end_line) == ("pkg/mod.py", 3, 5)
    assert match.preview == "def greet(name):"


async def test_a_search_that_timed_out_is_not_remembered(project) -> None:
    class Slow(SearchStrategy):
        def __init__(self) -> None:
            self.answers = [SearchCodeResult.timed_out(), PythonWalkSearchStrategy()]

        def search(self, *args):
            answer = self.answers.pop(0)
            return answer if isinstance(answer, SearchCodeResult) else answer.search(*args)

    controller = _controller(project, Slow())

    first = await controller.search_code("def greet(name):")
    second = await controller.search_code("def greet(name):")

    assert first.error == "search timed out"
    assert (second.error, [m.line for m in second.matches]) == ("", [3])


@pytest.mark.parametrize("limit", [0, -1])
async def test_a_limit_below_one_gives_no_matches(project, limit) -> None:
    result = await _controller(project, PythonWalkSearchStrategy()).search_code(
        "def greet(name):", max_results=limit
    )

    assert result.matches == []
