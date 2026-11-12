"""An upload as PostgreSQL holds it: its size, its name, its copy, its end.

``igni_cloud.storage.attachments`` — what the existing storage tests leave
open: empty uploads, a hostile filename, the cached copy and when it is
rewritten, a copy that fails midway, and a delete that frees the object.
Each test needs ``IGNI_TEST_DATABASE_URL``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from igni_cloud.storage.attachments import LargeObjectAttachments
from sqlalchemy import select, text

from igni_core.core.db.database import Database
from igni_core.core.db.urls import DatabaseUrl
from igni_core.core.utils.attachment_store import AttachmentModel
from tests.storage_backends import Scopes


def _store(postgres_url: str, project: Path) -> tuple[LargeObjectAttachments, Database]:
    db = Database(DatabaseUrl.parse(postgres_url), scope=Scopes.fresh())
    return LargeObjectAttachments(db, project), db


def _row(db: Database, ref: str) -> AttachmentModel:
    with db.sync_session() as session:
        row = session.scalar(
            select(AttachmentModel).where(
                db.scope.where(AttachmentModel),
                AttachmentModel.id == ref.removeprefix(LargeObjectAttachments.REF_PREFIX),
            )
        )
        assert row is not None
        session.expunge(row)
        return row


def _object_exists(db: Database, oid: int) -> bool:
    with db.sync_session() as session:
        return bool(
            session.scalar(
                text("SELECT count(*) FROM pg_largeobject_metadata WHERE oid = :oid"),
                {"oid": oid},
            )
        )


def test_it_refuses_a_database_that_is_not_postgresql(tmp_path: Path):
    db = Database(DatabaseUrl.parse(f"sqlite:///{tmp_path / 'x.db'}"))
    with pytest.raises(ValueError, match="PostgreSQL"):
        LargeObjectAttachments(db, tmp_path)


def test_an_upload_without_bytes_is_an_empty_object_of_size_zero(
    postgres_url: str, tmp_path: Path
):
    store, db = _store(postgres_url, tmp_path)
    for chunks in ([], [b"", b""]):
        stored = store.save("s1", "empty.txt", chunks)
        assert stored.size == 0
        assert _row(db, stored.ref).size == 0
        assert list(store.open(stored.ref)) == []
        path = store.local_path(stored.ref)
        assert path is not None and path.read_bytes() == b""


def test_empty_chunks_are_skipped_and_only_real_bytes_counted(
    postgres_url: str, tmp_path: Path
):
    store, db = _store(postgres_url, tmp_path)
    stored = store.save("s1", "a.txt", [b"ab", b"", b"cde", b""])
    assert stored.size == 5
    assert _row(db, stored.ref).size == 5
    assert b"".join(store.open(stored.ref)) == b"abcde"


def test_a_hostile_filename_is_stored_safe_and_the_copy_stays_in_its_directory(
    postgres_url: str, tmp_path: Path
):
    store, db = _store(postgres_url, tmp_path)
    stored = store.save("s1", "../../etc/pass wd.txt", [b"x"])
    name = _row(db, stored.ref).filename
    assert "/" not in name
    assert name == LargeObjectAttachments.safe_name("../../etc/pass wd.txt")
    path = store.local_path(stored.ref)
    assert path is not None
    assert path.parent == tmp_path / ".igni" / "attachments" / "s1"
    assert path.name == f"{_row(db, stored.ref).id}-{name}"
    unicode_ref = store.save("s1", "résumé 日本.txt", [b"u"]).ref
    unicode_path = store.local_path(unicode_ref)
    assert unicode_path is not None and unicode_path.read_bytes() == b"u"


def test_the_copy_is_reused_when_its_size_matches_and_rewritten_when_not(
    postgres_url: str, tmp_path: Path
):
    store, _ = _store(postgres_url, tmp_path)
    stored = store.save("s1", "a.txt", [b"hello"])
    path = store.local_path(stored.ref)
    assert path is not None and path.read_bytes() == b"hello"

    path.write_bytes(b"HELLO")  # right size: kept as it is, not read again
    assert store.local_path(stored.ref) == path
    assert path.read_bytes() == b"HELLO"

    path.write_bytes(b"hel")  # truncated: made again from the object
    assert store.local_path(stored.ref) == path
    assert path.read_bytes() == b"hello"
    path.write_bytes(b"hello world")  # stale: too long
    assert store.local_path(stored.ref) == path
    assert path.read_bytes() == b"hello"
    assert not list(path.parent.glob("*.partial"))


def test_a_copy_that_fails_midway_leaves_no_final_file(postgres_url: str, tmp_path: Path):
    store, db = _store(postgres_url, tmp_path)
    stored = store.save("s1", "a.txt", [b"hello"])
    row = _row(db, stored.ref)
    with db.sync_session() as session, session.begin():
        session.execute(text("SELECT lo_unlink(:oid)"), {"oid": row.oid})
    with pytest.raises(Exception):  # noqa: B017 - the driver's "large object does not exist"
        store.local_path(stored.ref)
    directory = tmp_path / ".igni" / "attachments" / "s1"
    assert not (directory / f"{row.id}-a.txt").exists()
    with pytest.raises(Exception):  # noqa: B017
        list(store.open(stored.ref))


def test_a_path_or_a_reference_that_is_not_there_has_no_local_path(
    postgres_url: str, tmp_path: Path
):
    store, _ = _store(postgres_url, tmp_path)
    mine = tmp_path / "mine.txt"
    mine.write_text("on disk")
    assert store.local_path(str(mine)) == mine
    assert store.local_path(str(tmp_path / "missing.txt")) is None
    assert store.local_path(str(tmp_path)) is None, "a directory is no file"
    assert store.local_path(LargeObjectAttachments.REF_PREFIX + "0" * 32) is None

    other, _ = _store(postgres_url, tmp_path)
    foreign = other.save("s1", "a.txt", [b"secret"]).ref
    assert store.local_path(foreign) is None
    assert other.local_path(foreign) is not None


def test_a_delete_frees_the_large_object_and_the_row(postgres_url: str, tmp_path: Path):
    store, db = _store(postgres_url, tmp_path)
    stored = store.save("s1", "a.txt", [b"hello"])
    oid = _row(db, stored.ref).oid
    assert _object_exists(db, oid)
    assert store.delete(stored.ref) is True
    assert not _object_exists(db, oid)
    assert store.local_path(stored.ref) is None
    with pytest.raises(FileNotFoundError):
        list(store.open(stored.ref))
    assert store.delete(stored.ref) is False


def test_without_a_project_the_copies_go_under_the_working_directory(
    postgres_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    here = tmp_path / "here"
    here.mkdir()
    monkeypatch.setattr("pathlib.Path.cwd", lambda: here)  # the suite already fakes it
    db = Database(DatabaseUrl.parse(postgres_url), scope=Scopes.fresh())
    store = LargeObjectAttachments(db)
    stored = store.save("s9", "a.txt", [b"hi"])
    path = store.local_path(stored.ref)
    assert path is not None
    assert path.resolve().is_relative_to((here / ".igni" / "attachments" / "s9").resolve())


def test_an_upload_larger_than_one_chunk_reads_back_in_bounded_pieces(
    postgres_url: str, tmp_path: Path
):
    store, _ = _store(postgres_url, tmp_path)
    payload = bytes(range(256)) * ((2 * LargeObjectAttachments.CHUNK + 300) // 256 + 1)

    def pieces() -> Iterator[bytes]:
        for start in range(0, len(payload), 7000):
            yield payload[start : start + 7000]

    stored = store.save("s1", "big.bin", pieces())
    read = list(store.open(stored.ref))
    assert b"".join(read) == payload and len(read) >= 3
    assert all(0 < len(p) <= LargeObjectAttachments.CHUNK for p in read)
