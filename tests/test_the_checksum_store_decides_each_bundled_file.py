"""``ChecksumStore``: what is recorded, and what happens to each bundled file.

The store is the memory behind "igni updates a bundled agent or skill unless
you edited it". Each outcome has a different promise to the person: a missing
file is supplied, a file igni never recorded is left alone, a file the package
did not change is not touched, an untouched copy is updated, and an edited
copy is kept with the new version beside it as ``<name>.new``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from igni_core.core.init.checksum_store import ChecksumStore
from igni_core.core.init.schemas import InitConfig
from igni_core.core.paths import CONFIG_DIR

KEY = "agents/x.md"


def _store(project: Path) -> ChecksumStore:
    return ChecksumStore.load(project, InitConfig())


def _src(tmp_path: Path, text: str) -> Path:
    src = tmp_path / "pkg" / "x.md"
    src.parent.mkdir(exist_ok=True)
    src.write_text(text)
    return src


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class TestHash:
    def test_it_is_the_first_sixteen_hex_digits_of_the_sha256_of_the_bytes(self, tmp_path):
        f = tmp_path / "f"
        f.write_bytes(b"\xff\x00 not text")

        assert ChecksumStore.file_hash(f) == hashlib.sha256(b"\xff\x00 not text").hexdigest()[:16]

    def test_an_empty_file_has_the_hash_of_nothing(self, tmp_path):
        f = tmp_path / "f"
        f.write_bytes(b"")

        assert ChecksumStore.file_hash(f) == "e3b0c44298fc1c14"


class TestPersistence:
    def test_a_missing_file_is_an_empty_store(self, tmp_path):
        assert _store(tmp_path).entries == {}

    def test_a_corrupt_file_is_an_empty_store(self, tmp_path):
        path = tmp_path / CONFIG_DIR / ".checksums.json"
        path.parent.mkdir()
        path.write_text("{broken")

        assert _store(tmp_path).entries == {}

    def test_save_then_load_round_trips_through_the_configured_file(self, tmp_path):
        store = _store(tmp_path)
        store.entries["skills/s/SKILL.md"] = "abc"

        store.save()

        path = tmp_path / CONFIG_DIR / ".checksums.json"
        assert json.loads(path.read_text()) == {"skills/s/SKILL.md": "abc"}
        assert _store(tmp_path).entries == {"skills/s/SKILL.md": "abc"}

    def test_the_file_name_comes_from_the_config(self, tmp_path):
        config = InitConfig(checksums_file="sums.json")
        store = ChecksumStore.load(tmp_path, config)
        store.entries["k"] = "v"

        store.save()

        assert json.loads((tmp_path / CONFIG_DIR / "sums.json").read_text()) == {"k": "v"}


class TestSyncFile:
    def test_a_missing_destination_is_copied_and_recorded(self, tmp_path):
        store = _store(tmp_path)
        src, dst = _src(tmp_path, "v1"), tmp_path / "dst.md"

        outcome = store.sync_file(src, dst, KEY)

        assert outcome.kind == "copied"
        assert outcome.warning_message() is None
        assert dst.read_text() == "v1"
        assert store.entries == {KEY: _hash("v1")}

    def test_a_file_igni_never_recorded_is_recorded_and_left_untouched(self, tmp_path):
        store = _store(tmp_path)
        src, dst = _src(tmp_path, "v1"), tmp_path / "dst.md"
        dst.write_text("the person's own")

        outcome = store.sync_file(src, dst, KEY)

        assert outcome.kind == "recorded_legacy"
        assert outcome.warning_message() is None
        assert dst.read_text() == "the person's own"
        assert store.entries == {KEY: _hash("v1")}
        assert not dst.with_suffix(".md.new").exists()

    def test_a_package_that_did_not_change_touches_nothing(self, tmp_path):
        store = _store(tmp_path)
        src, dst = _src(tmp_path, "v1"), tmp_path / "dst.md"
        store.sync_file(src, dst, KEY)
        dst.write_text("edited since")

        outcome = store.sync_file(src, dst, KEY)

        assert outcome.kind == "unchanged"
        assert dst.read_text() == "edited since"
        assert store.entries == {KEY: _hash("v1")}
        assert not dst.with_suffix(".md.new").exists()

    def test_an_untouched_copy_is_overwritten_and_the_new_hash_recorded(self, tmp_path):
        store = _store(tmp_path)
        src, dst = _src(tmp_path, "v1"), tmp_path / "dst.md"
        store.sync_file(src, dst, KEY)
        src.write_text("v2")

        outcome = store.sync_file(src, dst, KEY)

        assert outcome.kind == "overwritten"
        assert outcome.warning_message() is None
        assert dst.read_text() == "v2"
        assert store.entries == {KEY: _hash("v2")}

    def test_an_edited_copy_is_kept_with_the_new_version_beside_it(self, tmp_path):
        store = _store(tmp_path)
        src, dst = _src(tmp_path, "v1"), tmp_path / "dst.md"
        store.sync_file(src, dst, KEY)
        dst.write_text("my edit")
        src.write_text("v2")

        outcome = store.sync_file(src, dst, KEY)

        sidecar = tmp_path / "dst.md.new"
        assert outcome.kind == "diverged"
        assert outcome.sidecar_path == sidecar
        assert dst.read_text() == "my edit"
        assert sidecar.read_text() == "v2"
        assert KEY in (outcome.warning_message() or "")
        assert store.entries == {KEY: _hash("v2")}

    def test_a_second_run_after_a_divergence_does_not_warn_again(self, tmp_path):
        store = _store(tmp_path)
        src, dst = _src(tmp_path, "v1"), tmp_path / "dst.md"
        store.sync_file(src, dst, KEY)
        dst.write_text("my edit")
        src.write_text("v2")
        store.sync_file(src, dst, KEY)

        outcome = store.sync_file(src, dst, KEY)

        assert outcome.kind == "unchanged"
        assert outcome.warning_message() is None

    def test_an_existing_sidecar_is_replaced_by_the_newest_version(self, tmp_path):
        store = _store(tmp_path)
        src, dst = _src(tmp_path, "v1"), tmp_path / "dst.md"
        store.sync_file(src, dst, KEY)
        dst.write_text("my edit")
        (tmp_path / "dst.md.new").write_text("stale sidecar")
        src.write_text("v2")

        store.sync_file(src, dst, KEY)

        assert (tmp_path / "dst.md.new").read_text() == "v2"
