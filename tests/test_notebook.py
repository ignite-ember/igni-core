"""Tests for tools/notebook.py — Jupyter notebook cell editing."""

import json

from igni_core.core.tools.notebook import NotebookTools


def _make_notebook(cells=None, kernel="python3"):
    """Create a minimal valid notebook dict."""
    return {
        "cells": cells or [],
        "metadata": {
            "kernelspec": {
                "display_name": kernel,
                "language": "python",
                "name": "python3",
            }
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def _code_cell(source, outputs=None):
    lines = source.split("\n")
    src_list = [line + "\n" for line in lines[:-1]] + [lines[-1]] if lines else []
    return {
        "cell_type": "code",
        "source": src_list,
        "metadata": {},
        "execution_count": 1,
        "outputs": outputs or [],
    }


def _md_cell(source):
    return {
        "cell_type": "markdown",
        "source": source,
        "metadata": {},
    }


def _write_notebook(path, nb):
    path.write_text(json.dumps(nb, indent=1))


class TestNotebookRead:
    def test_reads_notebook_summary(self, tmp_path):
        nb = _make_notebook([_code_cell("x = 1"), _md_cell("# Title")])
        path = tmp_path / "test.ipynb"
        _write_notebook(path, nb)

        tools = NotebookTools(base_dir=str(tmp_path))
        result = tools.notebook_read("test.ipynb")

        assert "2 cells" in result
        assert "[0] code" in result
        assert "[1] markdown" in result
        assert "x = 1" in result

    def test_reads_empty_notebook(self, tmp_path):
        nb = _make_notebook([])
        path = tmp_path / "empty.ipynb"
        _write_notebook(path, nb)

        tools = NotebookTools(base_dir=str(tmp_path))
        result = tools.notebook_read("empty.ipynb")
        assert "no cells" in result.lower()

    def test_error_on_missing_file(self, tmp_path):
        tools = NotebookTools(base_dir=str(tmp_path))
        result = tools.notebook_read("missing.ipynb")
        assert "Error" in result

    def test_error_on_non_notebook(self, tmp_path):
        (tmp_path / "test.py").write_text("x = 1")
        tools = NotebookTools(base_dir=str(tmp_path))
        result = tools.notebook_read("test.py")
        assert "Error" in result


class TestNotebookReadCell:
    def test_reads_code_cell(self, tmp_path):
        nb = _make_notebook([_code_cell("print('hello')")])
        path = tmp_path / "test.ipynb"
        _write_notebook(path, nb)

        tools = NotebookTools(base_dir=str(tmp_path))
        result = tools.notebook_read_cell("test.ipynb", 0)
        assert "print('hello')" in result
        assert "code" in result

    def test_reads_markdown_cell(self, tmp_path):
        nb = _make_notebook([_md_cell("# Hello")])
        path = tmp_path / "test.ipynb"
        _write_notebook(path, nb)

        tools = NotebookTools(base_dir=str(tmp_path))
        result = tools.notebook_read_cell("test.ipynb", 0)
        assert "# Hello" in result
        assert "markdown" in result

    def test_shows_outputs(self, tmp_path):
        outputs = [{"output_type": "stream", "name": "stdout", "text": ["hello\n"]}]
        nb = _make_notebook([_code_cell("print('hello')", outputs=outputs)])
        path = tmp_path / "test.ipynb"
        _write_notebook(path, nb)

        tools = NotebookTools(base_dir=str(tmp_path))
        result = tools.notebook_read_cell("test.ipynb", 0)
        assert "hello" in result
        assert "Outputs" in result

    def test_error_on_invalid_index(self, tmp_path):
        nb = _make_notebook([_code_cell("x = 1")])
        path = tmp_path / "test.ipynb"
        _write_notebook(path, nb)

        tools = NotebookTools(base_dir=str(tmp_path))
        result = tools.notebook_read_cell("test.ipynb", 5)
        assert "Error" in result
        assert "out of range" in result


class TestNotebookEditCell:
    def test_edits_cell_source(self, tmp_path):
        nb = _make_notebook([_code_cell("x = 1"), _code_cell("y = 2")])
        path = tmp_path / "test.ipynb"
        _write_notebook(path, nb)

        tools = NotebookTools(base_dir=str(tmp_path))
        result = tools.notebook_edit_cell("test.ipynb", 0, "x = 42")
        assert "Successfully edited" in result

        # Verify only cell 0 changed
        edited = json.loads(path.read_text())
        src0 = "".join(edited["cells"][0]["source"])
        src1 = "".join(edited["cells"][1]["source"])
        assert src0 == "x = 42"
        assert src1 == "y = 2"

    def test_clears_outputs_on_edit(self, tmp_path):
        outputs = [{"output_type": "stream", "name": "stdout", "text": ["1\n"]}]
        nb = _make_notebook([_code_cell("x = 1", outputs=outputs)])
        path = tmp_path / "test.ipynb"
        _write_notebook(path, nb)

        tools = NotebookTools(base_dir=str(tmp_path))
        tools.notebook_edit_cell("test.ipynb", 0, "x = 2")

        edited = json.loads(path.read_text())
        assert edited["cells"][0]["outputs"] == []
        assert edited["cells"][0]["execution_count"] is None

    def test_preserves_metadata(self, tmp_path):
        nb = _make_notebook([_code_cell("x = 1")])
        nb["metadata"]["custom"] = "value"
        path = tmp_path / "test.ipynb"
        _write_notebook(path, nb)

        tools = NotebookTools(base_dir=str(tmp_path))
        tools.notebook_edit_cell("test.ipynb", 0, "x = 2")

        edited = json.loads(path.read_text())
        assert edited["metadata"]["custom"] == "value"
        assert edited["nbformat"] == 4


class TestNotebookAddCell:
    def test_adds_cell_at_beginning(self, tmp_path):
        nb = _make_notebook([_code_cell("x = 1")])
        path = tmp_path / "test.ipynb"
        _write_notebook(path, nb)

        tools = NotebookTools(base_dir=str(tmp_path))
        result = tools.notebook_add_cell("test.ipynb", 0, "markdown", "# New")
        assert "Successfully added" in result

        edited = json.loads(path.read_text())
        assert len(edited["cells"]) == 2
        assert edited["cells"][0]["cell_type"] == "markdown"

    def test_appends_cell(self, tmp_path):
        nb = _make_notebook([_code_cell("x = 1")])
        path = tmp_path / "test.ipynb"
        _write_notebook(path, nb)

        tools = NotebookTools(base_dir=str(tmp_path))
        result = tools.notebook_add_cell("test.ipynb", -1, "code", "y = 2")
        assert "Successfully added" in result

        edited = json.loads(path.read_text())
        assert len(edited["cells"]) == 2
        src = "".join(edited["cells"][1]["source"])
        assert src == "y = 2"

    def test_code_cell_has_outputs_field(self, tmp_path):
        nb = _make_notebook([])
        path = tmp_path / "test.ipynb"
        _write_notebook(path, nb)

        tools = NotebookTools(base_dir=str(tmp_path))
        tools.notebook_add_cell("test.ipynb", 0, "code", "x = 1")

        edited = json.loads(path.read_text())
        assert "outputs" in edited["cells"][0]
        assert "execution_count" in edited["cells"][0]

    def test_error_on_invalid_type(self, tmp_path):
        nb = _make_notebook([])
        path = tmp_path / "test.ipynb"
        _write_notebook(path, nb)

        tools = NotebookTools(base_dir=str(tmp_path))
        result = tools.notebook_add_cell("test.ipynb", 0, "invalid", "x")
        assert "Error" in result


class TestNotebookRemoveCell:
    def test_removes_cell(self, tmp_path):
        nb = _make_notebook([_code_cell("x = 1"), _code_cell("y = 2"), _code_cell("z = 3")])
        path = tmp_path / "test.ipynb"
        _write_notebook(path, nb)

        tools = NotebookTools(base_dir=str(tmp_path))
        result = tools.notebook_remove_cell("test.ipynb", 1)
        assert "Successfully removed" in result

        edited = json.loads(path.read_text())
        assert len(edited["cells"]) == 2
        src0 = "".join(edited["cells"][0]["source"])
        src1 = "".join(edited["cells"][1]["source"])
        assert src0 == "x = 1"
        assert src1 == "z = 3"

    def test_error_on_invalid_index(self, tmp_path):
        nb = _make_notebook([_code_cell("x = 1")])
        path = tmp_path / "test.ipynb"
        _write_notebook(path, nb)

        tools = NotebookTools(base_dir=str(tmp_path))
        result = tools.notebook_remove_cell("test.ipynb", 5)
        assert "Error" in result


class TestNotebookMalformedFiles:
    """A notebook the toolkit cannot parse comes back as an error string."""

    def _tools(self, tmp_path):
        return NotebookTools(base_dir=str(tmp_path))

    def test_invalid_json_is_reported(self, tmp_path):
        (tmp_path / "bad.ipynb").write_text("{not json")

        result = self._tools(tmp_path).notebook_read("bad.ipynb")

        assert result.startswith("Error: Invalid notebook JSON:")

    def test_a_json_list_is_not_a_notebook(self, tmp_path):
        (tmp_path / "list.ipynb").write_text("[]")

        result = self._tools(tmp_path).notebook_read("list.ipynb")

        assert result.startswith("Error: Invalid notebook structure:")

    def test_a_cell_without_cell_type_is_reported(self, tmp_path):
        nb = _make_notebook([{"source": "x"}])
        _write_notebook(tmp_path / "nt.ipynb", nb)

        result = self._tools(tmp_path).notebook_read_cell("nt.ipynb", 0)

        assert result.startswith("Error: Invalid notebook structure:")

    def test_null_metadata_is_reported(self, tmp_path):
        nb = _make_notebook()
        nb["metadata"] = None
        _write_notebook(tmp_path / "nm.ipynb", nb)

        result = self._tools(tmp_path).notebook_read("nm.ipynb")

        assert result.startswith("Error: Invalid notebook structure:")

    def test_null_cells_is_reported(self, tmp_path):
        nb = _make_notebook()
        nb["cells"] = None
        _write_notebook(tmp_path / "nc.ipynb", nb)

        result = self._tools(tmp_path).notebook_remove_cell("nc.ipynb", 0)

        assert result.startswith("Error: Invalid notebook structure:")

    def test_non_utf8_file_is_reported(self, tmp_path):
        (tmp_path / "bin.ipynb").write_bytes(b"\xff\xfe\x00bad")

        result = self._tools(tmp_path).notebook_read("bin.ipynb")

        assert result.startswith("Error: Cannot read notebook")

    def test_a_directory_named_like_a_notebook_is_reported(self, tmp_path):
        (tmp_path / "dir.ipynb").mkdir()

        result = self._tools(tmp_path).notebook_read("dir.ipynb")

        assert result.startswith("Error: Cannot read notebook")

    def test_malformed_file_is_left_untouched_by_an_edit(self, tmp_path):
        path = tmp_path / "list.ipynb"
        path.write_text("[]")

        result = self._tools(tmp_path).notebook_add_cell("list.ipynb", -1, "code", "x")

        assert result.startswith("Error: Invalid notebook structure:")
        assert path.read_text() == "[]"


class TestNotebookUnwritable:
    """A notebook that cannot be written back is reported, not raised."""

    def _readonly(self, tmp_path, monkeypatch):
        import pathlib

        path = tmp_path / "ro.ipynb"
        _write_notebook(path, _make_notebook([_code_cell("x = 1")]))
        real = pathlib.Path.write_text

        def refuse(self, *args, **kwargs):
            if self == path:
                raise PermissionError(13, "Permission denied")
            return real(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "write_text", refuse)
        return path

    def test_edit_reports_the_failed_write(self, tmp_path, monkeypatch):
        self._readonly(tmp_path, monkeypatch)

        result = NotebookTools(base_dir=str(tmp_path)).notebook_edit_cell("ro.ipynb", 0, "y")

        assert result.startswith("Error: Cannot write notebook")
        assert "Successfully" not in result

    def test_add_reports_the_failed_write(self, tmp_path, monkeypatch):
        self._readonly(tmp_path, monkeypatch)

        result = NotebookTools(base_dir=str(tmp_path)).notebook_add_cell(
            "ro.ipynb", -1, "code", "y"
        )

        assert result.startswith("Error: Cannot write notebook")

    def test_remove_reports_the_failed_write(self, tmp_path, monkeypatch):
        self._readonly(tmp_path, monkeypatch)

        result = NotebookTools(base_dir=str(tmp_path)).notebook_remove_cell("ro.ipynb", 0)

        assert result.startswith("Error: Cannot write notebook")


class TestNotebookRoundTrip:
    """What an edit writes back, byte for byte."""

    def test_vendor_top_level_keys_survive_an_edit(self, tmp_path):
        nb = _make_notebook([_code_cell("x = 1")])
        nb["widgets"] = {"state": [1, 2]}
        path = tmp_path / "v.ipynb"
        _write_notebook(path, nb)

        NotebookTools(base_dir=str(tmp_path)).notebook_edit_cell("v.ipynb", 0, "y = 2")

        saved = json.loads(path.read_text())
        assert saved["widgets"] == {"state": [1, 2]}

    def test_a_vendor_key_named_path_does_not_break_the_edit(self, tmp_path):
        nb = _make_notebook([_code_cell("x = 1")])
        nb["path"] = "colab/origin.ipynb"
        path = tmp_path / "p.ipynb"
        _write_notebook(path, nb)

        result = NotebookTools(base_dir=str(tmp_path)).notebook_edit_cell("p.ipynb", 0, "y")

        assert result.startswith("Successfully edited cell [0]")
        saved = json.loads(path.read_text())
        assert saved["path"] == "colab/origin.ipynb"
        assert saved["cells"][0]["source"] == ["y"]

    def test_saved_bytes_use_one_space_indent_unescaped_unicode_and_a_final_newline(self, tmp_path):
        nb = _make_notebook([_md_cell("café")])
        path = tmp_path / "u.ipynb"
        _write_notebook(path, nb)

        NotebookTools(base_dir=str(tmp_path)).notebook_edit_cell("u.ipynb", 0, "naïve ☃")

        text = path.read_text(encoding="utf-8")
        assert "naïve ☃" in text
        assert "\\u" not in text
        assert text.endswith("}\n")
        assert text.startswith('{\n "cells": [')
        assert list(json.loads(text)) == ["cells", "metadata", "nbformat", "nbformat_minor"]

    def test_defaults_for_missing_nbformat_keys(self, tmp_path):
        path = tmp_path / "d.ipynb"
        path.write_text(json.dumps({"cells": []}))

        NotebookTools(base_dir=str(tmp_path)).notebook_add_cell("d.ipynb", -1, "raw", "r")

        saved = json.loads(path.read_text())
        assert (saved["nbformat"], saved["nbformat_minor"], saved["metadata"]) == (4, 5, {})

    def test_markdown_and_raw_cells_are_written_without_outputs(self, tmp_path):
        nb = _make_notebook([_md_cell("m")])
        nb["cells"][0]["outputs"] = [{"output_type": "stream", "text": "x"}]
        nb["cells"][0]["execution_count"] = 3
        path = tmp_path / "m.ipynb"
        _write_notebook(path, nb)

        NotebookTools(base_dir=str(tmp_path)).notebook_add_cell("m.ipynb", -1, "raw", "r")

        cells = json.loads(path.read_text())["cells"]
        assert set(cells[0]) == {"cell_type", "metadata", "source"}
        assert set(cells[1]) == {"cell_type", "metadata", "source"}

    def test_cell_vendor_keys_survive_and_code_cells_keep_their_shape(self, tmp_path):
        cell = _code_cell("x")
        cell["id"] = "abc123"
        nb = _make_notebook([cell])
        path = tmp_path / "c.ipynb"
        _write_notebook(path, nb)

        NotebookTools(base_dir=str(tmp_path)).notebook_edit_cell("c.ipynb", 0, "z")

        saved = json.loads(path.read_text())["cells"][0]
        assert saved["id"] == "abc123"
        assert saved["execution_count"] is None
        assert saved["outputs"] == []


class TestNotebookToolkit:
    def test_toolkit_and_function_names(self):
        tools = NotebookTools(base_dir="/tmp")

        assert tools.name == "igni_notebook"
        assert sorted(tools.functions) == [
            "notebook_add_cell",
            "notebook_edit_cell",
            "notebook_read",
            "notebook_read_cell",
            "notebook_remove_cell",
        ]

    def test_base_dir_defaults_to_the_working_directory(self, tmp_path, monkeypatch):
        _write_notebook(tmp_path / "cwd.ipynb", _make_notebook([_md_cell("hi")]))
        # conftest redirects ``Path.cwd``; point it at the notebook's folder.
        monkeypatch.setattr("pathlib.Path.cwd", lambda: tmp_path)

        tools = NotebookTools()

        assert tools.base_dir == tmp_path
        assert "1 cells" in tools.notebook_read("cwd.ipynb")

    def test_absolute_paths_ignore_base_dir(self, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        path = elsewhere / "abs.ipynb"
        _write_notebook(path, _make_notebook([_md_cell("hi")]))

        result = NotebookTools(base_dir=str(tmp_path / "other")).notebook_read(str(path))

        assert result.startswith(f"Notebook: {path} (1 cells")

    def test_requires_confirmation_marks_only_the_named_functions(self):
        tools = NotebookTools(
            base_dir="/tmp",
            requires_confirmation_tools=["notebook_edit_cell", "notebook_remove_cell", "nope"],
        )

        flagged = {n for n, f in tools.functions.items() if f.requires_confirmation}
        assert flagged == {"notebook_edit_cell", "notebook_remove_cell"}
        assert tools.requires_confirmation_tools == [
            "notebook_edit_cell",
            "notebook_remove_cell",
            "nope",
        ]

    def test_no_confirmation_by_default(self):
        tools = NotebookTools(base_dir="/tmp")

        assert not any(f.requires_confirmation for f in tools.functions.values())

    def test_toolkit_kwargs_reach_agno(self):
        tools = NotebookTools(base_dir="/tmp", instructions="be careful")

        assert tools.instructions == "be careful"


class TestNotebookToolMessages:
    def _nb(self, tmp_path, cells, name="m.ipynb", kernel="python3"):
        _write_notebook(tmp_path / name, _make_notebook(cells, kernel=kernel))
        return NotebookTools(base_dir=str(tmp_path))

    def test_header_names_the_kernel(self, tmp_path):
        tools = self._nb(tmp_path, [_code_cell("x")], kernel="Python 3.12")

        header = tools.notebook_read("m.ipynb").splitlines()[0]

        assert header == f"Notebook: {tmp_path / 'm.ipynb'} (1 cells, kernel: Python 3.12)"

    def test_header_says_unknown_without_a_kernelspec(self, tmp_path):
        nb = _make_notebook([_code_cell("x")])
        nb["metadata"] = {}
        _write_notebook(tmp_path / "k.ipynb", nb)

        result = NotebookTools(base_dir=str(tmp_path)).notebook_read("k.ipynb")

        assert "kernel: unknown)" in result

    def test_empty_notebook_message(self, tmp_path):
        tools = self._nb(tmp_path, [])

        assert tools.notebook_read("m.ipynb") == f"Notebook {tmp_path / 'm.ipynb'} has no cells."

    def test_success_messages(self, tmp_path):
        tools = self._nb(tmp_path, [_code_cell("a"), _md_cell("b")])
        path = tmp_path / "m.ipynb"

        assert (
            tools.notebook_edit_cell("m.ipynb", 1, "c") == f"Successfully edited cell [1] in {path}"
        )
        assert (
            tools.notebook_add_cell("m.ipynb", 0, "raw", "r")
            == f"Successfully added raw cell at [0] in {path}"
        )
        assert (
            tools.notebook_add_cell("m.ipynb", -1, "code", "r")
            == f"Successfully added code cell at [3] in {path}"
        )
        assert (
            tools.notebook_remove_cell("m.ipynb", 0)
            == f"Successfully removed raw cell [0] from {path}"
        )

    def test_add_out_of_range_names_the_inclusive_upper_bound_and_keeps_the_file(self, tmp_path):
        tools = self._nb(tmp_path, [_code_cell("a")])
        before = (tmp_path / "m.ipynb").read_text()

        for bad in (2, -2):
            assert (
                tools.notebook_add_cell("m.ipynb", bad, "code", "x")
                == f"Error: Cell index {bad} out of range (0-1)."
            )
        assert (tmp_path / "m.ipynb").read_text() == before

    def test_invalid_cell_type_is_refused_before_the_file_is_read(self, tmp_path):
        tools = NotebookTools(base_dir=str(tmp_path))

        for bad in ("Code", "", "cell"):
            assert (
                tools.notebook_add_cell("missing.ipynb", 0, bad, "x")
                == f"Error: Invalid cell_type '{bad}'. Must be 'code', 'markdown', or 'raw'."
            )

    def test_out_of_range_on_every_endpoint_keeps_the_file(self, tmp_path):
        tools = self._nb(tmp_path, [_code_cell("a")])
        before = (tmp_path / "m.ipynb").read_text()
        expected = "Error: Cell index {} out of range (0-0)."

        for bad in (-1, -2, 1, 10**9):
            assert tools.notebook_read_cell("m.ipynb", bad) == expected.format(bad)
            assert tools.notebook_edit_cell("m.ipynb", bad, "z") == expected.format(bad)
            assert tools.notebook_remove_cell("m.ipynb", bad) == expected.format(bad)
        assert (tmp_path / "m.ipynb").read_text() == before

    def test_empty_notebook_range_message_and_adds(self, tmp_path):
        tools = self._nb(tmp_path, [])

        assert tools.notebook_read_cell("m.ipynb", 0) == "Error: Cell index 0 out of range (0--1)."
        assert (
            tools.notebook_remove_cell("m.ipynb", 0) == "Error: Cell index 0 out of range (0--1)."
        )
        assert tools.notebook_add_cell("m.ipynb", 1, "code", "x") == (
            "Error: Cell index 1 out of range (0-0)."
        )
        assert "at [0]" in tools.notebook_add_cell("m.ipynb", 0, "code", "x")
        assert "at [1]" in tools.notebook_add_cell("m.ipynb", 1, "code", "y")

    def test_add_in_the_middle_and_at_the_end_index(self, tmp_path):
        tools = self._nb(tmp_path, [_md_cell("a"), _md_cell("b")])

        tools.notebook_add_cell("m.ipynb", 1, "markdown", "mid")
        tools.notebook_add_cell("m.ipynb", 3, "markdown", "end")

        sources = [
            "".join(c["source"]) if isinstance(c["source"], list) else c["source"]
            for c in json.loads((tmp_path / "m.ipynb").read_text())["cells"]
        ]
        assert sources == ["a", "mid", "b", "end"]

    def test_read_cell_without_outputs_has_no_outputs_section(self, tmp_path):
        tools = self._nb(tmp_path, [_code_cell("a\nb")])

        assert tools.notebook_read_cell("m.ipynb", 0) == "Cell [0] (code):\n\na\nb"

    def test_read_cell_lists_every_output_kind(self, tmp_path):
        outputs = [
            {"output_type": "stream", "name": "stderr", "text": ["a\n", "b"]},
            {"output_type": "display_data", "data": {"image/png": "xx"}},
            {"output_type": "error", "ename": "ValueError", "evalue": "bad"},
            {"output_type": "weird"},
        ]
        tools = self._nb(tmp_path, [_code_cell("a", outputs)])

        assert tools.notebook_read_cell("m.ipynb", 0) == (
            "Cell [0] (code):\n\na\n\n--- Outputs (4) ---\n"
            "[stream/stderr] a\nb\n"
            "[display_data] keys: ['image/png']\n"
            "[error] ValueError: bad\n"
            "[weird] (unrenderable)"
        )

    def test_read_truncates_the_preview_and_escapes_newlines(self, tmp_path):
        tools = self._nb(tmp_path, [_code_cell("x" * 130), _code_cell("a\nb"), _code_cell("")])

        lines = tools.notebook_read("m.ipynb").splitlines()

        assert lines[2] == f"[0] code (1 lines): {'x' * 120}..."
        assert lines[3] == "[1] code (2 lines): a\\nb"
        assert lines[4] == "[2] code (0 lines): "

    def test_edit_keeps_list_shape_and_splits_lines(self, tmp_path):
        tools = self._nb(tmp_path, [_code_cell("a"), _md_cell("string shaped")])

        tools.notebook_edit_cell("m.ipynb", 0, "l1\nl2\n")
        tools.notebook_edit_cell("m.ipynb", 1, "new\ntext")

        cells = json.loads((tmp_path / "m.ipynb").read_text())["cells"]
        assert cells[0]["source"] == ["l1\n", "l2\n", ""]
        assert cells[1]["source"] == "new\ntext"

    def test_added_cell_is_split_into_lines_with_empty_metadata(self, tmp_path):
        tools = self._nb(tmp_path, [])

        tools.notebook_add_cell("m.ipynb", -1, "code", "a\nb")

        cell = json.loads((tmp_path / "m.ipynb").read_text())["cells"][0]
        assert cell == {
            "cell_type": "code",
            "metadata": {},
            "source": ["a\n", "b"],
            "execution_count": None,
            "outputs": [],
        }

    def test_empty_source_is_one_empty_line(self, tmp_path):
        tools = self._nb(tmp_path, [])

        tools.notebook_add_cell("m.ipynb", -1, "markdown", "")

        cell = json.loads((tmp_path / "m.ipynb").read_text())["cells"][0]
        assert cell["source"] == [""]
        assert tools.notebook_read("m.ipynb").splitlines()[2] == "[0] markdown (0 lines): "

    def test_source_of_any_other_type_is_coerced(self, tmp_path):
        nb = _make_notebook(
            [
                {"cell_type": "markdown", "metadata": {}, "source": None},
                {"cell_type": "markdown", "metadata": {}, "source": 5},
            ]
        )
        _write_notebook(tmp_path / "s.ipynb", nb)

        result = NotebookTools(base_dir=str(tmp_path)).notebook_read("s.ipynb")

        assert result.splitlines()[2:] == ["[0] markdown (0 lines): ", "[1] markdown (1 lines): 5"]

    def test_edit_of_a_markdown_cell_leaves_other_cell_fields(self, tmp_path):
        cell = _md_cell("x")
        cell["outputs"] = [{"output_type": "stream", "text": "keep?"}]
        tools = self._nb(tmp_path, [cell])

        tools.notebook_edit_cell("m.ipynb", 0, "y")

        # Non-code cells never write outputs/execution_count.
        saved = json.loads((tmp_path / "m.ipynb").read_text())["cells"][0]
        assert "outputs" not in saved

    def test_data_output_prefers_text_plain_and_caps_long_text(self, tmp_path):
        outputs = [
            {"output_type": "execute_result", "data": {"text/plain": ["1\n", "2"], "x": 1}},
            {"output_type": "stream", "text": "y" * 600},
            {"output_type": "execute_result", "data": {"text/plain": "z" * 600}},
        ]
        tools = self._nb(tmp_path, [_code_cell("a", outputs)])

        lines = tools.notebook_read_cell("m.ipynb", 0).splitlines()

        text = "\n".join(lines)
        assert "[execute_result] 1\n2\n" in text
        assert f"[stream/stdout] {'y' * 500}\n" in text
        assert "y" * 501 not in text
        assert "z" * 501 not in text
