"""``ToolResultData``: the invariant that a diff cannot hide an ``Error:``
text, and how ``from_edit_diff`` builds the diff variant from rows."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.protocol.schemas.internal import ToolResultData


class _Row:
    def __init__(self, text: str, style: str) -> None:
        self._pair = (text, style)

    def as_tuple(self) -> tuple[str, str]:
        return self._pair


class TestAPlainResult:
    def test_everything_defaults_to_empty(self):
        data = ToolResultData()

        assert (data.summary, data.full_result, data.has_markup, data.diff_rows) == (
            "",
            "",
            False,
            None,
        )

    def test_text_beside_rows_is_fine_while_markup_is_off(self):
        data = ToolResultData(full_result="Error: nope", diff_rows=[("a", "b")])

        assert data.has_markup is False


class TestAMarkedUpResultMustBeAnUntouchedDiff:
    def test_markup_without_rows_is_refused(self):
        with pytest.raises(ValidationError, match="requires diff_rows"):
            ToolResultData(has_markup=True)

    def test_markup_with_an_empty_row_list_is_refused(self):
        with pytest.raises(ValidationError, match="requires diff_rows"):
            ToolResultData(has_markup=True, diff_rows=[])

    def test_markup_beside_a_result_text_is_refused(self):
        """The text would be hidden behind the diff: the v0.5.11 bug where an
        ``Error:`` disappeared."""
        with pytest.raises(ValidationError, match="requires full_result to be empty"):
            ToolResultData(has_markup=True, diff_rows=[("a", "b")], full_result="Error: x")

    def test_markup_with_rows_and_no_text_is_accepted(self):
        data = ToolResultData(has_markup=True, diff_rows=[("a", "b")])

        assert data.diff_rows == [("a", "b")]


class TestTheDiffVariantIsBuiltFromRows:
    def test_it_is_marked_up_with_no_text_and_keeps_its_summary(self):
        data = ToolResultData.from_edit_diff(summary="Edited x.py", diff_rows=[("+ a", "green")])

        assert data.has_markup is True
        assert data.full_result == ""
        assert data.summary == "Edited x.py"
        assert data.diff_rows == [("+ a", "green")]

    def test_a_typed_row_is_read_through_its_tuple(self):
        data = ToolResultData.from_edit_diff(summary="s", diff_rows=[_Row("- a", "red")])

        assert data.diff_rows == [("- a", "red")]

    def test_a_list_row_is_read_as_a_pair_of_text(self):
        data = ToolResultData.from_edit_diff(summary="s", diff_rows=[["a", "b"]])

        assert data.diff_rows == [("a", "b")]

    def test_a_longer_row_is_cut_to_its_first_two_and_written_as_text(self):
        data = ToolResultData.from_edit_diff(summary="s", diff_rows=[(1, 2, 3)])

        assert data.diff_rows == [("1", "2")]

    def test_rows_that_are_not_pairs_are_skipped_and_the_rest_keep_their_order(self):
        data = ToolResultData.from_edit_diff(
            summary="s", diff_rows=[("a", "1"), ("short",), "", 5, None, ("b", "2")]
        )

        assert data.diff_rows == [("a", "1"), ("b", "2")]

    def test_unicode_rows_survive(self):
        data = ToolResultData.from_edit_diff(summary="é", diff_rows=[("+ ☃", "g")])

        assert data.diff_rows == [("+ ☃", "g")]

    @pytest.mark.parametrize("rows", [[], [("only-one",)], [None]])
    def test_no_usable_row_is_refused_rather_than_a_diff_that_shows_nothing(self, rows):
        with pytest.raises(ValidationError, match="requires diff_rows"):
            ToolResultData.from_edit_diff(summary="s", diff_rows=rows)

    def test_rows_go_out_as_pairs_on_the_wire(self):
        data = ToolResultData.from_edit_diff(summary="s", diff_rows=[("a", "b")])

        assert data.model_dump()["diff_rows"] == [("a", "b")]
