"""Tests for ``MCPToolStateStore`` — file-backed disabled-tools state.

Pins the store's contract in isolation from ``MCPClientManager``:
* No project_dir → no-op (load returns empty, save silently drops).
* Round-trip through disk preserves the ``{server: set[tool_name]}``
  shape.
* Malformed JSON on disk degrades to empty rather than raising.
* Empty inner sets are pruned by save so the file stays clean.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from igni_core.core.mcp.tool_state import MCPToolStateStore
from igni_core.core.paths import CONFIG_DIR


class TestPath:
    def test_none_project_dir_returns_none(self):
        assert MCPToolStateStore(None).path() is None

    def test_resolves_relative_to_config_dir(self, tmp_path: Path):
        store = MCPToolStateStore(tmp_path)
        assert store.path() == tmp_path / CONFIG_DIR / "mcp-tool-state.json"


class TestLoad:
    def test_missing_returns_empty(self, tmp_path: Path):
        store = MCPToolStateStore(tmp_path)
        assert store.load() == {}

    def test_no_project_dir_returns_empty(self):
        assert MCPToolStateStore(None).load() == {}

    def test_malformed_json_returns_empty(self, tmp_path: Path):
        (tmp_path / CONFIG_DIR).mkdir()
        (tmp_path / CONFIG_DIR / "mcp-tool-state.json").write_text("{not json")
        store = MCPToolStateStore(tmp_path)
        assert store.load() == {}

    def test_valid_state_returns_sets(self, tmp_path: Path):
        (tmp_path / CONFIG_DIR).mkdir()
        (tmp_path / CONFIG_DIR / "mcp-tool-state.json").write_text(
            '{"disabled": {"srv1": ["tool_a", "tool_b"], "srv2": ["tool_c"]}}'
        )
        store = MCPToolStateStore(tmp_path)
        state = store.load()
        assert state == {"srv1": {"tool_a", "tool_b"}, "srv2": {"tool_c"}}

    def test_missing_disabled_key_returns_empty(self, tmp_path: Path):
        (tmp_path / CONFIG_DIR).mkdir()
        (tmp_path / CONFIG_DIR / "mcp-tool-state.json").write_text('{"other": "data"}')
        store = MCPToolStateStore(tmp_path)
        assert store.load() == {}


def _write(tmp_path: Path, content: str | bytes) -> MCPToolStateStore:
    (tmp_path / CONFIG_DIR).mkdir(exist_ok=True)
    target = tmp_path / CONFIG_DIR / "mcp-tool-state.json"
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content)
    return MCPToolStateStore(tmp_path)


class TestLoadOfAnOddFile:
    """A file that is not what save writes reads as "nothing disabled"."""

    def test_an_empty_file_is_nothing_disabled(self, tmp_path: Path):
        assert _write(tmp_path, "").load() == {}

    @pytest.mark.parametrize(
        "content",
        [
            "[]",
            '"text"',
            "3",
            "null",
            '{"disabled": ["a"]}',
            '{"disabled": null}',
            '{"disabled": 4}',
        ],
    )
    def test_valid_json_of_the_wrong_shape_is_nothing_disabled(self, tmp_path: Path, content: str):
        assert _write(tmp_path, content).load() == {}

    def test_a_file_that_is_not_utf8_is_nothing_disabled(self, tmp_path: Path):
        assert _write(tmp_path, b'{"disabled": {"srv": ["\xff\xfe"]}}').load() == {}

    def test_a_directory_where_the_file_belongs_is_nothing_disabled(self, tmp_path: Path):
        (tmp_path / CONFIG_DIR / "mcp-tool-state.json").mkdir(parents=True)
        assert MCPToolStateStore(tmp_path).load() == {}

    def test_a_server_whose_value_is_not_a_list_is_skipped(self, tmp_path: Path):
        store = _write(
            tmp_path, '{"disabled": {"a": "tool", "b": {"x": 1}, "c": null, "d": ["t"]}}'
        )
        assert store.load() == {"d": {"t"}}

    def test_tool_names_that_are_not_strings_are_stringified(self, tmp_path: Path):
        assert _write(tmp_path, '{"disabled": {"srv": [1, "two", true]}}').load() == {
            "srv": {"1", "two", "True"}
        }

    def test_unicode_names_survive_a_round_trip(self, tmp_path: Path):
        store = MCPToolStateStore(tmp_path)
        store.save({"serveur-é": {"outil-ü"}})
        assert store.load() == {"serveur-é": {"outil-ü"}}


class TestSave:
    def test_no_project_dir_is_noop(self):
        MCPToolStateStore(None).save({"srv": {"tool"}})  # should not raise

    def test_writes_file(self, tmp_path: Path):
        store = MCPToolStateStore(tmp_path)
        store.save({"srv": {"tool_a", "tool_b"}})
        p = tmp_path / CONFIG_DIR / "mcp-tool-state.json"
        assert p.exists()

    def test_round_trip_preserves_state(self, tmp_path: Path):
        store = MCPToolStateStore(tmp_path)
        original = {"srv1": {"tool_a", "tool_b"}, "srv2": {"tool_c"}}
        store.save(original)
        assert store.load() == original

    def test_empty_inner_sets_pruned(self, tmp_path: Path):
        # A server with no disabled tools should not appear in the
        # saved blob — keeps the file clean when a tool gets
        # re-enabled and the caller doesn't pop the empty set.
        store = MCPToolStateStore(tmp_path)
        store.save({"srv1": {"tool"}, "srv2": set()})
        payload = json.loads((tmp_path / CONFIG_DIR / "mcp-tool-state.json").read_text())
        assert payload["disabled"] == {"srv1": ["tool"]}

    def test_creates_config_dir(self, tmp_path: Path):
        store = MCPToolStateStore(tmp_path)
        assert not (tmp_path / CONFIG_DIR).exists()
        store.save({"srv": {"tool"}})
        assert (tmp_path / CONFIG_DIR).is_dir()

    def test_overwrites_existing(self, tmp_path: Path):
        store = MCPToolStateStore(tmp_path)
        store.save({"srv": {"tool_a"}})
        store.save({"srv": {"tool_b"}})
        assert store.load() == {"srv": {"tool_b"}}
