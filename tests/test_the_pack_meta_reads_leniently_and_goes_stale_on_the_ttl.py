"""``pack_meta.json``: what it keeps, how it reads, and when it says "fetch again".

The meta is the pack's identity for the frontend and its freshness for the
poller. A file written by an older release, or damaged, must read as
"stale" and never as a crash.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.config.group_policy.meta import CACHE_TTL_SECONDS, PackMeta, PackMetaStore
from igni_core.core.config.group_policy.pack import GroupPolicyPack, PackTheme

NOW = datetime(2030, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def layout(tmp_path: Path) -> GroupPolicyLayout:
    return GroupPolicyLayout(root=tmp_path / "group-policy")


@pytest.fixture
def store(layout: GroupPolicyLayout) -> PackMetaStore:
    return PackMetaStore(layout)


def _write(layout: GroupPolicyLayout, text: str) -> None:
    layout.root.mkdir(parents=True, exist_ok=True)
    layout.file(GroupPolicyLayout.META_FILE).write_text(text)


class TestTheModelReadsPermissively:
    def test_an_empty_object_is_a_meta_that_knows_nothing(self):
        meta = PackMeta.model_validate({})

        assert meta.group_id is None
        assert meta.fetched_at is None
        assert meta.entry_count == 0
        assert meta.etag is None
        assert meta.theme is None

    def test_unknown_keys_are_ignored(self):
        meta = PackMeta.model_validate({"etag": "t", "from_the_future": [1, 2]})

        assert meta.etag == "t"
        assert not hasattr(meta, "from_the_future")

    def test_an_unreadable_time_becomes_unknown_and_keeps_the_tag(self):
        meta = PackMeta.model_validate({"fetched_at": "last tuesday", "etag": "abc"})

        assert meta.fetched_at is None
        assert meta.etag == "abc"
        assert meta.is_stale(NOW)

    def test_a_time_with_a_z_suffix_is_read(self):
        meta = PackMeta.model_validate({"fetched_at": "2030-06-01T12:00:00Z"})

        assert meta.fetched_at == NOW

    def test_the_meta_is_frozen(self):
        meta = PackMeta()

        with pytest.raises(ValidationError):
            meta.etag = "x"

    def test_a_pack_gives_its_identity_count_model_tag_and_theme(self):
        pack = GroupPolicyPack(
            group_id="g-1",
            group_name="Legal",
            fetched_at=NOW,
            entries=[],
            default_model="m-1",
            etag="e-1",
            theme=PackTheme.model_validate({"accent": "#fff"}),
        )

        meta = PackMeta.from_pack(pack)

        assert (meta.group_id, meta.group_name) == ("g-1", "Legal")
        assert meta.fetched_at == NOW
        assert meta.entry_count == 0
        assert (meta.default_model, meta.etag) == ("m-1", "e-1")
        assert meta.theme is not None and meta.theme.model_dump() == {"accent": "#fff"}

    def test_absence_keeps_only_the_tag_and_the_time(self):
        meta = PackMeta.absence("tag-9", NOW)

        assert meta == PackMeta(etag="tag-9", fetched_at=NOW, entry_count=0)
        assert meta.group_id is None and meta.theme is None

    def test_touched_restamps_the_time_and_nothing_else(self):
        meta = PackMeta(group_id="g", etag="e", entry_count=3, fetched_at=NOW)
        later = NOW + timedelta(hours=2)

        touched = meta.touched(later)

        assert touched.fetched_at == later
        assert touched.model_copy(update={"fetched_at": NOW}) == meta
        assert meta.fetched_at == NOW


class TestFreshness:
    def test_the_ttl_is_five_minutes(self):
        assert CACHE_TTL_SECONDS == 300

    def test_a_meta_with_no_time_is_stale(self):
        assert PackMeta().is_stale(NOW)

    def test_exactly_the_ttl_old_is_still_fresh(self):
        meta = PackMeta(fetched_at=NOW - timedelta(seconds=300))

        assert not meta.is_stale(NOW)

    def test_a_second_past_the_ttl_is_stale(self):
        meta = PackMeta(fetched_at=NOW - timedelta(seconds=301))

        assert meta.is_stale(NOW)

    def test_a_given_ttl_replaces_the_default(self):
        meta = PackMeta(fetched_at=NOW - timedelta(seconds=30))

        assert meta.is_stale(NOW, ttl_seconds=10)
        assert not meta.is_stale(NOW, ttl_seconds=60)

    def test_a_naive_time_is_read_as_utc(self):
        meta = PackMeta(fetched_at=datetime(2030, 6, 1, 11, 59, 0))

        assert not meta.is_stale(NOW)
        assert meta.is_stale(NOW + timedelta(minutes=10))

    def test_a_time_in_another_zone_is_compared_as_the_same_instant(self):
        plus_two = timezone(timedelta(hours=2))
        meta = PackMeta(fetched_at=datetime(2030, 6, 1, 14, 0, 0, tzinfo=plus_two))

        assert meta.is_stale(NOW + timedelta(seconds=301))
        assert not meta.is_stale(NOW + timedelta(seconds=299))


class TestTheStoreOnDisk:
    def test_a_missing_file_loads_as_none(self, store):
        assert store.load() is None

    @pytest.mark.parametrize("text", ["", "not json", "[1, 2]", '{"entry_count": "many"}'])
    def test_a_file_that_does_not_parse_loads_as_none(self, store, layout, text):
        _write(layout, text)

        assert store.load() is None

    def test_save_creates_the_directory_and_writes_isoformat_not_z(self, store, layout):
        store.save(PackMeta(etag="e", fetched_at=NOW))

        raw = json.loads(layout.file(GroupPolicyLayout.META_FILE).read_text())
        assert raw["fetched_at"] == "2030-06-01T12:00:00+00:00"
        assert raw["etag"] == "e"
        assert store.load() == PackMeta(etag="e", fetched_at=NOW)

    def test_a_meta_without_a_time_is_saved_with_null(self, store, layout):
        store.save(PackMeta(etag="e"))

        raw = json.loads(layout.file(GroupPolicyLayout.META_FILE).read_text())
        assert raw["fetched_at"] is None

    def test_a_garbage_time_on_disk_keeps_the_tag_and_reads_stale(self, store, layout):
        _write(layout, json.dumps({"etag": "kept", "fetched_at": "garbage"}))

        assert store.stored_etag() == "kept"
        assert store.is_stale()

    def test_touch_with_nothing_stored_writes_nothing(self, store, layout):
        store.touch()

        assert not layout.file(GroupPolicyLayout.META_FILE).exists()

    def test_touch_restamps_a_stored_meta_to_now_and_keeps_the_rest(self, store):
        store.save(PackMeta(group_id="g", etag="e", entry_count=2, fetched_at=NOW))

        store.touch()

        loaded = store.load()
        assert loaded is not None
        assert loaded.fetched_at is not None
        assert abs(datetime.now(timezone.utc) - loaded.fetched_at) < timedelta(seconds=60)
        assert (loaded.group_id, loaded.etag, loaded.entry_count) == ("g", "e", 2)
        assert not store.is_stale()

    def test_a_missing_meta_is_stale(self, store):
        assert store.is_stale()

    def test_a_meta_confirmed_just_now_is_fresh_and_an_old_one_is_stale(self, store):
        store.save(PackMeta(fetched_at=datetime.now(timezone.utc)))
        assert not store.is_stale()

        store.save(PackMeta(fetched_at=datetime.now(timezone.utc) - timedelta(seconds=400)))
        assert store.is_stale()

    def test_the_stored_tag_is_the_tag_or_none(self, store, layout):
        assert store.stored_etag() is None

        store.save(PackMeta(etag="abc"))
        assert store.stored_etag() == "abc"

        store.save(PackMeta(etag=""))
        assert store.stored_etag() is None

        store.save(PackMeta())
        assert store.stored_etag() is None
