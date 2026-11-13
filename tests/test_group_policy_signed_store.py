"""The signed pack on disk: what is written, what a read says, and a damaged file reading as none."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from igni_core.core.auth.schemas import SignedInSession
from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.jwks import Jwks
from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.group_policy.signed_store import (
    PackState,
    PluginSourceClaim,
    SignedPackStore,
    StoredPackRead,
)
from igni_core.core.config.group_policy.trust import PackTrust
from igni_core.core.config.group_policy.verifier import AcceptedPack
from tests.group_policy_signing import ISSUER, KEY, PERSON, ServerKey

STRANGER = ServerKey()


def _layout(tmp_path: Path) -> GroupPolicyLayout:
    return GroupPolicyLayout(root=tmp_path / "group-policy")


def _pack(*entries: GroupPolicyEntry) -> GroupPolicyPack:
    return GroupPolicyPack(
        group_id="g1",
        group_name="Regulated",
        fetched_at=datetime.now(timezone.utc),
        entries=list(entries),
    )


def _plugin(name: str = "acme", source: str = "https://plugins.invalid/acme.zip") -> dict:
    return {"kind": "plugins", "entry_name": name, "source_url": source}


def _accepted(compact: str, version: int, *, jwks: Jwks | None = None) -> AcceptedPack:
    return AcceptedPack(pack=None, compact=compact, jwks=jwks or KEY.key_set, version=version)


def _trust(**kwargs) -> PackTrust:
    return PackTrust(api_url=ISSUER, session=SignedInSession(subject=PERSON), **kwargs)


def _write(layout: GroupPolicyLayout, name: str, data: str | bytes) -> None:
    layout.root.mkdir(parents=True, exist_ok=True)
    path = layout.file(name)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")


def _signed_with_entries(entries: object) -> str:
    return KEY.sign_claims(
        {
            "iss": ISSUER,
            "sub": PERSON,
            "token_use": "group_policy",
            "version": 1,
            "policy": {"entries": entries},
        }
    )


class TestStore:
    def test_it_writes_the_pack_as_received_the_keys_and_the_version(self, tmp_path):
        layout = _layout(tmp_path)
        compact = KEY.sign(_pack(), version=7)

        SignedPackStore(layout).store(_accepted(compact, 7))

        assert layout.file(GroupPolicyLayout.SIGNED_PACK_FILE).read_text() == compact
        assert json.loads(layout.file(GroupPolicyLayout.JWKS_FILE).read_text()) == KEY.jwks
        assert json.loads(layout.file(GroupPolicyLayout.STATE_FILE).read_text()) == {"version": 7}

    def test_it_creates_the_directory_it_writes_into(self, tmp_path):
        layout = _layout(tmp_path)
        assert not layout.root.exists()

        SignedPackStore(layout).store(_accepted(KEY.sign(_pack()), 1))

        assert layout.root.is_dir()

    def test_the_version_follows_the_live_pack_down_as_well_as_up(self, tmp_path):
        store = SignedPackStore(_layout(tmp_path))
        store.store(_accepted(KEY.sign(_pack(), version=9), 9))

        store.store(_accepted(KEY.sign(_pack(), version=3), 3))

        assert store.applied_version() == 3

    def test_the_legacy_unsigned_settings_file_is_deleted(self, tmp_path):
        layout = _layout(tmp_path)
        _write(layout, GroupPolicyLayout.LEGACY_SETTINGS_FILE, "{}")

        SignedPackStore(layout).store(_accepted(KEY.sign(_pack()), 1))

        assert not layout.file(GroupPolicyLayout.LEGACY_SETTINGS_FILE).exists()

    def test_dropping_a_legacy_file_that_is_not_there_is_silent(self, tmp_path):
        SignedPackStore(_layout(tmp_path)).drop_legacy_settings()

    def test_an_unwritable_root_raises_rather_than_pretending_to_store(self, tmp_path):
        blocker = tmp_path / "group-policy"
        blocker.write_text("a file where the directory should be")

        with pytest.raises(OSError):
            SignedPackStore(_layout(tmp_path)).store(_accepted(KEY.sign(_pack()), 1))


class TestPackState:
    def test_a_version_is_a_strict_integer(self):
        assert PackState(version=4).version == 4
        for bad in ("4", 4.0, 4.5, True, None):
            with pytest.raises(ValueError):
                PackState(version=bad)  # type: ignore[arg-type]

    def test_it_is_frozen(self):
        with pytest.raises(ValueError):
            PackState(version=1).version = 2  # type: ignore[misc]


class TestSignedText:
    def test_it_is_the_stored_text_stripped(self, tmp_path):
        layout = _layout(tmp_path)
        _write(layout, GroupPolicyLayout.SIGNED_PACK_FILE, "  a.b.c \n")
        assert SignedPackStore(layout).signed_text() == "a.b.c"

    def test_nothing_stored_is_none(self, tmp_path):
        assert SignedPackStore(_layout(tmp_path)).signed_text() is None

    def test_a_file_that_is_not_utf8_reads_as_none_not_a_crash(self, tmp_path):
        layout = _layout(tmp_path)
        _write(layout, GroupPolicyLayout.SIGNED_PACK_FILE, b"\xff\xfe\x00bad")
        assert SignedPackStore(layout).signed_text() is None

    def test_a_directory_where_the_file_belongs_reads_as_none(self, tmp_path):
        layout = _layout(tmp_path)
        layout.file(GroupPolicyLayout.SIGNED_PACK_FILE).mkdir(parents=True)
        assert SignedPackStore(layout).signed_text() is None


class TestJwks:
    def test_the_stored_keys_are_returned_parsed(self, tmp_path):
        store = SignedPackStore(_layout(tmp_path))
        store.store(_accepted(KEY.sign(_pack()), 1))
        assert store.jwks().as_mapping() == KEY.jwks

    @pytest.mark.parametrize(
        "content", [None, "not json", "[1, 2]", "", b"\xff\xfe", '"keys"'], ids=repr
    )
    def test_a_missing_or_unusable_file_is_none(self, tmp_path, content):
        layout = _layout(tmp_path)
        if content is not None:
            _write(layout, GroupPolicyLayout.JWKS_FILE, content)
        assert SignedPackStore(layout).jwks() is None


class TestAppliedVersion:
    def test_the_stored_integer_is_returned(self, tmp_path):
        layout = _layout(tmp_path)
        _write(layout, GroupPolicyLayout.STATE_FILE, '{"version": 12}')
        assert SignedPackStore(layout).applied_version() == 12

    @pytest.mark.parametrize(
        "content",
        [
            None,
            '{"version": "12"}',
            '{"version": 12.5}',
            '{"version": true}',
            '{"version": null}',
            "{}",
            "[12]",
            "12",
            '{"version": 1',
            "",
            b"\xff\xfe",
        ],
        ids=repr,
    )
    def test_anything_else_is_none(self, tmp_path, content):
        layout = _layout(tmp_path)
        if content is not None:
            _write(layout, GroupPolicyLayout.STATE_FILE, content)
        assert SignedPackStore(layout).applied_version() is None


class TestRead:
    def test_nothing_stored_is_missing_and_names_the_directory(self, tmp_path):
        layout = _layout(tmp_path)
        read = SignedPackStore(layout).read(_trust())
        assert read.status == "missing"
        assert str(layout.root) in read.reason

    def test_a_whitespace_only_file_is_missing(self, tmp_path):
        layout = _layout(tmp_path)
        _write(layout, GroupPolicyLayout.SIGNED_PACK_FILE, " \n\t ")
        assert SignedPackStore(layout).read(_trust()).status == "missing"

    def test_a_file_that_is_not_utf8_is_missing_not_a_crash(self, tmp_path):
        layout = _layout(tmp_path)
        _write(layout, GroupPolicyLayout.SIGNED_PACK_FILE, b"\xff\xfe")
        assert SignedPackStore(layout).read(_trust()).status == "missing"

    def test_a_genuine_pack_is_applied_with_the_etag_and_the_stored_text(self, tmp_path):
        store = SignedPackStore(_layout(tmp_path))
        pack = _pack(
            GroupPolicyEntry(kind="settings", entry_name="p", content="{}", content_type="json")
        )
        compact = KEY.sign(pack, version=5)
        store.store(_accepted(compact, 5))

        read = store.read(_trust(), etag='"tag-1"')

        assert read.status == "applied"
        assert read.pack.group_id == "g1"
        assert read.pack.etag == '"tag-1"'
        assert read.pack.signed_pack == compact
        assert [e.entry_name for e in read.pack.entries] == ["p"]

    def test_the_pack_is_built_from_the_signed_policy(self, tmp_path):
        layout = _layout(tmp_path)
        store = SignedPackStore(layout)
        store.store(_accepted(KEY.sign(_pack(), version=5), 5))
        assert store.read(_trust()).pack.etag is None

    def test_a_signed_no_pack_answer_is_absent(self, tmp_path):
        store = SignedPackStore(_layout(tmp_path))
        store.store(_accepted(KEY.sign_absence(version=2), 2))

        read = store.read(_trust())

        assert read == StoredPackRead(status="absent")
        assert read.pack is None

    def test_a_pack_signed_by_a_stranger_is_rejected_with_the_reason(self, tmp_path):
        store = SignedPackStore(_layout(tmp_path))
        store.store(_accepted(STRANGER.sign(_pack()), 1, jwks=KEY.key_set))

        read = store.read(_trust())

        assert read.status == "rejected"
        assert "does not verify" in read.reason

    def test_keys_stored_beside_it_are_the_only_keys_it_is_checked_against(self, tmp_path):
        layout = _layout(tmp_path)
        store = SignedPackStore(layout)
        store.store(_accepted(KEY.sign(_pack(), version=1), 1))
        layout.file(GroupPolicyLayout.JWKS_FILE).unlink()

        read = store.read(_trust())

        assert read.status == "rejected"
        assert "no igni server key" in read.reason

    def test_another_persons_pack_is_rejected(self, tmp_path):
        store = SignedPackStore(_layout(tmp_path))
        store.store(_accepted(KEY.sign(_pack(), sub="someone-else"), 1))
        read = store.read(_trust())
        assert read.status == "rejected"
        assert "somebody other" in read.reason

    def test_a_pack_older_than_the_stored_version_is_a_rollback(self, tmp_path):
        layout = _layout(tmp_path)
        store = SignedPackStore(layout)
        store.store(_accepted(KEY.sign(_pack(), version=3), 3))
        _write(layout, GroupPolicyLayout.STATE_FILE, '{"version": 4}')

        read = store.read(_trust())

        assert read.status == "rejected"
        assert "rollback" in read.reason

    def test_a_corrupt_state_file_is_no_floor(self, tmp_path):
        layout = _layout(tmp_path)
        store = SignedPackStore(layout)
        store.store(_accepted(KEY.sign(_pack(), version=3), 3))
        _write(layout, GroupPolicyLayout.STATE_FILE, "garbage")
        assert store.read(_trust()).status == "applied"

    def test_the_status_is_one_of_four(self):
        with pytest.raises(ValueError):
            StoredPackRead(status="other")  # type: ignore[arg-type]


class TestUnverifiedPluginSources:
    def _store(self, tmp_path, compact: str | bytes | None) -> SignedPackStore:
        layout = _layout(tmp_path)
        if compact is not None:
            _write(layout, GroupPolicyLayout.SIGNED_PACK_FILE, compact)
        return SignedPackStore(layout)

    def test_it_lists_the_entries_the_stored_pack_names(self, tmp_path):
        store = self._store(
            tmp_path, _signed_with_entries([_plugin("a", "https://x.invalid/a"), _plugin("b")])
        )
        claims = store.unverified_plugin_sources()
        assert [(c.kind, c.entry_name, c.source_url) for c in claims] == [
            ("plugins", "a", "https://x.invalid/a"),
            ("plugins", "b", "https://plugins.invalid/acme.zip"),
        ]

    def test_it_reads_a_pack_nothing_has_verified(self, tmp_path):
        forged = STRANGER.sign_claims({"policy": {"entries": [_plugin("planted")]}, "version": 1})
        claims = self._store(tmp_path, forged).unverified_plugin_sources()
        assert [c.entry_name for c in claims] == ["planted"]

    @pytest.mark.parametrize("compact", [None, "", "   ", "not.a-jws", "a.b.c", b"\xff\xfe"])
    def test_no_file_or_an_unparseable_one_names_nothing(self, tmp_path, compact):
        assert self._store(tmp_path, compact).unverified_plugin_sources() == []

    @pytest.mark.parametrize("entries", [None, "plugins", {"kind": "plugins"}, 3])
    def test_entries_that_are_not_a_list_name_nothing(self, tmp_path, entries):
        store = self._store(tmp_path, _signed_with_entries(entries))
        assert store.unverified_plugin_sources() == []

    def test_a_pack_with_no_policy_names_nothing(self, tmp_path):
        compact = KEY.sign_absence()
        assert self._store(tmp_path, compact).unverified_plugin_sources() == []

    def test_a_malformed_entry_costs_only_itself(self, tmp_path):
        entries = [
            "junk",
            4,
            None,
            {"kind": ["not", "text"]},
            {"kind": "plugins", "entry_name": {"nested": 1}},
            _plugin("good"),
        ]
        claims = self._store(tmp_path, _signed_with_entries(entries)).unverified_plugin_sources()
        assert [c.entry_name for c in claims] == ["good"]


class TestPluginSourceClaim:
    def test_missing_fields_default_to_empty(self):
        claim = PluginSourceClaim.model_validate({})
        assert (claim.kind, claim.entry_name, claim.source_url) == ("", "", None)

    def test_other_keys_are_ignored(self):
        claim = PluginSourceClaim.model_validate({"kind": "plugins", "content": "x", "n": 1})
        assert claim.kind == "plugins"

    def test_a_number_is_read_as_its_text(self):
        claim = PluginSourceClaim.model_validate({"entry_name": 42, "source_url": 7})
        assert (claim.entry_name, claim.source_url) == ("42", "7")
