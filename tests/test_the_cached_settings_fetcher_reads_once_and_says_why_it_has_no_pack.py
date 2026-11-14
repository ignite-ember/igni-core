"""``CachedSettingsFetcher``: the one read of the stored pack a settings load makes.

It verifies the pack on disk, rebuilds what derives from it, and answers
once: a pack, ``None``, or ``GroupPolicyUnavailable`` when the organisation
requires a policy and none can be had. The network is a boundary here and
is replaced by a transport that stores what a live fetch would.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from igni_core.core.auth.schemas import SignedInSession
from igni_core.core.config.group_policy.cache import GroupPolicyCache, RebuildResult
from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.jwks import Jwks
from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.group_policy.settings_fetcher import (
    CachedSettingsFetcher,
    FetcherState,
    GroupPolicyUnavailable,
)
from igni_core.core.config.group_policy.transport import LiveFetchResult
from igni_core.core.config.group_policy.trust import PackTrust
from igni_core.core.config.group_policy.verifier import AcceptedPack
from tests.group_policy_signing import ISSUER, KEY, PERSON, ServerKey, sign_in, store

PLUGIN_URL = "https://git.example.invalid/org/tool.git"


def _agent(name: str = "reviewer") -> GroupPolicyEntry:
    return GroupPolicyEntry(kind="agents", entry_name=name, content="body", content_type="markdown")


def _pack(*entries: GroupPolicyEntry, settings: dict | None = None) -> GroupPolicyPack:
    extra = []
    if settings is not None:
        extra.append(
            GroupPolicyEntry(
                kind="settings",
                entry_name="s",
                content=json.dumps(settings),
                content_type="json",
            )
        )
    return GroupPolicyPack(
        group_id="g-1",
        group_name="Legal",
        fetched_at=datetime.now(timezone.utc),
        entries=[*entries, *extra],
    )


def _trust(*, required: bool = False, api_url: str | None = ISSUER) -> PackTrust:
    return PackTrust(api_url=api_url, session=SignedInSession(subject=PERSON), required=required)


@pytest.fixture
def cache(tmp_path: Path) -> GroupPolicyCache:
    return GroupPolicyCache(GroupPolicyLayout(root=tmp_path / "group-policy"))


class _Transport:
    """A live fetch that stores whatever it is given, and counts how often it ran."""

    def __init__(self, store_into=None, *, stored: bool = True, reason: str = "") -> None:
        self.calls = 0
        self._store_into = store_into
        self._result = LiveFetchResult(stored=stored, reason=reason)

    def fetch_into(self, cache: GroupPolicyCache) -> LiveFetchResult:
        self.calls += 1
        if self._store_into is not None:
            self._store_into(cache)
        return self._result


def _store_pack(pack: GroupPolicyPack, *, key: ServerKey = KEY):
    def put(cache: GroupPolicyCache) -> None:
        store(cache.layout.root, pack, key=key, jwks=KEY.jwks)

    return put


def _store_absence(cache: GroupPolicyCache, version: int = 10) -> None:
    compact = KEY.sign_absence(version=version)
    cache.signed.store(
        AcceptedPack(
            pack=None, compact=compact, jwks=Jwks.model_validate(KEY.jwks), version=version
        )
    )


class TestItReadsOnce:
    def test_the_answer_is_remembered_and_the_disk_is_not_read_again(self, cache):
        store(cache.layout.root, _pack(_agent(), settings={"learning": {}}))
        fetcher = CachedSettingsFetcher(cache, _trust())

        first = fetcher()
        cache.layout.file(GroupPolicyLayout.SIGNED_PACK_FILE).unlink()
        second = fetcher()

        assert first is not None
        assert second is first
        assert CachedSettingsFetcher(cache, _trust())() is None

    def test_a_nothing_answer_is_remembered_too(self, cache):
        fetcher = CachedSettingsFetcher(cache, _trust())
        assert fetcher() is None

        store(cache.layout.root, _pack(_agent()))

        assert fetcher() is None

    def test_the_unavailable_outcome_is_remembered_and_raised_again_without_a_second_fetch(
        self, cache
    ):
        transport = _Transport(stored=False)
        fetcher = CachedSettingsFetcher(cache, _trust(required=True), transport)

        with pytest.raises(GroupPolicyUnavailable) as first:
            fetcher()
        with pytest.raises(GroupPolicyUnavailable) as second:
            fetcher()

        assert str(first.value) == str(second.value)
        assert transport.calls == 1

    def test_the_state_starts_unread(self):
        state = FetcherState()

        assert (state.phase, state.pack, state.error) == ("unread", None, "")


class TestWhenThereIsNoUsablePackAndNothingIsRequired:
    def test_no_pack_on_disk_clears_what_was_derived_and_says_so_at_debug(self, cache, caplog):
        caplog.set_level(logging.DEBUG)
        derived = cache.layout.ensure_dir("agents") / "left-over.md"
        derived.write_text("stale")

        assert CachedSettingsFetcher(cache, _trust())() is None

        assert not derived.exists()
        record = next(r for r in caplog.records if "no cached org group policy" in r.getMessage())
        assert record.levelno == logging.DEBUG

    def test_a_pack_that_does_not_verify_clears_what_was_derived_and_warns(self, cache, caplog):
        caplog.set_level(logging.WARNING)
        store(cache.layout.root, _pack(_agent()), key=ServerKey(), jwks=KEY.jwks)
        assert (cache.layout.dir_for("agents") / "reviewer.md").is_file()

        assert CachedSettingsFetcher(cache, _trust())() is None

        assert not cache.layout.dir_for("agents").exists()
        warning = next(r for r in caplog.records if "ignored" in r.getMessage())
        assert warning.levelno == logging.WARNING
        assert "does not verify" in warning.getMessage()

    def test_without_a_trust_context_nothing_applies(self, cache):
        store(cache.layout.root, _pack(_agent(), settings={"learning": {}}))

        assert CachedSettingsFetcher(cache)() is None
        assert not cache.layout.dir_for("agents").exists()

    def test_a_signed_absence_clears_what_was_derived(self, cache, caplog):
        caplog.set_level(logging.INFO)
        derived = cache.layout.ensure_dir("agents") / "left-over.md"
        derived.write_text("stale")
        _store_absence(cache)

        assert CachedSettingsFetcher(cache, _trust())() is None

        assert not derived.exists()
        assert any("has no group policy" in r.getMessage() for r in caplog.records)

    def test_an_error_while_reading_the_cache_is_a_rejected_read_not_a_crash(
        self, cache, caplog, monkeypatch
    ):
        caplog.set_level(logging.WARNING)

        def explode(trust):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(cache, "verified_pack", explode)

        assert CachedSettingsFetcher(cache, _trust())() is None

        assert any("ignored: disk on fire" in r.getMessage() for r in caplog.records)

    def test_a_cache_that_cannot_be_cleared_is_warned_about_and_the_load_goes_on(
        self, cache, caplog, monkeypatch
    ):
        caplog.set_level(logging.WARNING)

        def refuse():
            raise OSError("read-only volume")

        monkeypatch.setattr(cache, "clear_derived", refuse)

        assert CachedSettingsFetcher(cache, _trust())() is None

        assert any(
            "could not clear" in r.getMessage() and "read-only volume" in r.getMessage()
            for r in caplog.records
        )


class TestAVerifiedPack:
    def test_its_files_are_rebuilt_and_the_pack_is_returned(self, cache):
        store(cache.layout.root, _pack(_agent(), settings={"learning": {"enabled": False}}))
        (cache.layout.dir_for("agents") / "reviewer.md").write_text("edited")
        (cache.layout.dir_for("agents") / "planted.md").write_text("evil")

        pack = CachedSettingsFetcher(cache, _trust())()

        assert pack is not None
        assert pack.to_settings_dict() == {"learning": {"enabled": False}}
        assert sorted(p.name for p in cache.layout.dir_for("agents").iterdir()) == ["reviewer.md"]
        assert (cache.layout.dir_for("agents") / "reviewer.md").read_text() == "body"

    def test_the_log_names_the_group_and_its_settings(self, cache, caplog):
        caplog.set_level(logging.INFO)
        store(cache.layout.root, _pack(settings={"b": 1, "a": 2}))

        CachedSettingsFetcher(cache, _trust())()

        message = next(
            r.getMessage() for r in caplog.records if "applied to settings" in r.getMessage()
        )
        assert "group Legal (g-1)" in message
        assert "settings a, b," in message

    def test_a_pack_with_no_settings_says_none(self, cache, caplog):
        caplog.set_level(logging.INFO)
        store(cache.layout.root, _pack(_agent()))

        CachedSettingsFetcher(cache, _trust())()

        assert any("settings (none)" in r.getMessage() for r in caplog.records)


class TestRequiredWithNoUsablePack:
    def test_the_message_names_the_server_and_what_to_do(self, cache):
        fetcher = CachedSettingsFetcher(cache, _trust(required=True), _Transport(stored=False))

        with pytest.raises(GroupPolicyUnavailable) as raised:
            fetcher()

        message = str(raised.value)
        assert "there is no group policy on this machine yet" in message
        assert f"Connect to {ISSUER}" in message
        assert "restart igni" in message
        assert "No agent session will start until then." in message

    def test_with_no_api_url_the_message_names_igni_server(self, cache):
        trust = _trust(required=True, api_url=None)

        with pytest.raises(GroupPolicyUnavailable, match="Connect to igni server and sign in"):
            CachedSettingsFetcher(cache, trust, _Transport(stored=False))()

    def test_a_rejected_pack_gives_its_reason(self, cache):
        store(cache.layout.root, _pack(), key=ServerKey(), jwks=KEY.jwks)
        fetcher = CachedSettingsFetcher(cache, _trust(required=True), _Transport(stored=False))

        with pytest.raises(GroupPolicyUnavailable, match="does not verify"):
            fetcher()

    def test_a_live_fetch_that_stores_a_pack_is_read_rebuilt_and_returned(self, cache, caplog):
        caplog.set_level(logging.INFO)
        transport = _Transport(_store_pack(_pack(_agent(), settings={"learning": {}})))

        pack = CachedSettingsFetcher(cache, _trust(required=True), transport)()

        assert pack is not None and pack.group_id == "g-1"
        assert transport.calls == 1
        assert (cache.layout.dir_for("agents") / "reviewer.md").is_file()
        assert any("fetched from igni server at start-up" in r.getMessage() for r in caplog.records)

    def test_a_live_signed_absence_means_no_policy(self, cache):
        transport = _Transport(_store_absence)

        assert CachedSettingsFetcher(cache, _trust(required=True), transport)() is None

    def test_a_live_fetch_that_stores_but_cannot_rebuild_refuses_with_the_reason(self, cache):
        plugin = GroupPolicyEntry(
            kind="plugins",
            entry_name="tool",
            content="",
            content_type="yaml",
            source_url=PLUGIN_URL,
        )
        transport = _Transport(_store_pack(_pack(plugin)))

        with pytest.raises(GroupPolicyUnavailable, match=r"plugin\(s\) tool are not installed"):
            CachedSettingsFetcher(cache, _trust(required=True), transport)()

    def test_a_live_fetch_that_says_stored_but_left_nothing_usable_refuses(self, cache):
        transport = _Transport(stored=True)

        with pytest.raises(GroupPolicyUnavailable, match="no signed group pack"):
            CachedSettingsFetcher(cache, _trust(required=True), transport)()

    def test_a_verified_pack_whose_files_cannot_be_rebuilt_refuses(self, cache, monkeypatch):
        store(cache.layout.root, _pack(_agent()))

        def fail(pack):
            return RebuildResult(ok=False, reason="disk full")

        monkeypatch.setattr(cache, "rebuild", fail)

        with pytest.raises(GroupPolicyUnavailable, match="could not be rebuilt: disk full"):
            CachedSettingsFetcher(cache, _trust(required=True))()

    def test_the_same_pack_not_required_still_applies_its_settings(
        self, cache, monkeypatch, caplog
    ):
        caplog.set_level(logging.WARNING)
        store(cache.layout.root, _pack(settings={"x": 1}))

        def fail(pack):
            return RebuildResult(ok=False, reason="disk full")

        monkeypatch.setattr(cache, "rebuild", fail)

        pack = CachedSettingsFetcher(cache, _trust())()

        assert pack is not None and pack.to_settings_dict() == {"x": 1}
        assert any("could not be rebuilt (disk full)" in r.getMessage() for r in caplog.records)


class TestBuiltFromSettings:
    def _settings(self, data_dir: Path, *, required: bool = False, api_url: str = ISSUER):
        return SimpleNamespace(
            api_url=api_url,
            storage=SimpleNamespace(data_dir=str(data_dir)),
            auth=SimpleNamespace(credentials_file=str(data_dir / "credentials.json")),
            group_policy=SimpleNamespace(trusted_keys=[], required=required),
        )

    def test_required_follows_the_trust(self, tmp_path):
        assert CachedSettingsFetcher.for_settings(self._settings(tmp_path)).required is False
        assert (
            CachedSettingsFetcher.for_settings(self._settings(tmp_path, required=True)).required
            is True
        )

    def test_the_cache_is_the_one_under_the_settings_data_dir(self, tmp_path):
        sign_in(tmp_path / "credentials.json")
        store(tmp_path / "group-policy", _pack(settings={"learning": {"enabled": False}}))

        pack = CachedSettingsFetcher.for_settings(self._settings(tmp_path))()

        assert pack is not None
        assert pack.to_settings_dict() == {"learning": {"enabled": False}}
