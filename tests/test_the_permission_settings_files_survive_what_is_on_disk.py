"""The permission settings files are read and written by people as well as by igni.

A file that is missing, empty, not JSON, not UTF-8 or the wrong shape must
cost only that file, never the session: the loader reports it and moves on,
and a save starts again from what it can read. The rules a save writes keep
every other key in the file.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from igni_core.core.config.tool_permissions import SettingsFileLoader, SettingsFileWriter
from igni_core.core.config.tool_permissions.store import ToolPermissions
from igni_core.core.paths import project_config_dir


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    return path


def _settings(project: Path, name: str = "settings.json") -> Path:
    return project_config_dir(project) / name


def _put(path: Path, text: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text)
    return path


# ── the loader ────────────────────────────────────────────────────


def test_the_loader_consults_home_then_project_and_only_the_files_that_exist(project):
    home = Path.home() / ".igni"
    paths = SettingsFileLoader(project).paths()

    assert paths == [
        home / "settings.json",
        home / "settings.local.json",
        project_config_dir(project) / "settings.json",
        project_config_dir(project) / "settings.local.json",
    ]
    assert SettingsFileLoader(project).load_all() == []

    _put(_settings(project, "settings.local.json"), "{}")
    _put(home / "settings.json", "{}")

    results = SettingsFileLoader(project).load_all()

    assert [r.path for r in results] == [
        str(home / "settings.json"),
        str(_settings(project, "settings.local.json")),
    ]


def test_a_file_that_is_not_json_is_reported_and_logged_not_raised(project, caplog):
    path = _put(_settings(project), "{not json")

    with caplog.at_level(logging.WARNING):
        (result,) = SettingsFileLoader(project).load_all()

    assert not result.ok
    assert result.file is None
    assert result.reason is not None and result.reason.startswith("json decode failed")
    assert str(path) in caplog.text


def test_an_empty_file_is_not_json_either(project):
    _put(_settings(project), "")

    (result,) = SettingsFileLoader(project).load_all()

    assert not result.ok
    assert result.reason is not None and result.reason.startswith("json decode failed")


def test_a_file_that_is_not_utf8_is_reported_not_raised(project, caplog):
    _put(_settings(project), b'{"permissions": {"deny": ["\xff\xfe"]}}')

    with caplog.at_level(logging.WARNING):
        (result,) = SettingsFileLoader(project).load_all()

    assert not result.ok
    assert result.reason is not None and result.reason.startswith("read failed")
    assert "Failed to read" in caplog.text


def test_a_path_that_cannot_be_read_as_a_file_is_reported(project):
    _settings(project).mkdir(parents=True)

    (result,) = SettingsFileLoader(project).load_all()

    assert not result.ok
    assert result.reason is not None and result.reason.startswith("read failed")


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        "3",
        '"permissions"',
        "{}",
        '{"mode": "plan"}',
        '{"permissions": null}',
        '{"permissions": {}}',
        '{"permissions": []}',
    ],
)
def test_a_file_without_a_permissions_block_is_fine_and_holds_no_rules(project, text):
    _put(_settings(project), text)

    (result,) = SettingsFileLoader(project).load_all()

    assert result.ok
    assert result.reason is None
    assert result.file is not None
    assert result.file.rules_by_level() == []


@pytest.mark.parametrize(
    "block",
    [{"allow": "Bash"}, {"deny": ["Bash", 1]}, "Bash", ["Bash"], {"ask": {"a": "b"}}],
)
def test_a_permissions_block_of_the_wrong_shape_is_reported_and_logged(project, block, caplog):
    _put(_settings(project), json.dumps({"permissions": block}))

    with caplog.at_level(logging.WARNING):
        (result,) = SettingsFileLoader(project).load_all()

    assert not result.ok
    assert result.reason is not None and result.reason.startswith("schema validation failed")
    assert "Failed to validate" in caplog.text


def test_a_valid_block_is_parsed_with_its_lists(project):
    _put(
        _settings(project),
        json.dumps({"permissions": {"allow": ["Bash(git status)"], "deny": ["WebFetch"]}}),
    )

    (result,) = SettingsFileLoader(project).load_all()

    assert result.ok and result.file is not None
    assert result.file.allow == ["Bash(git status)"]
    assert result.file.ask == []
    assert result.file.deny == ["WebFetch"]


def test_one_bad_file_does_not_stop_the_store_reading_the_others(project):
    _put(_settings(project), b"\xff\xff")
    _put(_settings(project, "settings.local.json"), json.dumps({"permissions": {"deny": ["Bash"]}}))

    perms = ToolPermissions(project_dir=project)

    assert perms.get_level("Bash") == "deny"


# ── the writer ────────────────────────────────────────────────────


def test_the_writer_targets_the_project_local_file_or_the_home_local_one(project):
    assert SettingsFileWriter(project).target_path() == (
        project_config_dir(project) / "settings.local.json"
    )
    assert SettingsFileWriter().target_path() == Path.home() / ".igni" / "settings.local.json"


def test_a_saved_rule_creates_the_directory_and_returns_the_path_it_wrote(project):
    writer = SettingsFileWriter(project)

    path = writer.save_rule("Bash(git push)", "allow")

    assert path == writer.target_path()
    text = path.read_text()
    assert json.loads(text) == {"permissions": {"allow": ["Bash(git push)"]}}
    # Two-space indent and a closing newline: the file is meant to be read.
    assert text == json.dumps({"permissions": {"allow": ["Bash(git push)"]}}, indent=2) + "\n"


def test_a_saved_rule_without_a_project_goes_to_home(tmp_path):
    path = SettingsFileWriter().save_rule("WebFetch", "deny")

    assert path == Path.home() / ".igni" / "settings.local.json"
    assert json.loads(path.read_text())["permissions"]["deny"] == ["WebFetch"]


def test_a_saved_rule_keeps_every_other_key_and_rule_in_the_file(project):
    path = _put(
        _settings(project, "settings.local.json"),
        json.dumps({"mode": "plan", "permissions": {"allow": ["Edit"], "extra": 1}, "x": [1]}),
    )

    SettingsFileWriter(project).save_rule("Bash(ls)", "ask")

    assert json.loads(path.read_text()) == {
        "mode": "plan",
        "permissions": {"allow": ["Edit"], "extra": 1, "ask": ["Bash(ls)"]},
        "x": [1],
    }


def test_saving_a_rule_at_another_level_moves_it_and_saving_it_again_does_not_duplicate_it(project):
    writer = SettingsFileWriter(project)

    writer.save_rule("Bash(ls)", "allow")
    writer.save_rule("Bash(pwd)", "allow")
    path = writer.save_rule("Bash(ls)", "deny")
    writer.save_rule("Bash(ls)", "deny")

    assert json.loads(path.read_text())["permissions"] == {
        "allow": ["Bash(pwd)"],
        "deny": ["Bash(ls)"],
    }


def test_a_corrupt_or_non_object_file_is_started_over_with_a_warning(project, caplog):
    path = _put(_settings(project, "settings.local.json"), "{broken")

    with caplog.at_level(logging.WARNING):
        SettingsFileWriter(project).save_rule("Bash", "allow")

    assert json.loads(path.read_text()) == {"permissions": {"allow": ["Bash"]}}
    assert "starting from empty" in caplog.text

    _put(path, "[1, 2]")
    SettingsFileWriter(project).save_rule("Edit", "ask")
    assert json.loads(path.read_text()) == {"permissions": {"ask": ["Edit"]}}


def test_a_file_that_is_not_utf8_is_started_over_not_raised(project):
    path = _put(_settings(project, "settings.local.json"), b"\xff\xfe\x00")

    SettingsFileWriter(project).save_rule("Bash", "deny")

    assert json.loads(path.read_text()) == {"permissions": {"deny": ["Bash"]}}


@pytest.mark.parametrize(
    "existing",
    [
        {"permissions": ["Bash"]},
        {"permissions": None},
        {"permissions": "allow"},
        {"permissions": {"allow": "Bash"}},
        {"permissions": {"deny": None, "allow": 3}},
    ],
)
def test_a_save_over_a_permissions_block_of_the_wrong_shape_replaces_what_cannot_be_read(
    project, existing
):
    path = _put(_settings(project, "settings.local.json"), json.dumps(existing))

    SettingsFileWriter(project).save_rule("Bash(ls)", "allow")

    saved = json.loads(path.read_text())["permissions"]
    assert saved["allow"] == ["Bash(ls)"]


def test_a_rule_with_unicode_survives_a_round_trip_through_the_file(project):
    SettingsFileWriter(project).save_rule("Bash(echo é)", "allow")

    (result,) = SettingsFileLoader(project).load_all()

    assert result.file is not None
    assert result.file.allow == ["Bash(echo é)"]
