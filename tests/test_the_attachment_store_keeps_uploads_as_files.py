"""The desktop's upload store, its base64 slicing and the reference rewrite.

Real files under ``tmp_path``; the store is never mocked. The cloud's
store has its own tests in ``tests/test_storage_backends.py``.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterator
from pathlib import Path

import pytest

from igni_core.core.db.locator import StorageLocator
from igni_core.core.db.providers import SharedStorage, SharedStorages
from igni_core.core.db.scope import Tenant, tenancy
from igni_core.core.utils.attachment_store import (
    AttachmentReferences,
    Attachments,
    Base64Chunks,
    DirectoryAttachments,
)


def _ref(char: str = "a") -> str:
    return "attachment:" + char * 32


class TestBase64Chunks:
    def test_an_empty_string_yields_no_chunks(self):
        assert list(Base64Chunks("")) == []

    def test_whitespace_only_yields_no_chunks(self):
        assert list(Base64Chunks(" \n\t ")) == []

    def test_a_slice_is_a_multiple_of_four_characters(self):
        assert Base64Chunks.SLICE % 4 == 0

    def test_text_exactly_one_slice_long_is_one_chunk(self):
        payload = b"x" * (Base64Chunks.SLICE // 4 * 3)
        chunks = list(Base64Chunks(base64.b64encode(payload).decode()))
        assert chunks == [payload]

    def test_text_one_group_over_a_slice_is_two_chunks(self):
        payload = b"y" * (Base64Chunks.SLICE // 4 * 3 + 3)
        chunks = list(Base64Chunks(base64.b64encode(payload).decode()))
        assert [len(c) for c in chunks] == [Base64Chunks.SLICE // 4 * 3, 3]
        assert b"".join(chunks) == payload

    def test_whitespace_inside_a_slice_does_not_shift_the_slices(self):
        payload = bytes(range(256)) * 600
        encoded = base64.b64encode(payload).decode()
        wrapped = "\r\n".join(encoded[i : i + 76] for i in range(0, len(encoded), 76))
        assert b"".join(Base64Chunks(wrapped)) == payload


class TestSafeNames:
    @pytest.mark.parametrize(
        ("filename", "safe"),
        [
            ("a b.bin", "a_b.bin"),
            ("../../etc/passwd", ".._.._etc_passwd"),
            ("ключ.txt", "_.txt"),
            ("a  \t b", "a_b"),
            ("keep-This_one.v2.TXT", "keep-This_one.v2.TXT"),
        ],
    )
    def test_anything_outside_the_safe_set_becomes_one_underscore_per_run(
        self, filename: str, safe: str
    ):
        assert Attachments.safe_name(filename) == safe

    def test_an_empty_name_is_called_file(self):
        assert Attachments.safe_name("") == "file"

    def test_a_name_of_only_illegal_characters_is_an_underscore(self):
        assert Attachments.safe_name("///") == "_"

    @pytest.mark.parametrize("session_id", ["", ".", "..", "....", "/"])
    def test_a_session_id_with_nothing_safe_left_is_called_session(self, session_id: str):
        expected = "_" if session_id == "/" else "session"
        assert Attachments.session_directory(session_id) == expected

    def test_dots_are_trimmed_from_both_ends_of_a_session_id(self):
        assert Attachments.session_directory("..a.b..") == "a.b"

    def test_the_attachments_directory_is_under_the_project_config_dir(self, tmp_path: Path):
        assert Attachments.directory(tmp_path) == tmp_path / ".igni" / "attachments"


class TestSaving:
    def test_an_empty_upload_is_an_empty_file_of_size_zero(self, tmp_path: Path):
        stored = DirectoryAttachments(tmp_path).save("s1", "empty.txt", [])
        assert stored.size == 0
        assert Path(stored.ref).read_bytes() == b""

    def test_the_size_is_the_sum_of_the_chunks_and_the_ref_is_the_path(self, tmp_path: Path):
        stored = DirectoryAttachments(tmp_path).save("s1", "a.txt", [b"ab", b"", b"cde"])
        assert stored.size == 5
        assert stored.ref == str(Attachments.directory(tmp_path) / "s1" / "a.txt")
        assert Path(stored.ref).read_bytes() == b"abcde"

    def test_a_name_that_is_taken_gets_the_next_number_before_its_extension(self, tmp_path: Path):
        store = DirectoryAttachments(tmp_path)
        refs = [store.save("s1", "a.txt", [bytes([i])]).ref for i in range(3)]
        assert [Path(r).name for r in refs] == ["a.txt", "a-2.txt", "a-3.txt"]
        assert [Path(r).read_bytes() for r in refs] == [b"\x00", b"\x01", b"\x02"]

    def test_a_name_without_an_extension_is_numbered_at_the_end(self, tmp_path: Path):
        store = DirectoryAttachments(tmp_path)
        names = [Path(store.save("s1", "README", [b"x"]).ref).name for _ in range(2)]
        assert names == ["README", "README-2"]

    def test_a_dotfile_is_numbered_before_its_dot_name(self, tmp_path: Path):
        store = DirectoryAttachments(tmp_path)
        names = [Path(store.save("s1", ".env", [b"x"]).ref).name for _ in range(2)]
        assert names == [".env", "-2.env"]

    def test_the_same_name_in_another_session_is_not_a_collision(self, tmp_path: Path):
        store = DirectoryAttachments(tmp_path)
        first = store.save("s1", "a.txt", [b"1"])
        second = store.save("s2", "a.txt", [b"2"])
        assert Path(first.ref).name == Path(second.ref).name == "a.txt"
        assert Path(first.ref).read_bytes() == b"1"

    def test_a_source_that_fails_midway_leaves_no_partial_file(self, tmp_path: Path):
        def chunks() -> Iterator[bytes]:
            yield b"first half"
            raise RuntimeError("connection dropped")

        store = DirectoryAttachments(tmp_path)
        with pytest.raises(RuntimeError, match="connection dropped"):
            store.save("s1", "a.txt", chunks())
        assert list((Attachments.directory(tmp_path) / "s1").iterdir()) == []
        assert Path(store.save("s1", "a.txt", [b"ok"]).ref).name == "a.txt"

    def test_an_interrupt_midway_also_removes_the_partial_file(self, tmp_path: Path):
        def chunks() -> Iterator[bytes]:
            yield b"x"
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            DirectoryAttachments(tmp_path).save("s1", "a.txt", chunks())
        assert list((Attachments.directory(tmp_path) / "s1").iterdir()) == []

    def test_invalid_base64_fails_the_save_and_leaves_nothing_behind(self, tmp_path: Path):
        good = base64.b64encode(b"z" * (Base64Chunks.SLICE // 4 * 3)).decode()
        with pytest.raises(binascii.Error):
            DirectoryAttachments(tmp_path).save("s1", "a.bin", Base64Chunks(good + "A"))
        assert list((Attachments.directory(tmp_path) / "s1").iterdir()) == []


class TestReading:
    def test_a_large_upload_is_read_back_in_chunks_of_the_store_size(self, tmp_path: Path):
        store = DirectoryAttachments(tmp_path)
        payload = b"q" * (Attachments.CHUNK * 2 + 5)
        chunks = list(store.open(store.save("s1", "big.bin", [payload]).ref))
        assert [len(c) for c in chunks] == [Attachments.CHUNK, Attachments.CHUNK, 5]
        assert b"".join(chunks) == payload

    def test_an_attachment_id_is_not_a_file_on_the_desktop(self, tmp_path: Path):
        store = DirectoryAttachments(tmp_path)
        with pytest.raises(FileNotFoundError, match="keeps attachments as files"):
            store.open(_ref())
        assert store.local_path(_ref()) is None

    def test_a_directory_inside_the_attachments_root_is_no_upload(self, tmp_path: Path):
        store = DirectoryAttachments(tmp_path)
        stored = store.save("s1", "a.txt", [b"x"])
        directory = str(Path(stored.ref).parent)
        assert store.local_path(directory) is None
        with pytest.raises(FileNotFoundError, match="not an upload"):
            store.open(directory)

    def test_a_missing_file_inside_the_root_is_no_upload(self, tmp_path: Path):
        store = DirectoryAttachments(tmp_path)
        missing = str(Attachments.directory(tmp_path) / "s1" / "nope.txt")
        assert store.local_path(missing) is None
        with pytest.raises(FileNotFoundError):
            store.open(missing)

    def test_a_symlink_out_of_the_root_is_no_upload(self, tmp_path: Path):
        project = tmp_path / "project"
        store = DirectoryAttachments(project)
        stored = store.save("s1", "a.txt", [b"x"])
        secret = tmp_path / "secret.txt"
        secret.write_text("secret")
        link = Path(stored.ref).parent / "link.txt"
        link.symlink_to(secret)
        assert store.local_path(str(link)) is None
        with pytest.raises(FileNotFoundError):
            store.open(str(link))

    @pytest.mark.parametrize("ref", ["", "bad\0ref", "~/x"])
    def test_a_reference_that_is_not_a_usable_path_is_no_upload(self, tmp_path: Path, ref: str):
        store = DirectoryAttachments(tmp_path)
        assert store.local_path(ref) is None
        with pytest.raises(FileNotFoundError):
            store.open(ref)


class TestChoosingTheStore:
    def test_a_shared_database_whose_dialect_keeps_no_attachments_falls_back_to_files(
        self, tmp_path: Path
    ):
        locator = StorageLocator(
            database_url=f"sqlite:///{tmp_path / 'shared.db'}", data_dir=tmp_path
        )
        tenancy.use(lambda: Tenant(org="acme", producer="person:ada"))
        assert locator.shared
        assert isinstance(Attachments.for_project(tmp_path, locator), DirectoryAttachments)

    def test_a_shared_database_whose_dialect_keeps_attachments_gives_its_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        locator = StorageLocator(
            database_url=f"sqlite:///{tmp_path / 'shared.db'}", data_dir=tmp_path
        )
        tenancy.use(lambda: Tenant(org="acme", producer="person:ada"))
        own = DirectoryAttachments(tmp_path / "elsewhere")
        asked: list[Path] = []

        class Keeps(SharedStorage):
            def agno_db(self, **kwargs):  # pragma: no cover - not asked
                raise AssertionError

            def attachments(self, db, project_dir):
                asked.append(project_dir)
                return own

        dialect = locator.project_url(tmp_path).dialect
        monkeypatch.setitem(SharedStorages._found, dialect, Keeps())
        assert Attachments.for_project(tmp_path, locator) is own
        assert asked == [tmp_path]


class _Held(Attachments):
    """A store that holds some references, for the rewrite."""

    def __init__(self, held: dict[str, Path]) -> None:
        self._held = held

    def save(self, session_id, filename, chunks):  # pragma: no cover - unused
        raise NotImplementedError

    def open(self, ref):  # pragma: no cover - unused
        raise NotImplementedError

    def local_path(self, ref: str) -> Path | None:
        return self._held.get(ref)


class TestAttachmentReferences:
    def test_a_reference_of_32_lowercase_hex_digits_is_present(self):
        assert AttachmentReferences.present(f"see {_ref('0')} please")

    @pytest.mark.parametrize(
        "text",
        ["", "no reference", "attachment:" + "A" * 32, "attachment:" + "a" * 31, "attachment:xyz"],
    )
    def test_anything_else_is_not(self, text: str):
        assert not AttachmentReferences.present(text)

    def test_each_held_reference_becomes_its_readable_path(self):
        store = _Held({_ref("a"): Path("/tmp/one.png"), _ref("b"): Path("/tmp/two.png")})
        text = f"look at {_ref('a')} and {_ref('b')}, then {_ref('a')} again"
        assert AttachmentReferences.localize(text, store) == (
            "look at /tmp/one.png and /tmp/two.png, then /tmp/one.png again"
        )

    def test_a_reference_the_store_does_not_hold_stays_literal(self):
        store = _Held({_ref("a"): Path("/tmp/one.png")})
        text = f"{_ref('a')} {_ref('c')}"
        assert AttachmentReferences.localize(text, store) == f"/tmp/one.png {_ref('c')}"

    def test_text_without_references_comes_back_unchanged(self):
        assert AttachmentReferences.localize("plain text", _Held({})) == "plain text"

    def test_the_desktop_store_leaves_every_reference_literal(self, tmp_path: Path):
        text = f"here: {_ref('d')}"
        assert AttachmentReferences.localize(text, DirectoryAttachments(tmp_path)) == text
