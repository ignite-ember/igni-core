"""Tests for the notebook package's own types: cells, outputs, results, document."""

import json

import pytest

import igni_core.core.tools.notebook as notebook
from igni_core.core.tools.notebook import (
    CellType,
    NotebookCell,
    NotebookDataOutput,
    NotebookDocument,
    NotebookErrorOutput,
    NotebookLoadResult,
    NotebookOpResult,
    NotebookOutput,
    NotebookStreamOutput,
    UnknownOutput,
)
from igni_core.core.tools.notebook.schemas import CELL_TYPES, _parse_output


def test_every_name_in_all_resolves_and_the_list_is_the_public_surface():
    assert sorted(notebook.__all__) == sorted(
        [
            "CellType",
            "NotebookCell",
            "NotebookDataOutput",
            "NotebookDocument",
            "NotebookErrorOutput",
            "NotebookLoadResult",
            "NotebookOpResult",
            "NotebookOutput",
            "NotebookStreamOutput",
            "NotebookTools",
            "UnknownOutput",
        ]
    )
    for name in notebook.__all__:
        assert getattr(notebook, name) is not None
    assert CellType is not None


def test_cell_types_are_code_markdown_raw():
    assert CELL_TYPES == ("code", "markdown", "raw")


class TestCellSource:
    def test_list_source_is_joined_and_string_source_returned(self):
        assert NotebookCell(cell_type="code", source=["a\n", "b"]).source_text() == "a\nb"
        assert NotebookCell(cell_type="code", source="a\nb").source_text() == "a\nb"

    def test_set_source_text_keeps_a_list_shape(self):
        cell = NotebookCell(cell_type="code", source=["old"])

        cell.set_source_text("a\nb\n")

        assert cell.source == ["a\n", "b\n", ""]

    def test_set_source_text_keeps_a_string_shape(self):
        cell = NotebookCell(cell_type="markdown", source="old")

        cell.set_source_text("a\nb")

        assert cell.source == "a\nb"

    def test_source_defaults_to_a_list_shape(self):
        cell = NotebookCell(cell_type="raw")

        cell.set_source_text("x\ny")

        assert cell.source == ["x\n", "y"]

    def test_none_and_other_sources_are_coerced(self):
        assert NotebookCell(cell_type="raw", source=None).source == ""
        assert NotebookCell(cell_type="raw", source=12).source == "12"

    @pytest.mark.parametrize(
        ("source", "expected"),
        [("a\r\nb", ["a\r\n"[:-1] + "\n", "b"]), ("only\n", ["only\n", ""])],
    )
    def test_new_splits_on_newlines_only(self, source, expected):
        cell = NotebookCell.new("code", source)

        # A carriage return stays in the text; only "\n" splits.
        assert "".join(cell.source) == source
        assert len(cell.source) == len(expected)


class TestCellSummary:
    def test_format_and_line_count(self):
        cell = NotebookCell(cell_type="code", source="a\nb\nc")

        assert cell.summary(3) == "[3] code (3 lines): a\\nb\\nc"

    def test_empty_source_has_zero_lines_and_trailing_newline_counts_one_more(self):
        assert NotebookCell(cell_type="raw", source="").summary(0) == "[0] raw (0 lines): "
        assert NotebookCell(cell_type="raw", source="a\n").summary(0) == "[0] raw (2 lines): a\\n"

    def test_preview_is_cut_at_120_characters_with_an_ellipsis(self):
        exact = NotebookCell(cell_type="code", source="x" * 120).summary(0)
        over = NotebookCell(cell_type="code", source="x" * 121).summary(0)

        assert exact.endswith(": " + "x" * 120)
        assert over.endswith(": " + "x" * 120 + "...")

    def test_escaping_happens_inside_the_120_window(self):
        src = "\n" * 130

        assert NotebookCell(cell_type="code", source=src).summary(0).endswith("\\n" * 120 + "...")


class TestCellOutputsAndWire:
    def test_clear_outputs_only_touches_code_cells(self):
        code = NotebookCell(cell_type="code", outputs=[{"output_type": "x"}], execution_count=4)
        md = NotebookCell(cell_type="markdown", outputs=[{"output_type": "x"}], execution_count=4)

        code.clear_outputs()
        md.clear_outputs()

        assert (code.outputs, code.execution_count) == ([], None)
        assert (md.outputs, md.execution_count) == ([{"output_type": "x"}], 4)

    def test_new_builds_each_kind(self):
        for kind in CELL_TYPES:
            cell = NotebookCell.new(kind, "a")
            assert cell.cell_type == kind
            assert cell.metadata == {}
            assert cell.outputs == []
            assert cell.execution_count is None

    def test_to_wire_for_a_code_cell(self):
        cell = NotebookCell(cell_type="code", source=["a"], execution_count=2, outputs=[{"a": 1}])

        assert cell.to_wire() == {
            "cell_type": "code",
            "metadata": {},
            "source": ["a"],
            "execution_count": 2,
            "outputs": [{"a": 1}],
        }

    def test_to_wire_for_other_kinds_drops_outputs_and_keeps_vendor_keys(self):
        cell = NotebookCell.model_validate(
            {"cell_type": "raw", "source": "r", "outputs": [{"a": 1}], "id": "k1"}
        )

        assert cell.to_wire() == {"cell_type": "raw", "metadata": {}, "source": "r", "id": "k1"}

    def test_a_vendor_key_never_replaces_a_wire_field(self):
        cell = NotebookCell(cell_type="markdown", source="a")
        object.__setattr__(cell, "__pydantic_extra__", {"source": "other", "extra": 1})

        assert cell.to_wire()["source"] == "a"
        assert cell.to_wire()["extra"] == 1

    def test_malformed_cells_are_rejected(self):
        from pydantic import ValidationError

        for bad in (
            {"source": "x"},
            {"cell_type": "code", "execution_count": 1.5},
            {"cell_type": "code", "outputs": ["text"]},
        ):
            with pytest.raises(ValidationError):
                NotebookCell.model_validate(bad)

    def test_an_unknown_cell_type_is_loaded_as_is_and_written_without_outputs(self):
        cell = NotebookCell.model_validate({"cell_type": "heading", "source": "h"})

        assert cell.summary(0) == "[0] heading (1 lines): h"
        assert "outputs" not in cell.to_wire()


class TestOutputs:
    def test_parse_routes_each_output_type(self):
        assert isinstance(_parse_output({"output_type": "stream"}), NotebookStreamOutput)
        assert isinstance(_parse_output({"output_type": "execute_result"}), NotebookDataOutput)
        assert isinstance(_parse_output({"output_type": "display_data"}), NotebookDataOutput)
        assert isinstance(_parse_output({"output_type": "error"}), NotebookErrorOutput)
        assert isinstance(_parse_output({"output_type": "other"}), UnknownOutput)
        assert isinstance(_parse_output({}), UnknownOutput)

    def test_stream_preview(self):
        assert _parse_output({"output_type": "stream", "text": ["a", "b"]}).format_preview() == (
            "[stream/stdout] ab"
        )
        assert _parse_output(
            {"output_type": "stream", "name": "stderr", "text": "e"}
        ).format_preview() == ("[stream/stderr] e")
        assert _parse_output({"output_type": "stream"}).format_preview() == "[stream/stdout] "

    def test_stream_preview_is_capped_at_500(self):
        out = _parse_output({"output_type": "stream", "text": ["a" * 300, "b" * 300]})

        assert out.format_preview() == "[stream/stdout] " + "a" * 300 + "b" * 200

    def test_data_preview_prefers_text_plain_string_or_list(self):
        data = {"text/plain": "t", "text/html": "<b>"}
        assert _parse_output({"output_type": "display_data", "data": data}).format_preview() == (
            "[display_data] t"
        )
        listed = {"output_type": "execute_result", "data": {"text/plain": ["a", "b"]}}
        assert _parse_output(listed).format_preview() == "[execute_result] ab"

    def test_data_preview_without_text_plain_lists_the_keys(self):
        out = _parse_output({"output_type": "execute_result", "data": {"a": 1, "b": 2}})

        assert out.format_preview() == "[execute_result] keys: ['a', 'b']"
        assert _parse_output({"output_type": "display_data"}).format_preview() == (
            "[display_data] keys: []"
        )

    def test_data_preview_is_capped_at_500(self):
        out = _parse_output({"output_type": "execute_result", "data": {"text/plain": "z" * 501}})

        assert out.format_preview() == "[execute_result] " + "z" * 500

    def test_error_preview(self):
        out = _parse_output({"output_type": "error", "ename": "E", "evalue": "v", "traceback": []})

        assert out.format_preview() == "[error] E: v"
        assert _parse_output({"output_type": "error"}).format_preview() == "[error] : "

    def test_unknown_and_base_previews(self):
        assert _parse_output({"output_type": "vendor/x", "k": 1}).format_preview() == (
            "[vendor/x] (unrenderable)"
        )
        assert _parse_output({}).format_preview() == "[unknown] (unrenderable)"
        assert NotebookOutput(output_type="q").format_preview() == "[q] (unrenderable)"

    def test_typed_outputs_follow_the_stored_order(self):
        cell = NotebookCell(
            cell_type="code",
            outputs=[{"output_type": "error"}, {"output_type": "stream", "text": "s"}],
        )

        kinds = [type(o) for o in cell.typed_outputs()]

        assert kinds == [NotebookErrorOutput, NotebookStreamOutput]


class TestResults:
    def test_op_result_prints_its_message(self):
        result = NotebookOpResult(ok=True, message="done", path="p", cell_index=2)

        assert str(result) == "done"
        assert (result.ok, result.path, result.cell_index) == (True, "p", 2)

    def test_op_result_defaults(self):
        result = NotebookOpResult(ok=False, message="m")

        assert (result.path, result.cell_index) == ("", None)

    def test_load_result_tags(self, tmp_path):
        doc = NotebookDocument(path=tmp_path / "a.ipynb")

        good = NotebookLoadResult.success(doc)
        bad = NotebookLoadResult.fail("boom")

        assert (good.ok, good.error, good.document) == (True, "", doc)
        assert (bad.ok, bad.error, bad.document) == (False, "boom", None)


class TestDocument:
    def _doc(self, tmp_path, n=2):
        cells = [NotebookCell.new("markdown", f"c{i}") for i in range(n)]
        return NotebookDocument(path=tmp_path / "d.ipynb", cells=cells)

    def test_load_errors(self, tmp_path):
        missing = NotebookDocument.load(tmp_path / "no.ipynb")
        wrong = tmp_path / "a.txt"
        wrong.write_text("{}")

        assert missing.error == f"Error: File not found: {tmp_path / 'no.ipynb'}"
        assert NotebookDocument.load(wrong).error == f"Error: Not a notebook file: {wrong}"

    def test_load_reads_cells_metadata_and_defaults(self, tmp_path):
        path = tmp_path / "n.ipynb"
        path.write_text(json.dumps({"cells": [{"cell_type": "raw", "source": "x"}]}))

        doc = NotebookDocument.load(path).document

        assert doc is not None
        assert [c.cell_type for c in doc.cells] == ["raw"]
        assert (doc.metadata, doc.nbformat, doc.nbformat_minor) == ({}, 4, 5)

    def test_load_without_cells_key_is_an_empty_notebook(self, tmp_path):
        path = tmp_path / "n.ipynb"
        path.write_text("{}")

        doc = NotebookDocument.load(path).document

        assert doc is not None
        assert doc.cells == []

    def test_load_then_save_keeps_unknown_keys(self, tmp_path):
        path = tmp_path / "n.ipynb"
        path.write_text(json.dumps({"cells": [], "colab": {"a": 1}, "nbformat": 3}))
        doc = NotebookDocument.load(path).document
        assert doc is not None

        doc.save()

        assert json.loads(path.read_text()) == {
            "cells": [],
            "metadata": {},
            "nbformat": 3,
            "nbformat_minor": 5,
            "colab": {"a": 1},
        }

    def test_kernel_display_name(self, tmp_path):
        doc = self._doc(tmp_path)
        assert doc.kernel_display_name() == "unknown"
        doc.metadata = {"kernelspec": {"display_name": "R"}}
        assert doc.kernel_display_name() == "R"

    def test_at_and_remove_bounds(self, tmp_path):
        doc = self._doc(tmp_path)

        assert doc.at(1) is doc.cells[1]
        assert doc.at(2) is None
        assert doc.at(-1) is None
        assert doc.remove(-1) is None
        assert doc.remove(2) is None
        assert len(doc.cells) == 2
        assert doc.remove(0) is not None
        assert [c.source_text() for c in doc.cells] == ["c1"]

    def test_insert_boundaries(self, tmp_path):
        doc = self._doc(tmp_path)

        assert doc.insert(NotebookCell.new("raw", "a"), -1) == 2
        assert doc.insert(NotebookCell.new("raw", "b"), 3) == 3
        assert doc.insert(NotebookCell.new("raw", "c"), 0) == 0
        assert doc.insert(NotebookCell.new("raw", "d"), 6) == -1
        assert doc.insert(NotebookCell.new("raw", "e"), -2) == -1
        assert [c.source_text() for c in doc.cells] == ["c", "c0", "c1", "a", "b"]

    def test_out_of_range_message(self, tmp_path):
        assert self._doc(tmp_path, 3).out_of_range_message(7) == (
            "Error: Cell index 7 out of range (0-2)."
        )
        assert self._doc(tmp_path, 0).out_of_range_message(0) == (
            "Error: Cell index 0 out of range (0--1)."
        )

    def test_replace_source(self, tmp_path):
        doc = self._doc(tmp_path)
        doc.cells[0] = NotebookCell(
            cell_type="code", source="old", outputs=[{"output_type": "x"}], execution_count=1
        )

        cell = doc.replace_source(0, "new")

        assert cell is doc.cells[0]
        assert (cell.source_text(), cell.outputs, cell.execution_count) == ("new", [], None)
        assert doc.replace_source(5, "x") is None
        assert doc.replace_source(-1, "x") is None


class TestMalformedOutputs:
    def test_an_output_without_a_type_is_unrenderable_not_a_crash(self):
        out = _parse_output({"text": "x"})

        assert isinstance(out, UnknownOutput)
        assert out.format_preview() == "[unknown] (unrenderable)"

    def test_a_non_string_type_is_named_by_its_text(self):
        assert _parse_output({"output_type": 5}).format_preview() == "[5] (unrenderable)"

    def test_a_known_type_with_a_malformed_body_is_unrenderable(self):
        assert _parse_output({"output_type": "stream", "text": None}).format_preview() == (
            "[stream] (unrenderable)"
        )
        assert _parse_output({"output_type": "display_data", "data": []}).format_preview() == (
            "[display_data] (unrenderable)"
        )

    def test_reading_a_cell_with_such_outputs_still_answers(self, tmp_path):
        from igni_core.core.tools.notebook import NotebookTools

        nb = {
            "cells": [
                {
                    "cell_type": "code",
                    "source": "a",
                    "outputs": [{"text": "x"}, {"output_type": "stream", "text": None}],
                }
            ],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
        (tmp_path / "o.ipynb").write_text(json.dumps(nb))

        result = NotebookTools(base_dir=str(tmp_path)).notebook_read_cell("o.ipynb", 0)

        assert result.endswith(
            "--- Outputs (2) ---\n[unknown] (unrenderable)\n[stream] (unrenderable)"
        )
