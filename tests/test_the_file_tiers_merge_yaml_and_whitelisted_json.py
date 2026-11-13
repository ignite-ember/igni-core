"""``YamlTier`` and ``JsonFragmentTier``: the settings files a person writes.

YAML merges whole; ``settings.json`` contributes only its whitelisted keys
(``permissions``). A missing, empty, malformed or non-mapping file changes
nothing.
"""

from __future__ import annotations

from pathlib import Path

from igni_core.core.config.accumulator import SettingsAccumulator
from igni_core.core.config.tiers.files import JsonFragmentTier, YamlTier


def _applied(tier, payload: dict | None = None) -> dict:
    return tier.apply(SettingsAccumulator(payload=payload or {})).payload


def test_a_yaml_file_is_merged_deeply_over_what_is_there(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("permissions:\n  mode: allow\nnote: café\n", encoding="utf-8")

    merged = _applied(YamlTier(path), {"permissions": {"mode": "ask", "web_search": "deny"}})

    assert merged == {"permissions": {"mode": "allow", "web_search": "deny"}, "note": "café"}


def test_a_missing_yaml_file_changes_nothing(tmp_path: Path):
    assert _applied(YamlTier(tmp_path / "nope.yaml"), {"a": 1}) == {"a": 1}


def test_an_empty_yaml_file_changes_nothing(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("")

    assert _applied(YamlTier(path), {"a": 1}) == {"a": 1}


def test_a_yaml_file_that_is_not_a_mapping_changes_nothing(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("- one\n- two\n")

    assert _applied(YamlTier(path), {"a": 1}) == {"a": 1}


def test_a_json_fragment_lifts_its_permissions(tmp_path: Path):
    path = tmp_path / "settings.json"
    path.write_text('{"permissions": {"web_search": "deny"}}')

    merged = _applied(JsonFragmentTier(path), {"permissions": {"mode": "ask"}})

    assert merged == {"permissions": {"mode": "ask", "web_search": "deny"}}


def test_a_json_fragment_drops_keys_that_are_not_whitelisted(tmp_path: Path):
    path = tmp_path / "settings.json"
    path.write_text('{"permissions": {"mode": "allow"}, "air_gapped": false, "env": {"X": "1"}}')

    merged = _applied(JsonFragmentTier(path))

    assert merged == {"permissions": {"mode": "allow"}}


def test_a_json_fragment_with_nothing_whitelisted_changes_nothing(tmp_path: Path):
    path = tmp_path / "settings.json"
    path.write_text('{"env": {"X": "1"}}')
    accumulator = SettingsAccumulator(payload={"a": 1})

    assert JsonFragmentTier(path).apply(accumulator) is accumulator


def test_a_malformed_json_fragment_changes_nothing(tmp_path: Path):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    accumulator = SettingsAccumulator(payload={"a": 1})

    assert JsonFragmentTier(path).apply(accumulator) is accumulator


def test_a_json_fragment_that_is_not_an_object_changes_nothing(tmp_path: Path):
    path = tmp_path / "settings.json"
    path.write_text("[1, 2]")
    accumulator = SettingsAccumulator(payload={"a": 1})

    assert JsonFragmentTier(path).apply(accumulator) is accumulator


def test_a_missing_json_fragment_changes_nothing(tmp_path: Path):
    accumulator = SettingsAccumulator(payload={"a": 1})

    assert JsonFragmentTier(tmp_path / "nope.json").apply(accumulator) is accumulator
