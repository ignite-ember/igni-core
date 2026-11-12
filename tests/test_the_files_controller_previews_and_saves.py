"""The FE's file preview and upload, as ``FilesController`` answers them.

``test_file_preview_sandbox`` pins which directories a preview may read;
this file pins what it answers for a path that is allowed — the text, the
size and the highlight hint — and for each way a read can fail, and what
``upload_attachment`` hands back, including that the decode and the write
leave the event loop free.
"""

from __future__ import annotations

import asyncio
import base64
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from igni_core.backend.server_files import FilesController
from igni_core.core.utils.attachment_store import (
    Attachments,
    DirectoryAttachments,
)

CAP = 256 * 1024


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    return root


def _controller(project: Path) -> FilesController:
    session = SimpleNamespace(
        project_dir=project,
        session_id="s-1",
        settings=SimpleNamespace(storage=SimpleNamespace(database_url=None, data_dir=project)),
    )
    return FilesController(session)  # type: ignore[arg-type]


class TestWhatAReadAnswers:
    def test_the_text_the_size_and_the_language(self, project: Path) -> None:
        (project / "app.py").write_text("print('hi')\n")

        result = _controller(project).read_file("app.py")

        assert result.error == ""
        assert result.contents == "print('hi')\n"
        assert result.size == len("print('hi')\n")
        assert result.language == "python"
        assert result.path == str((project / "app.py").resolve())

    def test_an_absolute_path_inside_the_project(self, project: Path) -> None:
        (project / "notes.md").write_text("# hi")

        result = _controller(project).read_file(str(project / "notes.md"))

        assert result.contents == "# hi"
        assert result.language == "markdown"

    def test_an_unknown_extension_has_no_language(self, project: Path) -> None:
        (project / "data.xyz").write_text("x")

        assert _controller(project).read_file("data.xyz").language == ""

    def test_an_empty_path_is_the_project_directory(self, project: Path) -> None:
        result = _controller(project).read_file("")

        assert result.error == "Path is a directory."
        assert result.contents == ""
        assert result.size == 0

    def test_a_directory(self, project: Path) -> None:
        (project / "sub").mkdir()

        result = _controller(project).read_file("sub")

        assert result.error == "Path is a directory."
        assert result.path == str((project / "sub").resolve())

    def test_a_missing_file(self, project: Path) -> None:
        result = _controller(project).read_file("nope.txt")

        assert result.error == "File not found."
        assert (result.contents, result.size, result.language) == ("", 0, "")

    def test_a_file_of_exactly_the_cap_is_read(self, project: Path) -> None:
        (project / "edge.txt").write_text("a" * CAP)

        result = _controller(project).read_file("edge.txt")

        assert result.error == ""
        assert result.size == CAP
        assert len(result.contents) == CAP

    def test_one_byte_over_the_cap_is_refused_with_its_real_size(self, project: Path) -> None:
        (project / "big.txt").write_text("a" * (CAP + 1))

        result = _controller(project).read_file("big.txt")

        assert result.error == f"File too large to preview ({CAP + 1} bytes; cap {CAP})."
        assert result.contents == ""
        assert result.size == CAP + 1

    def test_bytes_that_are_not_utf8_are_replaced_not_raised(self, project: Path) -> None:
        (project / "bin.dat").write_bytes(b"ok\xff\xfe!")

        result = _controller(project).read_file("bin.dat")

        assert result.error == ""
        assert result.contents == "ok��!"
        assert result.size == 5

    def test_a_file_the_os_will_not_read_says_so(self, project: Path) -> None:
        secret = project / "locked.txt"
        secret.write_text("x")
        secret.chmod(0)
        try:
            result = _controller(project).read_file("locked.txt")
        finally:
            secret.chmod(0o600)

        if result.contents == "x":
            pytest.skip("running as a user that ignores file modes")
        assert result.error.startswith("read failed: ")
        assert result.contents == ""
        assert result.size == 1

    def test_a_file_that_vanishes_between_the_check_and_the_read_is_an_answer(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        victim = project / "gone.txt"
        victim.write_text("x")
        real_is_dir = Path.is_dir

        def delete_then_answer(self: Path) -> bool:
            if self.name == "gone.txt":
                self.unlink()
            return real_is_dir(self)

        monkeypatch.setattr(Path, "is_dir", delete_then_answer)

        result = _controller(project).read_file("gone.txt")

        assert result.contents == ""
        assert result.error


class _KeptElsewhere(Attachments):
    """An attachment store that keeps its uploads outside any project."""

    def __init__(self, files: dict[str, Path]) -> None:
        self._files = files

    def save(self, session_id: str, filename: str, chunks):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def open(self, ref: str):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def local_path(self, ref: str) -> Path | None:
        return self._files.get(ref)


class TestAnAttachmentReference:
    REF = "attachment:" + "a" * 32

    def test_a_held_upload_is_read_through_the_store(
        self, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The store's copy sits under the sandbox's roots, as the cloud's
        # materialised copy does.
        kept = project / "from-the-database.txt"
        kept.write_text("from the database")
        controller = _controller(project)
        monkeypatch.setattr(controller, "_attachments", lambda: _KeptElsewhere({self.REF: kept}))

        result = controller.read_file(self.REF)

        assert result.error == ""
        assert result.contents == "from the database"
        assert result.language == "" and result.size == len("from the database")

    def test_an_unknown_reference_is_not_found(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        controller = _controller(project)
        monkeypatch.setattr(controller, "_attachments", lambda: _KeptElsewhere({}))

        result = controller.read_file(self.REF)

        assert result.error == "File not found."
        assert result.path == self.REF
        assert (result.contents, result.size) == ("", 0)

    def test_a_reference_the_store_places_outside_the_roots_is_still_refused(
        self, project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        elsewhere = tmp_path / "elsewhere.txt"
        elsewhere.write_text("SECRET")
        controller = _controller(project)
        monkeypatch.setattr(
            controller, "_attachments", lambda: _KeptElsewhere({self.REF: elsewhere})
        )

        result = controller.read_file(self.REF)

        assert result.contents == ""
        assert result.error.startswith("Refused")


class TestTheLanguageHint:
    @pytest.mark.parametrize(
        ("suffix", "language"),
        [
            (".py", "python"),
            (".PY", "python"),
            (".Tsx", "typescript"),
            (".jsx", "javascript"),
            (".zsh", "bash"),
            (".yml", "yaml"),
            (".markdown", "markdown"),
            (".sql", "sql"),
            (".exe", ""),
            ("", ""),
        ],
    )
    def test_by_extension_ignoring_case(self, suffix: str, language: str) -> None:
        assert FilesController._guess_language(suffix) == language


class TestUploadingAnAttachment:
    async def test_the_file_is_saved_and_its_path_and_size_returned(self, project: Path) -> None:
        payload = b"\x89PNG-bytes" * 10

        result = await _controller(project).upload_attachment(
            "pic one.png", base64.b64encode(payload).decode()
        )

        assert result.error == ""
        assert result.size == len(payload)
        stored = Path(result.path)
        assert stored.name == "pic_one.png"
        assert stored.parent.name == "s-1"
        assert stored.read_bytes() == payload

    async def test_an_empty_upload_is_a_zero_byte_file(self, project: Path) -> None:
        result = await _controller(project).upload_attachment("empty.txt", "")

        assert result.error == ""
        assert result.size == 0
        assert Path(result.path).read_bytes() == b""

    async def test_bad_base64_is_an_error_result_and_leaves_no_file(self, project: Path) -> None:
        result = await _controller(project).upload_attachment("a.bin", "not base64!!!")

        assert result.path == ""
        assert result.size == 0
        assert result.error.startswith("invalid base64: ")
        assert not list(project.rglob("a.bin"))

    async def test_a_payload_with_non_ascii_characters_is_an_error_result(
        self, project: Path
    ) -> None:
        result = await _controller(project).upload_attachment("a.bin", "aGVsbG8=é")

        assert result.path == ""
        assert result.error.startswith("invalid base64: ")

    async def test_the_decode_and_write_run_off_the_event_loop(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop_thread = threading.get_ident()
        seen: list[int] = []
        original = DirectoryAttachments.save

        def spy(self, session_id, filename, chunks):  # type: ignore[no-untyped-def]
            seen.append(threading.get_ident())
            return original(self, session_id, filename, chunks)

        monkeypatch.setattr(DirectoryAttachments, "save", spy)

        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        big = base64.b64encode(b"x" * 3_000_000).decode()
        try:
            result = await _controller(project).upload_attachment("big.bin", big)
        finally:
            beat.cancel()

        assert result.size == 3_000_000
        assert seen and seen[0] != loop_thread
        assert ticks > 0
