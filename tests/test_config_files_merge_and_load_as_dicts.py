"""``DictMerger`` and ``YamlSource``: the merge and file primitives under the
settings tiers."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from igni_core.core.config.config_io import DictMerger, YamlSource


def test_override_wins_and_dicts_recurse() -> None:
    base = {"a": {"x": 1, "y": 2}, "b": 1}
    assert DictMerger.deep(base, {"a": {"y": 3, "z": 4}, "c": 5}) == {
        "a": {"x": 1, "y": 3, "z": 4},
        "b": 1,
        "c": 5,
    }


def test_a_list_is_replaced_not_appended() -> None:
    assert DictMerger.deep({"a": [1, 2]}, {"a": [3]}) == {"a": [3]}


def test_a_scalar_replaces_a_dict_and_a_dict_replaces_a_scalar() -> None:
    assert DictMerger.deep({"a": {"x": 1}}, {"a": 5}) == {"a": 5}
    assert DictMerger.deep({"a": 5}, {"a": {"x": 1}}) == {"a": {"x": 1}}


def test_an_explicit_none_overrides() -> None:
    assert DictMerger.deep({"a": 1}, {"a": None}) == {"a": None}


def test_neither_input_is_changed() -> None:
    base = {"a": {"x": 1}}
    override = {"a": {"y": 2}}
    out = DictMerger.deep(base, override)
    out["a"]["x"] = 99
    assert base == {"a": {"x": 1}}
    assert override == {"a": {"y": 2}}


def test_merge_is_the_hook_a_subclass_can_change() -> None:
    assert DictMerger().merge({"a": {"x": 1}}, {"a": {"y": 2}}) == {"a": {"x": 1, "y": 2}}

    class Appending(DictMerger):
        def merge(self, base: dict, override: dict) -> dict:
            return {**base, **override}

    assert Appending().merge({"a": {"x": 1}}, {"a": {"y": 2}}) == {"a": {"y": 2}}


def test_a_yaml_file_loads_as_its_mapping(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("name: café\nnested:\n  a: [1, 2]\n", encoding="utf-8")
    source = YamlSource(path)
    assert source.path == path
    assert source.exists
    assert source.load() == {"name": "café", "nested": {"a": [1, 2]}}


@pytest.mark.parametrize(
    "text", ["", "# only a comment\n", "- a\n- b\n", "just a string\n", "42\n"]
)
def test_a_file_that_is_not_a_mapping_loads_as_empty(tmp_path: Path, text: str) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(text)
    assert YamlSource(path).load() == {}


def test_a_missing_file_loads_as_empty(tmp_path: Path) -> None:
    source = YamlSource(tmp_path / "nope.yaml")
    assert not source.exists
    assert source.load() == {}


def test_malformed_yaml_raises_the_yaml_error(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text("a: [1, 2\n")
    with pytest.raises(yaml.YAMLError):
        YamlSource(path).load()
