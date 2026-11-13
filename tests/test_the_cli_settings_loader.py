"""``CliSettingsLoader``: the two passes, the organisation's refusal, the required pack.

The first pass reads local configuration alone to learn ``storage.data_dir``;
the second reads the group policy cached under that directory. The
``--auto-approve`` refusal is pinned in ``test_an_organisation_sets_the_loosest_mode.py``
for a managed file; here it is pinned for each source that can name it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from igni_core.cli.options import CliOptions
from igni_core.cli.run_settings import CliSettingsLoader
from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.group_policy.settings_fetcher import GroupPolicyUnavailable
from igni_core.core.config.managed_policy import ManagedPolicySource
from tests.group_policy_signing import ISSUER, sign_in, store


@pytest.fixture(autouse=True)
def _no_managed_file(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ManagedPolicySource, "platform_path", staticmethod(lambda: None))


def _managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    path = tmp_path / "managed-settings.yaml"
    path.write_text(text)
    monkeypatch.setattr(ManagedPolicySource, "platform_path", staticmethod(lambda: path))


def _publish(data_dir: Path, payload: dict) -> None:
    """A verified pack in ``data_dir``'s cache, for the person signed in at home."""
    data_dir.mkdir(parents=True, exist_ok=True)
    sign_in(Path.home() / ".igni" / "credentials.json")
    pack = GroupPolicyPack(
        group_id="g1",
        group_name="Platform",
        fetched_at=datetime.now(timezone.utc),
        entries=[
            GroupPolicyEntry(
                kind="settings",
                entry_name="policy",
                content=json.dumps(payload),
                content_type="json",
            )
        ],
    )
    store(data_dir / "group-policy", pack)


def _configure(home_igni: Path, *, data_dir: Path | None) -> None:
    home_igni.mkdir(parents=True, exist_ok=True)
    lines = [f"api_url: {ISSUER}"]
    if data_dir is not None:
        lines += ["storage:", f"  data_dir: {data_dir}"]
    (home_igni / "config.yaml").write_text("\n".join(lines) + "\n")


def test_the_policy_is_read_from_the_data_dir_the_local_config_names(tmp_path: Path):
    """The cache lives under ``storage.data_dir``, itself a setting: the first
    pass answers which directory, so a policy cached there applies."""
    elsewhere = tmp_path / "elsewhere"
    _configure(Path.home() / ".igni", data_dir=elsewhere)
    _publish(elsewhere, {"learning": {"enabled": False}})

    assert CliSettingsLoader(CliOptions()).load().learning.enabled is False


def test_a_policy_in_the_default_directory_is_not_read_when_the_config_moves_it(tmp_path: Path):
    _configure(Path.home() / ".igni", data_dir=tmp_path / "elsewhere")
    _publish(Path.home() / ".igni", {"learning": {"enabled": False}})

    assert CliSettingsLoader(CliOptions()).load().learning.enabled is True


_NO_BYPASS = {"permissions": {"allow_bypass": False}}


def _refusal(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().err


def test_a_group_that_forbids_bypass_is_named_in_the_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    _configure(Path.home() / ".igni", data_dir=None)
    _publish(Path.home() / ".igni", _NO_BYPASS)

    settings = CliSettingsLoader(CliOptions(auto_approve=True)).load()

    assert settings.permissions.mode != "bypassPermissions"
    assert settings.permissions.shell_execute != "allow"
    assert _refusal(capsys) == (
        "Warning: --auto-approve is turned off by your organisation's group policy "
        "(permissions.allow_bypass: false); running without it.\n"
    )


def test_a_managed_file_that_forbids_bypass_is_named_in_the_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _managed(tmp_path, monkeypatch, "permissions:\n  allow_bypass: false\n")

    CliSettingsLoader(CliOptions(auto_approve=True)).load()

    assert "organisation's managed settings (permissions.allow_bypass: false)" in _refusal(capsys)


def test_both_sources_are_named_when_both_forbid_bypass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _managed(tmp_path, monkeypatch, "permissions:\n  allow_bypass: false\n")
    _configure(Path.home() / ".igni", data_dir=None)
    _publish(Path.home() / ".igni", _NO_BYPASS)

    CliSettingsLoader(CliOptions(auto_approve=True)).load()

    assert "managed settings and group policy" in _refusal(capsys)


def test_nothing_is_said_when_bypass_is_forbidden_and_was_not_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _managed(tmp_path, monkeypatch, "permissions:\n  allow_bypass: false\n")

    settings = CliSettingsLoader(CliOptions(no_web=True)).load()

    assert _refusal(capsys) == ""
    assert settings.permissions.web_fetch == "deny"


def test_a_required_policy_with_no_pack_starts_no_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _managed(tmp_path, monkeypatch, "group_policy:\n  required: true\n")

    with pytest.raises(GroupPolicyUnavailable):
        CliSettingsLoader(CliOptions()).load()


def test_the_group_policy_still_applies_to_the_run_that_dropped_auto_approve(tmp_path: Path):
    """Dropping the refused flag reloads the settings; the reload must read the
    cached pack too, or the rest of the organisation's policy would fall away."""
    _configure(Path.home() / ".igni", data_dir=None)
    _publish(
        Path.home() / ".igni",
        {"permissions": {"allow_bypass": False}, "learning": {"enabled": False}},
    )

    settings = CliSettingsLoader(CliOptions(auto_approve=True)).load()

    assert settings.permissions.mode != "bypassPermissions"
    assert settings.learning.enabled is False
