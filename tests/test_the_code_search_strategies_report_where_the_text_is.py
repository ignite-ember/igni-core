"""What each search strategy reports for a snippet: where, how far, what preview,
what it skips, and how it is chosen."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from igni_core.backend import search_strategies
from igni_core.backend.search_strategies import (
    PythonWalkSearchStrategy,
    RgSearchStrategy,
    SearchStrategy,
)

needs_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _rg() -> RgSearchStrategy:
    return RgSearchStrategy(shutil.which("rg") or "rg")


def _lines(snippet: str) -> int:
    return snippet.count("\n") + 1


# ── which strategy is picked ────────────────────────────────────────


def test_ripgrep_is_picked_when_it_is_on_the_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_strategies.shutil, "which", lambda name: "/bin/rg")

    assert isinstance(SearchStrategy.pick(), RgSearchStrategy)


def test_the_walk_is_picked_when_ripgrep_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_strategies.shutil, "which", lambda name: None)

    assert isinstance(SearchStrategy.pick(), PythonWalkSearchStrategy)


# ── the pure-Python walk ────────────────────────────────────────────


def test_the_walk_reports_path_line_end_line_and_a_trimmed_preview(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    _write(root, "pkg/mod.py", "import os\n\n    value = compute(1)\n    more = 2\n")
    snippet = "value = compute(1)\n    more = 2"

    result = PythonWalkSearchStrategy().search(root, snippet, _lines(snippet), 20)

    [match] = result.matches
    assert (match.path, match.line, match.end_line) == (os.path.join("pkg", "mod.py"), 3, 4)
    assert match.preview == "value = compute(1)"
    assert result.truncated is False


def test_the_walk_reports_a_file_once_at_its_first_occurrence(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    _write(root, "a.txt", "x\nneedle here\nneedle here\n")

    result = PythonWalkSearchStrategy().search(root, "needle here", 1, 20)

    assert [m.line for m in result.matches] == [2]


def test_the_walk_skips_noise_directories(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    for skipped in (".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"):
        _write(root, f"{skipped}/f.txt", "needle text\n")
    _write(root, "src/f.txt", "needle text\n")

    result = PythonWalkSearchStrategy().search(root, "needle text", 1, 20)

    assert [m.path for m in result.matches] == [os.path.join("src", "f.txt")]


def test_the_walk_takes_the_skipped_directories_it_is_given(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    _write(root, "vendor/f.txt", "needle text\n")
    _write(root, "node_modules/f.txt", "needle text\n")

    result = PythonWalkSearchStrategy(skip_dirs=frozenset({"vendor"})).search(
        root, "needle text", 1, 20
    )

    assert [m.path for m in result.matches] == [os.path.join("node_modules", "f.txt")]


def test_the_walk_skips_a_file_over_two_mebibytes(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    _write(root, "big.txt", "needle text\n" + "x" * (2 * 1024 * 1024))
    _write(root, "edge.txt", "needle text\n" + "x" * (2 * 1024 * 1024 - 12))

    result = PythonWalkSearchStrategy().search(root, "needle text", 1, 20)

    assert [m.path for m in result.matches] == ["edge.txt"]


def test_the_walk_stops_at_the_limit_and_says_it_was_cut(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    for i in range(4):
        _write(root, f"f{i}.txt", "needle text\n")

    result = PythonWalkSearchStrategy().search(root, "needle text", 1, 2)

    assert len(result.matches) == 2
    assert result.truncated is True


def test_the_walk_reads_a_file_that_is_not_utf8_and_skips_one_it_cannot_open(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    (root / "latin.txt").write_bytes(b"\xff\xfe junk\nneedle text\n")
    (root / "dangling").symlink_to(root / "nowhere")

    result = PythonWalkSearchStrategy().search(root, "needle text", 1, 20)

    assert [(m.path, m.line) for m in result.matches] == [("latin.txt", 2)]


def test_the_walk_keeps_a_colon_in_the_preview_and_counts_lines_by_newline(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    (root / "w.txt").write_bytes(b"one\r\ntwo\r\nkey: value here\r\n")

    result = PythonWalkSearchStrategy().search(root, "key: value here", 1, 20)

    assert [(m.line, m.preview) for m in result.matches] == [(3, "key: value here")]


def test_a_match_found_through_a_link_out_of_the_root_keeps_the_raw_path(tmp_path: Path) -> None:
    root = (tmp_path / "root").resolve()
    outside = tmp_path / "outside"
    target = _write(outside, "f.txt", "needle text\n")
    root.mkdir()
    (root / "link.txt").symlink_to(target)

    result = PythonWalkSearchStrategy().search(root, "needle text", 1, 20)

    assert [m.path for m in result.matches] == [str(root / "link.txt")]


# ── ripgrep ─────────────────────────────────────────────────────────


@needs_rg
def test_ripgrep_reports_path_line_end_line_and_a_trimmed_preview(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    _write(root, "pkg/mod.py", "import os\n\n    value = compute(1)\n")

    result = _rg().search(root, "value = compute(1)", 1, 20)

    [match] = result.matches
    assert (match.path, match.line, match.end_line) == (os.path.join("pkg", "mod.py"), 3, 3)
    assert match.preview == "value = compute(1)"


@needs_rg
def test_ripgrep_reports_a_multi_line_snippet_once_not_once_per_line(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    _write(root, "a.txt", "a\nhello one\nhello two\nhello three\nz\n")
    snippet = "hello one\nhello two\nhello three"

    result = _rg().search(root, snippet, _lines(snippet), 20)

    [match] = result.matches
    assert (match.line, match.end_line, match.preview) == (2, 4, "hello one")


@needs_rg
def test_ripgrep_finds_a_snippet_that_begins_with_a_dash(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    _write(root, "notes.md", "intro\n-- hello there\n")

    result = _rg().search(root, "-- hello there", 1, 20)

    assert [(m.path, m.line) for m in result.matches] == [("notes.md", 2)]


@needs_rg
def test_ripgrep_finds_nothing_in_a_project_without_the_text(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    _write(root, "a.txt", "something else\n")

    result = _rg().search(root, "needle text", 1, 20)

    assert result.matches == []
    assert result.truncated is False


@needs_rg
def test_ripgrep_stops_at_the_limit_and_says_it_was_cut(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    for i in range(4):
        _write(root, f"f{i}.txt", "needle text\n")

    result = _rg().search(root, "needle text", 1, 2)

    assert len(result.matches) == 2
    assert result.truncated is True


@needs_rg
def test_ripgrep_leaves_out_what_the_project_ignores(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    _write(root, ".ignore", "secret.txt\n")
    _write(root, "secret.txt", "needle text\n")
    _write(root, "open.txt", "needle text\n")

    result = _rg().search(root, "needle text", 1, 20)

    assert [m.path for m in result.matches] == ["open.txt"]


def test_ripgrep_that_takes_too_long_is_a_timed_out_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    seen: dict[str, object] = {}

    def slow(cmd: list[str], **kwargs: object) -> None:
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(cmd, 10)

    monkeypatch.setattr(search_strategies.subprocess, "run", slow)

    result = RgSearchStrategy("rg").search(tmp_path, "needle text", 1, 20)

    assert seen["timeout"] == 10
    assert result.error == "search timed out"
    assert result.truncated is True


def test_ripgrep_output_that_is_not_a_match_line_is_skipped_and_outside_paths_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = (tmp_path / "root").resolve()
    root.mkdir()
    inside = root / "a.py"
    inside.write_text("x\n")
    stdout = "\n".join(
        [
            "garbage without colons",
            f"{inside}:notanumber:text",
            f"{inside}:7:  key: value  ",
            "/elsewhere/b.py:3:other",
        ]
    )

    class Done:
        pass

    done = Done()
    done.stdout = stdout  # type: ignore[attr-defined]
    monkeypatch.setattr(search_strategies.subprocess, "run", lambda *a, **k: done)

    result = RgSearchStrategy("rg").search(root, "key", 2, 20)

    assert [(m.path, m.line, m.end_line, m.preview) for m in result.matches] == [
        ("a.py", 7, 8, "key: value"),
        ("/elsewhere/b.py", 3, 4, "other"),
    ]
