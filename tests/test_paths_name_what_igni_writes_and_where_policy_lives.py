"""``core/paths``: the names, the rules-file order, the managed policy
directory per platform and the config directories with their legacy fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core import paths


def test_the_names_are_igni_with_the_old_ones_still_read():
    assert paths.CONFIG_DIR == ".igni"
    assert paths.LEGACY_CONFIG_DIR == ".ember"
    assert paths.DEFAULT_DATA_DIR == "~/.igni"
    assert paths.CLAUDE_CONFIG_DIR == ".claude"
    assert paths.PROJECT_CONTEXT_FILES == ("igni.md", "ember.md")
    assert paths.PROJECT_CONTEXT_FILES_CROSS_TOOL == ("igni.md", "ember.md", "CLAUDE.md")


def test_each_rules_file_is_followed_by_its_local_override():
    assert paths.RULES_FILES == ("igni.md", "igni.local.md", "ember.md", "ember.local.md")
    assert paths.RULES_FILES_CROSS_TOOL == (
        "igni.md",
        "igni.local.md",
        "ember.md",
        "ember.local.md",
        "CLAUDE.md",
        "CLAUDE.local.md",
    )


def test_sessions_saved_under_the_old_agent_name_are_still_the_users_own():
    assert paths.MAIN_AGENT_NAME == "igni"
    assert paths.MAIN_AGENT_IDS == ("igni", "ember")


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("darwin", Path("/Library/Application Support/igni")),
        ("linux", Path("/etc/igni")),
        ("linux2", Path("/etc/igni")),
        ("freebsd14", None),
    ],
)
def test_the_managed_policy_dir_per_platform(monkeypatch, platform, expected):
    monkeypatch.setattr(paths.sys, "platform", platform)
    assert paths.managed_policy_dir() == expected


def test_windows_policy_lives_under_programdata(monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMDATA", "D:/Data")
    assert paths.managed_policy_dir() == Path("D:/Data") / "igni"
    monkeypatch.delenv("PROGRAMDATA")
    assert paths.managed_policy_dir() == Path(r"C:\ProgramData") / "igni"


@pytest.mark.parametrize("as_text", [True, False])
def test_a_project_with_neither_dir_gets_the_new_path_and_nothing_is_created(tmp_path, as_text):
    found = paths.project_config_dir(str(tmp_path) if as_text else tmp_path)
    assert found == tmp_path / ".igni"
    assert not found.exists()


def test_a_project_still_on_the_legacy_dir_reads_it(tmp_path):
    (tmp_path / ".ember").mkdir()
    assert paths.project_config_dir(tmp_path) == tmp_path / ".ember"


def test_a_project_with_both_dirs_uses_the_new_one(tmp_path):
    (tmp_path / ".ember").mkdir()
    (tmp_path / ".igni").mkdir()
    assert paths.project_config_dir(tmp_path) == tmp_path / ".igni"


def test_home_config_dir_prefers_new_falls_back_to_legacy_else_new(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert paths.home_config_dir() == tmp_path / ".igni"
    assert not (tmp_path / ".igni").exists()
    (tmp_path / ".ember").mkdir()
    assert paths.home_config_dir() == tmp_path / ".ember"
    (tmp_path / ".igni").mkdir()
    assert paths.home_config_dir() == tmp_path / ".igni"
