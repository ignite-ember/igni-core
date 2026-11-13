"""``JsonFile``: reads never raise, writes never hide a failure.

Every JSON file the init flow touches goes through it. A file that is missing,
empty, corrupt or not text reads as ``{}``; a file that parses but does not
fit the model is told apart from one that does not parse, because a caller
that saves the result back must not overwrite what it could not read.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from igni_core.core.init.json_file import JsonFile


class _Doc(BaseModel):
    name: str = "default"
    count: int = 0


def _file(tmp_path: Path, content: str | bytes | None) -> JsonFile:
    path = tmp_path / "doc.json"
    if isinstance(content, bytes):
        path.write_bytes(content)
    elif content is not None:
        path.write_text(content)
    return JsonFile(path=path)


class TestLoad:
    def test_a_file_that_is_there_loads_as_its_object(self, tmp_path):
        assert _file(tmp_path, '{"a": 1}').load() == {"a": 1}

    @pytest.mark.parametrize("content", [None, "", "{not json", b"\xff\xfe\x00bad"])
    def test_a_missing_empty_corrupt_or_non_text_file_loads_as_empty(self, tmp_path, content):
        assert _file(tmp_path, content).load() == {}

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
    def test_a_file_that_cannot_be_read_loads_as_empty(self, tmp_path):
        jf = _file(tmp_path, '{"a": 1}')
        jf.path.chmod(0)
        try:
            assert jf.load() == {}
        finally:
            jf.path.chmod(0o600)


class TestSave:
    def test_it_writes_indent_two_json_and_a_trailing_newline(self, tmp_path):
        jf = _file(tmp_path, None)

        jf.save({"a": 1, "b": [1]})

        assert jf.path.read_text() == '{\n  "a": 1,\n  "b": [\n    1\n  ]\n}\n'

    def test_it_creates_missing_parent_directories(self, tmp_path):
        jf = JsonFile(path=tmp_path / "a" / "b" / "doc.json")

        jf.save({})

        assert json.loads(jf.path.read_text()) == {}

    def test_a_write_that_fails_raises_rather_than_losing_state_quietly(self, tmp_path):
        (tmp_path / "blocker").write_text("a file, where a directory is needed")
        jf = JsonFile(path=tmp_path / "blocker" / "doc.json")

        with pytest.raises(OSError):
            jf.save({"a": 1})

    def test_the_value_object_is_frozen(self, tmp_path):
        jf = _file(tmp_path, None)

        with pytest.raises(Exception):  # noqa: B017 - pydantic's frozen-instance error
            jf.path = tmp_path / "other.json"


class TestLoadModel:
    def test_content_that_fits_becomes_the_model(self, tmp_path):
        assert _file(tmp_path, '{"name": "x", "count": 3}').load_model(_Doc) == _Doc(
            name="x", count=3
        )

    def test_a_missing_or_unparseable_file_is_the_empty_model(self, tmp_path):
        assert _file(tmp_path, "{oops").load_model(_Doc) == _Doc()

    def test_content_that_does_not_validate_is_the_empty_model_and_names_the_path(
        self, tmp_path, caplog
    ):
        jf = _file(tmp_path, '{"count": "many"}')

        with caplog.at_level(logging.WARNING, logger="igni_core.core.init.json_file"):
            model = jf.load_model(_Doc)

        assert model == _Doc()
        assert str(jf.path) in caplog.text


class TestLoadModelIfValid:
    """The refusal that keeps a caller from overwriting a file it could not read."""

    def test_content_that_fits_becomes_the_model(self, tmp_path):
        assert _file(tmp_path, '{"name": "x"}').load_model_if_valid(_Doc) == _Doc(name="x")

    @pytest.mark.parametrize("content", [None, "", "{oops"])
    def test_a_missing_or_unparseable_file_is_still_the_empty_model(self, tmp_path, content):
        assert _file(tmp_path, content).load_model_if_valid(_Doc) == _Doc()

    def test_content_that_does_not_validate_is_none_not_an_empty_model(self, tmp_path):
        assert _file(tmp_path, '{"count": "many"}').load_model_if_valid(_Doc) is None


class TestSaveModel:
    def test_it_saves_the_models_dump(self, tmp_path):
        jf = _file(tmp_path, None)

        jf.save_model(_Doc(name="x", count=2))

        assert json.loads(jf.path.read_text()) == {"name": "x", "count": 2}

    def test_the_callers_dump_options_decide_the_shape_on_disk(self, tmp_path):
        jf = _file(tmp_path, None)

        jf.save_model(_Doc(name="x"), exclude_defaults=True)

        assert json.loads(jf.path.read_text()) == {"name": "x"}
