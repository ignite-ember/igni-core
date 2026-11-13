"""Where the managed policy file is, what it yields, and what a settings.json lifts.

``ManagedPolicySource`` is the sysadmin's file (``platform_path`` / ``load``)
and the user's ``settings.json`` fragment (``load_json_fragment``), of which
only ``permissions`` is lifted: ``hooks`` and the rest belong to loaders
that read that file themselves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from igni_core.core.config import managed_policy
from igni_core.core.config.managed_policy import JsonFragmentResult, ManagedPolicySource
from igni_core.core.layer import Layer, layers


@pytest.fixture(autouse=True)
def _desktop_layer():
    layers.reset()
    yield
    layers.reset()


class _PlacedPolicy(Layer):
    def __init__(self, path: Path) -> None:
        self.path = path

    def managed_settings_path(self) -> Path | None:
        return self.path


def test_the_file_is_managed_settings_yaml_under_the_platform_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(managed_policy, "managed_policy_dir", lambda: tmp_path)

    assert ManagedPolicySource.platform_path() == tmp_path / "managed-settings.yaml"


def test_a_platform_with_no_managed_directory_has_no_managed_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(managed_policy, "managed_policy_dir", lambda: None)

    assert ManagedPolicySource.platform_path() is None
    assert ManagedPolicySource.load() == {}


def test_a_layer_that_places_the_policy_wins_over_the_platform_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(managed_policy, "managed_policy_dir", lambda: tmp_path / "platform")
    placed = tmp_path / "image" / "policy.yaml"
    layers.install(_PlacedPolicy(placed))
    placed.parent.mkdir()
    placed.write_text("air_gapped: true\n")

    assert ManagedPolicySource.platform_path() == placed
    assert ManagedPolicySource.load() == {"air_gapped": True}


def test_the_managed_file_loads_as_yaml_or_as_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(managed_policy, "managed_policy_dir", lambda: tmp_path)
    path = tmp_path / "managed-settings.yaml"

    path.write_text("permissions:\n  mode: dontAsk\n")
    assert ManagedPolicySource.load() == {"permissions": {"mode": "dontAsk"}}

    path.write_text(json.dumps({"permissions": {"mode": "plan"}}))
    assert ManagedPolicySource.load() == {"permissions": {"mode": "plan"}}


def test_no_file_deployed_is_an_empty_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(managed_policy, "managed_policy_dir", lambda: tmp_path)

    assert ManagedPolicySource.load() == {}


def test_a_fragment_that_is_not_there_is_missing(tmp_path: Path) -> None:
    result = ManagedPolicySource.load_json_fragment(tmp_path / "settings.json")

    assert result == JsonFragmentResult(ok=False, reason="missing")
    assert result.data == {}


def test_a_fragment_that_is_not_json_says_it_did_not_parse(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"

    for text in ("{not json", ""):
        path.write_text(text)
        result = ManagedPolicySource.load_json_fragment(path)
        assert result.ok is False
        assert (result.reason or "").startswith("parse error: ")
        assert result.data == {}

    path.write_bytes(b'{"permissions": "\xff\xfe"}')
    result = ManagedPolicySource.load_json_fragment(path)
    assert result.ok is False
    assert (result.reason or "").startswith("parse error: ")


@pytest.mark.parametrize("payload", ["[1, 2]", '"text"', "3", "null"])
def test_a_fragment_that_is_not_an_object_is_refused(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "settings.json"
    path.write_text(payload)

    assert ManagedPolicySource.load_json_fragment(path) == JsonFragmentResult(
        ok=False, reason="non-dict payload"
    )


def test_only_permissions_are_lifted_from_a_fragment(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    permissions = {"deny": ["Bash(rm -rf *)"]}
    path.write_text(
        json.dumps({"permissions": permissions, "hooks": {"PreToolUse": []}, "env": {"A": "1"}})
    )

    result = ManagedPolicySource.load_json_fragment(path)

    assert result.ok is True
    assert result.reason is None
    assert result.data == {"permissions": permissions}


def test_a_fragment_without_permissions_is_ok_and_empty(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": {}}))

    assert ManagedPolicySource.load_json_fragment(path) == JsonFragmentResult(ok=True, data={})
