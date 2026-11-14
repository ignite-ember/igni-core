"""Tests for ``protocol.edit_diff_computer`` — the rows an ``edit_file`` call shows.

What is worth pinning:

* the row text (prefix, right-aligned number, three spaces, content) and
  the style of each kind of row;
* where the numbering starts: the line the edit sits on in the file,
  found by the new text first and the old text second, else line 1;
* that an unreadable file never fails the diff, and a call with nothing
  to show — or a malformed one — yields no rows rather than an error.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.protocol.edit_diff_computer import (
    DELETE_STYLE,
    INSERT_STYLE,
    DiffRow,
    EditDiffComputer,
    LocalFileReader,
)


class _Files:
    """An in-memory FileReader; a missing path raises like a real read."""

    def __init__(self, files: dict[str, str] | None = None, error: Exception | None = None):
        self.files = files or {}
        self.error = error
        self.reads: list[str] = []

    def read(self, file_path: str) -> str:
        self.reads.append(file_path)
        if self.error is not None:
            raise self.error
        return self.files[file_path]


def _compute(args, files: dict[str, str] | None = None):
    return EditDiffComputer(file_reader=_Files(files)).compute(args)


def _as_tuples(rows):
    return [row.as_tuple() for row in rows]


class TestNothingToShow:
    @pytest.mark.parametrize("args", [None, {}, [], "edit", 7])
    def test_missing_empty_or_non_dict_args_give_no_rows(self, args):
        assert _compute(args) is None

    def test_both_strings_empty_gives_no_rows(self):
        assert _compute({"old_string": "", "new_string": "", "file_path": "a.py"}) is None

    def test_both_strings_absent_gives_no_rows(self):
        assert _compute({"file_path": "a.py"}) is None

    def test_blank_lines_are_still_shown(self):
        # An edit that only adds a blank line is still an edit to draw.
        rows = _compute({"old_string": "\n", "new_string": "\n\n"})
        assert _as_tuples(rows) == [("     1   ", ""), ("+    2   ", INSERT_STYLE)]

    def test_a_null_string_is_nothing_not_a_crash(self):
        rows = _compute({"old_string": "a", "new_string": None})
        assert _as_tuples(rows) == [("-    1   a", DELETE_STYLE)]

    def test_a_null_old_string_is_a_pure_insert(self):
        rows = _compute({"old_string": None, "new_string": "a"})
        assert _as_tuples(rows) == [("+    1   a", INSERT_STYLE)]

    @pytest.mark.parametrize(
        "args",
        [
            {"old_string": 5, "new_string": "a"},
            {"old_string": "a", "new_string": ["a"]},
            {"old_string": 1, "new_string": 2},
        ],
    )
    def test_strings_that_are_not_text_give_no_rows_instead_of_failing(self, args):
        assert _compute(args) is None

    def test_a_file_path_that_is_not_text_is_not_opened(self):
        reader = _Files({"x": "a\n"})
        rows = EditDiffComputer(file_reader=reader).compute(
            {"old_string": "a", "new_string": "b", "file_path": 3}
        )
        assert reader.reads == []
        assert [r.text for r in rows] == ["-    1   a", "+    1   b"]


class TestRows:
    def test_a_replace_lists_every_delete_before_the_inserts(self):
        rows = _compute({"old_string": "a\nb", "new_string": "x\ny"})
        assert _as_tuples(rows) == [
            ("-    1   a", DELETE_STYLE),
            ("-    2   b", DELETE_STYLE),
            ("+    1   x", INSERT_STYLE),
            ("+    2   y", INSERT_STYLE),
        ]

    def test_unchanged_lines_are_context_with_no_style(self):
        rows = _compute({"old_string": "a\nb\nc", "new_string": "a\nB\nc"})
        assert _as_tuples(rows) == [
            ("     1   a", ""),
            ("-    2   b", DELETE_STYLE),
            ("+    2   B", INSERT_STYLE),
            ("     3   c", ""),
        ]

    def test_a_pure_insert_has_only_inserted_rows(self):
        rows = _compute({"old_string": "", "new_string": "a\nb"})
        assert _as_tuples(rows) == [
            ("+    1   a", INSERT_STYLE),
            ("+    2   b", INSERT_STYLE),
        ]

    def test_a_pure_delete_has_only_deleted_rows(self):
        rows = _compute({"old_string": "a\nb", "new_string": ""})
        assert _as_tuples(rows) == [
            ("-    1   a", DELETE_STYLE),
            ("-    2   b", DELETE_STYLE),
        ]

    def test_identical_strings_are_all_context(self):
        rows = _compute({"old_string": "a\nb", "new_string": "a\nb"})
        assert _as_tuples(rows) == [("     1   a", ""), ("     2   b", "")]

    def test_an_insert_in_the_middle_advances_only_the_new_counter(self):
        rows = _compute({"old_string": "a\nc", "new_string": "a\nb\nc"})
        assert [r.text for r in rows] == ["     1   a", "+    2   b", "     3   c"]

    def test_a_delete_in_the_middle_advances_only_the_old_counter(self):
        rows = _compute({"old_string": "a\nb\nc", "new_string": "a\nc"})
        # Context rows are numbered from the new side: c is line 2 there.
        assert [r.text for r in rows] == ["     1   a", "-    2   b", "     2   c"]

    def test_the_palette_is_the_documented_one(self):
        assert DELETE_STYLE == "#ff6b6b on #3d0000"
        assert INSERT_STYLE == "#69db7c on #003d00"

    def test_crlf_and_lone_cr_break_lines(self):
        rows = _compute({"old_string": "a\r\nb\rc", "new_string": ""})
        assert [r.text for r in rows] == ["-    1   a", "-    2   b", "-    3   c"]

    def test_unicode_content_is_kept(self):
        rows = _compute({"old_string": "héllo 世界", "new_string": "x"})
        assert rows[0].text == "-    1   héllo 世界"


class TestStartLine:
    def test_numbering_starts_at_the_line_the_new_text_sits_on(self):
        files = {"a.py": "one\ntwo\nthree\nfour\n"}
        rows = _compute(
            {"file_path": "a.py", "old_string": "old", "new_string": "three"},
            files,
        )
        assert rows[-1].text == "+    3   three"

    def test_the_old_text_is_looked_up_when_the_new_is_not_in_the_file(self):
        # A live edit: the file still holds the pre-edit content.
        files = {"a.py": "one\ntwo\nthree\nfour\n"}
        rows = _compute(
            {"file_path": "a.py", "old_string": "four", "new_string": "FOUR"},
            files,
        )
        assert [r.text for r in rows] == ["-    4   four", "+    4   FOUR"]

    def test_the_new_text_wins_when_both_are_in_the_file(self):
        files = {"a.py": "old\nx\nnew\n"}
        rows = _compute(
            {"file_path": "a.py", "old_string": "old", "new_string": "new"},
            files,
        )
        assert rows[0].text == "-    3   old"

    def test_the_first_occurrence_is_used(self):
        files = {"a.py": "x\nhit\nhit\n"}
        rows = _compute({"file_path": "a.py", "old_string": "", "new_string": "hit"}, files)
        assert rows[0].text == "+    2   hit"

    def test_neither_text_in_the_file_starts_at_one(self):
        rows = _compute(
            {"file_path": "a.py", "old_string": "p", "new_string": "q"},
            {"a.py": "unrelated\n"},
        )
        assert [r.text for r in rows] == ["-    1   p", "+    1   q"]

    def test_a_pure_delete_is_found_by_its_old_text(self):
        rows = _compute(
            {"file_path": "a.py", "old_string": "two", "new_string": ""},
            {"a.py": "one\ntwo\n"},
        )
        assert rows[0].text == "-    2   two"

    def test_no_file_path_never_reads_a_file(self):
        reader = _Files({"": "x"})
        EditDiffComputer(file_reader=reader).compute({"old_string": "a", "new_string": "b"})
        assert reader.reads == []

    @pytest.mark.parametrize("error", [FileNotFoundError("x"), PermissionError("x"), OSError("x")])
    def test_a_reader_that_fails_starts_at_one(self, error):
        rows = EditDiffComputer(file_reader=_Files(error=error)).compute(
            {"file_path": "a.py", "old_string": "a", "new_string": "b"}
        )
        assert [r.text for r in rows] == ["-    1   a", "+    1   b"]

    def test_line_numbers_over_9999_widen_the_column(self):
        content = "x\n" * 10000 + "target\n"
        rows = _compute(
            {"file_path": "big.py", "old_string": "", "new_string": "target"},
            {"big.py": content},
        )
        assert rows[0].text == "+ 10001   target"


class TestLocalFileReader:
    def test_reads_a_real_file_through_the_default_computer(self, tmp_path: Path):
        f = tmp_path / "a.py"
        f.write_text("one\ntwo\nthree\n")
        rows = EditDiffComputer().compute(
            {"file_path": str(f), "old_string": "two", "new_string": "TWO"}
        )
        assert [r.text for r in rows] == ["-    2   two", "+    2   TWO"]

    def test_returns_the_text(self, tmp_path: Path):
        f = tmp_path / "a.txt"
        f.write_text("hello\n")
        assert LocalFileReader().read(str(f)) == "hello\n"

    def test_raises_for_a_missing_file(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            LocalFileReader().read(str(tmp_path / "nope"))

    def test_a_directory_or_undecodable_file_starts_at_one(self, tmp_path: Path):
        bad = tmp_path / "bin"
        bad.write_bytes(b"\xff\xfe\x00\x80" * 8)
        for path in (tmp_path, bad):
            rows = EditDiffComputer().compute(
                {"file_path": str(path), "old_string": "a", "new_string": "b"}
            )
            assert [r.text for r in rows] == ["-    1   a", "+    1   b"]


class TestDiffRow:
    def test_tuple_round_trip(self):
        row = DiffRow.from_tuple(("+    1   a", INSERT_STYLE))
        assert (row.text, row.style) == ("+    1   a", INSERT_STYLE)
        assert row.as_tuple() == ("+    1   a", INSERT_STYLE)

    def test_style_defaults_to_empty(self):
        assert DiffRow(text="x").style == ""
