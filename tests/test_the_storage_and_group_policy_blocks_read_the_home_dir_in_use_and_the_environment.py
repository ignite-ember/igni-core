"""``storage``, ``auth`` and ``group_policy``: paths under the config dir in use, env overrides."""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.config.schemas import AuthConfig, GroupPolicyConfig, StorageConfig


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "home"
    directory.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: directory))
    for name in (
        "IGNI_DATABASE_URL",
        "IGNI_GROUP_POLICY_TRUSTED_KEYS",
        "IGNI_GROUP_POLICY_REQUIRED",
    ):
        monkeypatch.delenv(name, raising=False)
    return directory


def test_paths_default_under_igni_when_neither_directory_exists(home: Path):
    assert StorageConfig().data_dir == str(home / ".igni")
    assert StorageConfig().audit_log == str(home / ".igni" / "audit.log")
    assert AuthConfig().credentials_file == str(home / ".igni" / "credentials.json")


def test_an_unmigrated_install_keeps_its_credentials_and_history_in_the_legacy_directory(
    home: Path,
):
    (home / ".ember").mkdir()
    assert StorageConfig().data_dir == str(home / ".ember")
    assert StorageConfig().audit_log == str(home / ".ember" / "audit.log")
    assert AuthConfig().credentials_file == str(home / ".ember" / "credentials.json")


def test_the_new_directory_wins_when_both_exist(home: Path):
    (home / ".ember").mkdir()
    (home / ".igni").mkdir()
    assert StorageConfig().data_dir == str(home / ".igni")
    assert AuthConfig().credentials_file == str(home / ".igni" / "credentials.json")


def test_an_explicit_path_is_kept(home: Path):
    assert StorageConfig(data_dir="/srv/igni").data_dir == "/srv/igni"
    assert AuthConfig(credentials_file="/x/c.json").credentials_file == "/x/c.json"


def test_storage_defaults_keep_ten_thousand_runs_and_no_database_url(home: Path):
    storage = StorageConfig()
    assert storage.max_history_runs == 10000
    assert storage.database_url is None


def test_the_database_url_environment_is_trimmed_and_beats_the_configured_one(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("IGNI_DATABASE_URL", "  postgresql://env/db \n")
    assert StorageConfig().database_url == "postgresql://env/db"
    assert StorageConfig(database_url="sqlite:///file.db").database_url == "postgresql://env/db"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_a_blank_database_url_environment_leaves_the_configured_one(
    home: Path, monkeypatch: pytest.MonkeyPatch, blank: str
):
    monkeypatch.setenv("IGNI_DATABASE_URL", blank)
    assert StorageConfig().database_url is None
    assert StorageConfig(database_url="sqlite:///file.db").database_url == "sqlite:///file.db"


def test_group_policy_polls_every_five_minutes_trusts_published_keys_and_is_not_required(
    home: Path,
):
    group = GroupPolicyConfig()
    assert group.poll_seconds == 300
    assert group.trusted_keys == []
    assert group.required is False


def test_zero_poll_seconds_is_kept_so_polling_can_be_turned_off(home: Path):
    assert GroupPolicyConfig(poll_seconds=0).poll_seconds == 0


def test_pinned_keys_in_the_environment_replace_the_configured_ones(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("IGNI_GROUP_POLICY_TRUSTED_KEYS", "kid-a, kid-b kid-c")
    assert GroupPolicyConfig().trusted_keys == ["kid-a", "kid-b", "kid-c"]
    assert GroupPolicyConfig(trusted_keys=["mine"]).trusted_keys == ["kid-a", "kid-b", "kid-c"]


def test_an_empty_pin_variable_leaves_the_configured_keys(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("IGNI_GROUP_POLICY_TRUSTED_KEYS", "  ")
    assert GroupPolicyConfig(trusted_keys=["mine"]).trusted_keys == ["mine"]


def test_the_required_environment_turns_it_on_and_never_off(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("IGNI_GROUP_POLICY_REQUIRED", "yes")
    assert GroupPolicyConfig().required is True
    monkeypatch.setenv("IGNI_GROUP_POLICY_REQUIRED", "0")
    assert GroupPolicyConfig().required is False
    assert GroupPolicyConfig(required=True).required is True
