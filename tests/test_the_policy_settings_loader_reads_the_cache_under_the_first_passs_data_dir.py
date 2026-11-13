"""``PolicySettingsLoader``: the two-pass load every igni process makes.

The policy cache lives under ``storage.data_dir``, which is itself a
setting. The first pass (local configuration only) answers "which
directory"; the second, with the verified cached pack, is the one that
counts. Neither touches the network.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.environment import GroupPolicyEnvironment
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.group_policy.policy_settings import PolicySettingsLoader
from igni_core.core.config.group_policy.settings_fetcher import GroupPolicyUnavailable
from igni_core.core.config.group_policy.transport import LiveFetchResult, SignedPackTransport
from igni_core.core.config.managed_policy import ManagedPolicySource
from igni_core.core.config.settings_loader import SettingsLoader
from tests.group_policy_signing import ISSUER, sign_in, store


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(GroupPolicyEnvironment.TRUSTED_KEYS_VAR, raising=False)
    monkeypatch.delenv(GroupPolicyEnvironment.REQUIRED_VAR, raising=False)
    monkeypatch.setattr(ManagedPolicySource, "platform_path", staticmethod(lambda: None))


def _pack(payload: dict) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g1",
        group_name="Regulated",
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


def _configure(tmp_path: Path, *, data_dir: Path | None = None, extra: str = "") -> Path:
    """The user's config: this server, and optionally a data directory of their choosing."""
    home = Path.home() / ".igni"
    home.mkdir(parents=True, exist_ok=True)
    text = f"api_url: {ISSUER}\n{extra}"
    if data_dir is not None:
        text += f"storage:\n  data_dir: {data_dir}\n"
    (home / "config.yaml").write_text(text)
    local = SettingsLoader.load(project_dir=tmp_path)
    sign_in(Path(local.auth.credentials_file).expanduser())
    return Path(local.storage.data_dir).expanduser()


class TestTheSecondPassAppliesTheCachedPack:
    def test_the_pack_cached_under_the_default_data_dir_applies(self, tmp_path):
        data_dir = _configure(tmp_path)
        store(data_dir / "group-policy", _pack({"learning": {"enabled": False}}))

        loader = PolicySettingsLoader(tmp_path)

        assert loader.local().learning.enabled is True
        assert loader.load().learning.enabled is False

    def test_the_cache_is_looked_up_under_the_data_dir_the_first_pass_found(self, tmp_path):
        chosen = tmp_path / "elsewhere"
        _configure(tmp_path, data_dir=chosen)
        store(chosen / "group-policy", _pack({"learning": {"enabled": False}}))

        assert PolicySettingsLoader(tmp_path).load().learning.enabled is False

    def test_a_pack_under_the_default_directory_is_not_read_when_the_data_dir_moved(self, tmp_path):
        chosen = tmp_path / "elsewhere"
        _configure(tmp_path, data_dir=chosen)
        default = Path.home() / ".igni"
        store(default / "group-policy", _pack({"learning": {"enabled": False}}))

        assert PolicySettingsLoader(tmp_path).load().learning.enabled is True

    def test_local_alone_never_applies_the_pack(self, tmp_path):
        data_dir = _configure(tmp_path)
        store(data_dir / "group-policy", _pack({"learning": {"enabled": False}}))

        assert PolicySettingsLoader(tmp_path).local().learning.enabled is True

    def test_with_no_cache_the_load_is_the_local_one(self, tmp_path):
        _configure(tmp_path, extra="air_gapped: true\n")

        settings = PolicySettingsLoader(tmp_path).load()

        assert settings.air_gapped is True
        assert settings.learning.enabled is True

    def test_the_project_directory_is_the_one_both_passes_read(self, tmp_path):
        data_dir = _configure(tmp_path)
        store(data_dir / "group-policy", _pack({"air_gapped": True}))
        project = tmp_path / "repo"
        (project / ".igni").mkdir(parents=True)
        (project / ".igni" / "config.yaml").write_text("learning:\n  enabled: false\n")

        loader = PolicySettingsLoader(project)

        assert loader.local().learning.enabled is False
        loaded = loader.load()
        assert loaded.learning.enabled is False
        assert loaded.air_gapped is True


class TestALocalSettingsIsNotReread:
    def test_a_given_local_is_what_decides_the_data_dir(self, tmp_path):
        chosen = tmp_path / "elsewhere"
        data_dir = _configure(tmp_path, data_dir=chosen)
        store(chosen / "group-policy", _pack({"learning": {"enabled": False}}))
        default_local = SettingsLoader.load(project_dir=tmp_path)
        assert Path(default_local.storage.data_dir).expanduser() == chosen
        assert data_dir == chosen

        # A local pass that says the default directory: the pack under
        # ``chosen`` is then not the one looked for.
        other = default_local.model_copy(deep=True)
        other.storage.data_dir = str(Path.home() / ".igni")

        assert PolicySettingsLoader(tmp_path).load(other).learning.enabled is True

    def test_load_without_one_reads_it_exactly_once(self, tmp_path, monkeypatch):
        _configure(tmp_path)
        loader = PolicySettingsLoader(tmp_path)
        calls: list[int] = []
        real = PolicySettingsLoader.local

        def counted(self):
            calls.append(1)
            return real(self)

        monkeypatch.setattr(PolicySettingsLoader, "local", counted)

        loader.load()

        assert calls == [1]

    def test_load_with_one_does_not_read_local_at_all(self, tmp_path, monkeypatch):
        _configure(tmp_path)
        local = PolicySettingsLoader(tmp_path).local()

        def refuse(self):
            raise AssertionError("local was re-read")

        monkeypatch.setattr(PolicySettingsLoader, "local", refuse)

        assert PolicySettingsLoader(tmp_path).load(local).learning.enabled is True


class TestARequiredPolicyThatCannotBeHad:
    def test_load_raises_and_local_still_answers(self, tmp_path, monkeypatch):
        _configure(tmp_path)
        monkeypatch.setenv(GroupPolicyEnvironment.REQUIRED_VAR, "1")
        monkeypatch.setattr(
            SignedPackTransport,
            "fetch_into",
            lambda self, cache: LiveFetchResult(stored=False, reason="unreachable"),
        )
        loader = PolicySettingsLoader(tmp_path)

        with pytest.raises(GroupPolicyUnavailable, match="requires its group policy"):
            loader.load()

        assert loader.local().learning.enabled is True
