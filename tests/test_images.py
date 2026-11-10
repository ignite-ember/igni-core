"""The media resolver turns free text into resolved paths and typed attachments.

Two questions, asked separately: where does the file the message names
live (``resolve_text_references``), and what may be put on the wire for
a vision model (``attach_local_paths``).
"""

from __future__ import annotations

from pathlib import Path

from agno.media import File, Image

from igni_core.core.utils.media import MediaResolver


class TestResolveTextReferences:
    """Bare filenames and explicit paths become absolute paths."""

    def test_a_bare_filename_in_the_project_is_resolved(self, tmp_path: Path) -> None:
        (tmp_path / "photo.png").write_bytes(b"fake png")
        resolution = MediaResolver(project_dir=tmp_path).resolve_text_references(
            "Look at photo.png"
        )
        assert resolution.paths == [str(tmp_path / "photo.png")]
        assert str(tmp_path / "photo.png") in resolution.text

    def test_a_bare_filename_nowhere_is_left_alone_and_reported(self) -> None:
        resolution = MediaResolver().resolve_text_references("Look at nonexistent_file.png")
        assert resolution.paths == []
        assert resolution.missed == ["nonexistent_file.png"]
        assert "nonexistent_file.png" in resolution.text

    def test_an_explicit_path_is_left_as_written_and_is_attachable(self, tmp_path: Path) -> None:
        """A path spelled out in full needs no rewriting — but it is
        attachable, because it lies in the project."""
        image = tmp_path / "test.jpg"
        image.write_bytes(b"fake jpg")
        resolution = MediaResolver(project_dir=tmp_path).resolve_text_references(f"Check {image}")
        assert resolution.paths == []
        assert resolution.attachable == [str(image)]
        assert str(image) in resolution.text

    def test_text_without_a_media_extension_is_unchanged(self) -> None:
        resolution = MediaResolver().resolve_text_references("Just a regular message")
        assert resolution.text == "Just a regular message"
        assert resolution.paths == []

    def test_a_non_media_extension_is_not_touched(self) -> None:
        resolution = MediaResolver().resolve_text_references("Check script.py")
        assert resolution.paths == []
        assert resolution.text == "Check script.py"

    def test_several_bare_filenames_are_all_resolved(self, tmp_path: Path) -> None:
        (tmp_path / "a.png").write_bytes(b"img")
        (tmp_path / "b.pdf").write_bytes(b"pdf")
        resolution = MediaResolver(project_dir=tmp_path).resolve_text_references(
            "Compare a.png and b.pdf"
        )
        assert len(resolution.paths) == 2

    def test_a_resolved_path_is_attachable(self, tmp_path: Path) -> None:
        (tmp_path / "photo.png").write_bytes(b"fake png")
        resolution = MediaResolver(project_dir=tmp_path).resolve_text_references(
            "Look at photo.png"
        )
        assert resolution.attachable == [str(tmp_path / "photo.png")]

    def test_an_explicit_path_that_is_not_there_is_skipped(self, tmp_path: Path) -> None:
        resolution = MediaResolver(project_dir=tmp_path).resolve_text_references(
            f"Check {tmp_path}/missing.png"
        )
        assert resolution.paths == []
        assert resolution.attachable == []

    def test_a_path_written_with_a_dot_inside_it_is_normalised(self, tmp_path: Path) -> None:
        image = tmp_path / "photo.png"
        image.write_bytes(b"fake png")
        resolution = MediaResolver(project_dir=tmp_path).resolve_text_references(
            f"Check {tmp_path}/./photo.png"
        )
        assert resolution.paths == [str(image)]
        assert str(image) in resolution.text

    def test_a_path_outside_the_project_stays_text(self, tmp_path: Path) -> None:
        """A path written out in full is attachable only inside an
        allowed root — elsewhere the model may read it with its tools,
        but its bytes are not put on the wire."""
        elsewhere = tmp_path.parent / "elsewhere.png"
        elsewhere.write_bytes(b"x")
        resolution = MediaResolver(project_dir=tmp_path).resolve_text_references(
            f"Check {elsewhere}"
        )
        assert resolution.paths == []
        assert resolution.attachable == []


class TestAttachLocalPaths:
    """Vision models get Agno media objects from resolved paths."""

    def test_an_image_lands_in_the_images_bucket(self, tmp_path: Path) -> None:
        image = tmp_path / "photo.jpg"
        image.write_bytes(b"\xff\xd8")
        attachments = MediaResolver().attach_local_paths([str(image)])
        assert attachments is not None
        assert len(attachments.images) == 1
        assert isinstance(attachments.images[0], Image)
        assert attachments.images[0].filepath == image

    def test_a_pdf_lands_in_the_files_bucket(self, tmp_path: Path) -> None:
        document = tmp_path / "doc.pdf"
        document.write_bytes(b"%PDF")
        attachments = MediaResolver().attach_local_paths([str(document)])
        assert attachments is not None
        assert len(attachments.files) == 1
        assert isinstance(attachments.files[0], File)

    def test_a_non_media_path_attaches_nothing(self) -> None:
        assert MediaResolver().attach_local_paths(["/some/path/script.py"]) is None

    def test_media_of_two_kinds_land_in_their_own_buckets(self, tmp_path: Path) -> None:
        image = tmp_path / "photo.png"
        document = tmp_path / "doc.pdf"
        image.write_bytes(b"img")
        document.write_bytes(b"pdf")
        attachments = MediaResolver().attach_local_paths([str(image), str(document)])
        assert attachments is not None
        assert len(attachments.images) == 1
        assert len(attachments.files) == 1
        assert attachments.total() == 2

    def test_no_paths_attach_nothing(self) -> None:
        assert MediaResolver().attach_local_paths([]) is None
