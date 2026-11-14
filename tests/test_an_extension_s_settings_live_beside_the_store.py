"""One extension's settings: its declared keys over what was stored beside the bundle.

:class:`~igni_core.extensions.extension_settings.ExtensionSettings` is
built from a manifest and a :class:`SettingsValues` alone — the values
are kept apart from the signed bundle and the extension store — and
renders the declared keys with their stored value or default.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from igni_core.extensions.events import HostTopic
from igni_core.extensions.extension_settings import ExtensionSettings
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.settings_values import FileSettingsValues
from igni_core.extensions.store_errors import StoreError

SETTINGS = [
    {
        "key": "failOn",
        "type": "enum",
        "label": "Fail on",
        "options": ["low", "high"],
        "default": "high",
    },
    {"key": "note", "type": "string", "label": "Note"},
    {"key": "retries", "type": "number", "label": "Retries", "default": 3},
]


def _manifest(settings: list[dict] | None = None) -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": "acme.scanner",
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "trust": "unsigned",
            "dev": {"command": ["true"]},
            "contributes": {"settings": SETTINGS if settings is None else settings},
        }
    )


@pytest.fixture
def values(tmp_path: Path) -> FileSettingsValues:
    return FileSettingsValues(tmp_path)


def test_the_declared_settings_are_listed_by_key(values):
    assert list(ExtensionSettings(_manifest(), values).declared) == ["failOn", "note", "retries"]


def test_a_read_has_defaults_and_leaves_out_a_key_with_neither_default_nor_value(values):
    assert ExtensionSettings(_manifest(), values).read() == {"failOn": "high", "retries": 3}


def test_a_stored_value_wins_a_stale_key_is_dropped_and_a_stored_none_is_left_out(values):
    values.write("acme.scanner", {"failOn": "low", "gone": "x", "retries": None})
    assert ExtensionSettings(_manifest(), values).read() == {"failOn": "low"}


def test_an_extension_declaring_nothing_reads_empty_whatever_is_stored(values):
    values.write("acme.scanner", {"failOn": "low"})
    assert ExtensionSettings(_manifest([]), values).read() == {}


def test_a_write_keeps_the_keys_it_was_not_given_and_returns_what_was_stored(values, tmp_path):
    settings = ExtensionSettings(_manifest(), values)
    assert settings.write({"note": "keep"}) == {"failOn": "high", "retries": 3, "note": "keep"}
    assert settings.write({"retries": 5}) == {"failOn": "high", "retries": 5, "note": "keep"}
    stored = json.loads((tmp_path / "acme.scanner.settings.json").read_text())
    assert stored == {"failOn": "high", "retries": 5, "note": "keep"}
    assert settings.read() == stored


def test_an_empty_write_stores_the_defaults_and_changes_nothing_else(values):
    settings = ExtensionSettings(_manifest(), values)
    settings.write({"note": "keep"})
    assert settings.write({}) == {"failOn": "high", "retries": 3, "note": "keep"}


def test_a_key_nobody_declared_is_refused_with_the_declared_ones_and_nothing_is_stored(values):
    settings = ExtensionSettings(_manifest(), values)
    with pytest.raises(StoreError) as refused:
        settings.write({"note": "ok", "zeta": 1, "alpha": 2})
    assert str(refused.value) == (
        "acme.scanner does not declare ['alpha', 'zeta']. "
        "Its settings are ['failOn', 'note', 'retries']."
    )
    assert values.read("acme.scanner") == {}


def test_an_extension_with_no_settings_says_none_when_written_to(values):
    with pytest.raises(StoreError, match="Its settings are none"):
        ExtensionSettings(_manifest([]), values).write({"a": 1})


def test_an_enum_value_outside_its_options_is_refused_and_nothing_is_stored(values):
    settings = ExtensionSettings(_manifest(), values)
    with pytest.raises(StoreError, match=r"'failOn' must be one of \['low', 'high'\]"):
        settings.write({"note": "ok", "failOn": "medium"})
    assert values.read("acme.scanner") == {}


def test_the_wire_names_of_the_host_topics_are_what_an_sdk_names():
    assert {topic.name: topic.value for topic in HostTopic} == {
        "SESSION_SWITCHED": "session.switched",
        "WORKSPACE_HEAD_CHANGED": "workspace.head-changed",
        "WORKSPACE_FILES_CHANGED": "workspace.files-changed",
        "EXTENSION_STATE_CHANGED": "extension.state-changed",
    }
    assert HostTopic.SESSION_SWITCHED == "session.switched"
