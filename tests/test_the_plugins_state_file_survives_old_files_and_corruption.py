"""``plugins.json``: what is remembered about plugins between sessions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from igni_core.core.plugins.state import PluginsState, load_state, save_state, state_path


def test_an_empty_state_has_nothing_disabled_pinned_or_sourced() -> None:
    state = PluginsState()
    assert (state.disabled, state.pins, state.group_disabled, state.sources) == ([], {}, [], {})


def test_the_state_path_follows_home_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert state_path("~/.igni-test") == tmp_path / ".igni-test" / "plugins.json"
    assert state_path(tmp_path / "d") == tmp_path / "d" / "plugins.json"


def test_every_field_round_trips_through_the_file(tmp_path: Path) -> None:
    state = PluginsState(
        disabled=["a", "ünï"],
        pins={"a": "abc123"},
        group_disabled=["blocked"],
        sources={"a": "https://h/a.git"},
    )
    save_state(state, tmp_path)
    assert load_state(tmp_path) == state
    assert json.loads((tmp_path / "plugins.json").read_text()) == state.model_dump()


def test_a_file_from_before_sources_and_group_policy_loads_with_them_empty(
    tmp_path: Path,
) -> None:
    (tmp_path / "plugins.json").write_text('{"disabled": ["a"], "pins": {"a": "abc"}}')
    state = load_state(tmp_path)
    assert (state.disabled, state.pins) == (["a"], {"a": "abc"})
    assert state.group_disabled == [] and state.sources == {}


def test_unknown_keys_in_the_file_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "plugins.json").write_text('{"disabled": ["a"], "future": 1}')
    assert load_state(tmp_path).disabled == ["a"]


def test_a_missing_file_is_an_empty_state(tmp_path: Path) -> None:
    assert load_state(tmp_path / "nowhere") == PluginsState()


@pytest.mark.parametrize("text", ["{", "", "[]", '{"disabled": "a"}', '{"pins": ["x"]}'])
def test_a_corrupt_file_is_an_empty_state_and_a_warning(
    tmp_path: Path, text: str, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "plugins.json").write_text(text)
    with caplog.at_level("WARNING"):
        assert load_state(tmp_path) == PluginsState()
    assert "Failed to parse plugins state" in caplog.text


def test_saving_creates_the_folder_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b"
    save_state(PluginsState(disabled=["x"]), target)
    assert [p.name for p in target.iterdir()] == ["plugins.json"]


def test_a_stale_temporary_file_does_not_stop_a_save(tmp_path: Path) -> None:
    (tmp_path / "plugins.json.tmp").write_text("left by a crash")
    save_state(PluginsState(disabled=["x"]), tmp_path)
    assert load_state(tmp_path).disabled == ["x"]
    assert not (tmp_path / "plugins.json.tmp").exists()


def test_a_save_replaces_the_previous_state_whole(tmp_path: Path) -> None:
    save_state(PluginsState(disabled=["x"], sources={"x": "u"}), tmp_path)
    save_state(PluginsState(pins={"y": "z"}), tmp_path)
    assert load_state(tmp_path) == PluginsState(pins={"y": "z"})
