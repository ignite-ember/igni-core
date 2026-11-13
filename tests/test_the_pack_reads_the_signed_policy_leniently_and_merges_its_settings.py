"""The signed ``policy`` claim and the pack built from it.

Only the signed content becomes a pack, and a pack's ``settings`` entries
are the one kind merged instead of written. The reading is lenient on
purpose: a stricter one would refuse packs that apply today.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.pack import GroupPolicyPack, PackTheme, SignedPolicy

WHEN = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def _settings(name: str, content: str, **extra) -> GroupPolicyEntry:
    return GroupPolicyEntry(
        kind=extra.pop("kind", "settings"),
        entry_name=name,
        content=content,
        content_type=extra.pop("content_type", "json"),
        **extra,
    )


def _pack(*entries: GroupPolicyEntry) -> GroupPolicyPack:
    return GroupPolicyPack(group_id="g", group_name="G", fetched_at=WHEN, entries=list(entries))


class TestTheSignedPolicyIsReadLeniently:
    def test_null_names_become_empty_strings(self):
        policy = SignedPolicy.model_validate({"group_id": None, "group_name": None})

        assert (policy.group_id, policy.group_name) == ("", "")

    def test_absent_fields_take_their_defaults(self):
        policy = SignedPolicy.model_validate({})

        assert policy.group_id == "" and policy.group_name == ""
        assert policy.fetched_at is None
        assert policy.entries == []
        assert policy.default_model is None
        assert policy.theme is None

    def test_numbers_read_as_strings(self):
        policy = SignedPolicy.model_validate({"group_id": 42, "group_name": 7})

        assert (policy.group_id, policy.group_name) == ("42", "7")

    def test_a_null_or_empty_time_is_unknown(self):
        assert SignedPolicy.model_validate({"fetched_at": None}).fetched_at is None
        assert SignedPolicy.model_validate({"fetched_at": ""}).fetched_at is None

    def test_a_time_is_read(self):
        policy = SignedPolicy.model_validate({"fetched_at": "2030-01-02T03:04:05Z"})

        assert policy.fetched_at == WHEN

    def test_null_entries_are_none_at_all(self):
        assert SignedPolicy.model_validate({"entries": None}).entries == []

    def test_entries_are_read_as_entries(self):
        policy = SignedPolicy.model_validate(
            {
                "entries": [
                    {
                        "kind": "agents",
                        "entry_name": "a",
                        "content": "c",
                        "content_type": "markdown",
                    }
                ]
            }
        )

        assert [(e.kind, e.entry_name) for e in policy.entries] == [("agents", "a")]

    def test_an_entry_of_the_wrong_shape_is_refused(self):
        with pytest.raises(ValidationError):
            SignedPolicy.model_validate({"entries": [{"kind": "agents"}]})

    def test_extra_keys_are_ignored(self):
        policy = SignedPolicy.model_validate({"group_id": "g", "surprise": True})

        assert policy.group_id == "g"
        assert not hasattr(policy, "surprise")

    def test_the_theme_keeps_every_key_it_was_given(self):
        policy = SignedPolicy.model_validate({"theme": {"accent": "#abc", "wordmark": "Acme"}})

        assert policy.theme is not None
        assert policy.theme.model_dump() == {"accent": "#abc", "wordmark": "Acme"}


class TestAPackIsBuiltFromTheSignedContent:
    def test_it_carries_the_content_the_tag_and_the_signed_text(self):
        policy = SignedPolicy.model_validate(
            {
                "group_id": "g-1",
                "group_name": "Legal",
                "fetched_at": "2030-01-02T03:04:05Z",
                "default_model": "m-1",
                "theme": {"accent": "#abc"},
                "entries": [_settings("s", "{}").model_dump()],
            }
        )

        pack = GroupPolicyPack.from_signed(policy, etag="tag-1", signed_pack="a.b.c")

        assert (pack.group_id, pack.group_name) == ("g-1", "Legal")
        assert pack.fetched_at == WHEN
        assert pack.default_model == "m-1"
        assert pack.theme is not None and pack.theme.model_dump() == {"accent": "#abc"}
        assert [e.entry_name for e in pack.entries] == ["s"]
        assert (pack.etag, pack.signed_pack) == ("tag-1", "a.b.c")

    def test_a_missing_time_becomes_now(self):
        before = datetime.now(timezone.utc)

        pack = GroupPolicyPack.from_signed(SignedPolicy())

        assert before <= pack.fetched_at <= datetime.now(timezone.utc) + timedelta(seconds=1)
        assert pack.fetched_at.tzinfo is not None
        assert (pack.etag, pack.signed_pack) == (None, None)


class TestTheSettingsEntriesAreMerged:
    def test_an_empty_pack_is_an_empty_dict(self):
        assert _pack().to_settings_dict() == {}

    def test_later_entries_win_and_nested_maps_merge(self):
        pack = _pack(
            _settings("a", json.dumps({"x": 1, "nest": {"p": 1, "q": 1}})),
            _settings("b", json.dumps({"x": 2, "nest": {"q": 2, "r": 2}})),
        )

        assert pack.to_settings_dict() == {"x": 2, "nest": {"p": 1, "q": 2, "r": 2}}

    def test_a_disabled_entry_contributes_nothing(self):
        pack = _pack(_settings("a", '{"x": 1}'), _settings("b", '{"y": 2}', enabled=False))

        assert pack.to_settings_dict() == {"x": 1}

    def test_another_kind_is_not_settings(self):
        pack = _pack(_settings("a", '{"x": 1}', kind="agents"))

        assert pack.to_settings_dict() == {}

    def test_content_that_is_not_declared_json_is_ignored(self):
        pack = _pack(
            _settings("a", '{"x": 1}', content_type="yaml"),
            _settings("b", '{"y": 2}', content_type="markdown"),
        )

        assert pack.to_settings_dict() == {}

    def test_invalid_json_is_warned_about_and_the_others_still_apply(self, caplog):
        caplog.set_level(logging.WARNING)
        pack = _pack(_settings("bad", "{nope"), _settings("good", '{"y": 2}'))

        assert pack.to_settings_dict() == {"y": 2}
        assert any(
            "a settings entry was not applied" in r.getMessage() and r.levelno == logging.WARNING
            for r in caplog.records
        )

    @pytest.mark.parametrize("content", ["[1, 2]", '"text"', "3", "null"])
    def test_json_that_is_not_an_object_is_ignored(self, content):
        pack = _pack(_settings("a", content), _settings("b", '{"y": 2}'))

        assert pack.to_settings_dict() == {"y": 2}


class TestTheTheme:
    def test_it_passes_any_keys_through_unchanged(self):
        theme = PackTheme.model_validate({"accent": "#abc", "nested": {"a": [1, 2]}, "n": 3})

        assert theme.model_dump() == {"accent": "#abc", "nested": {"a": [1, 2]}, "n": 3}

    def test_it_is_frozen(self):
        theme = PackTheme.model_validate({"accent": "#abc"})

        with pytest.raises(ValidationError):
            theme.accent = "#000"
