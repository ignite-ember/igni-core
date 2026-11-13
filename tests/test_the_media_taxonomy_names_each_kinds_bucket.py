"""The taxonomy's rows name the bucket each kind lands in, and the bundle holds only its own kinds."""

from __future__ import annotations

from pathlib import Path

import pytest
from agno.media import Audio, File, Image, Video
from pydantic import ValidationError

from igni_core.core.utils.media_schemas import (
    MediaAttachments,
    MediaKind,
    MediaTaxonomy,
    ResolvedText,
)


@pytest.mark.parametrize(
    ("kind", "bucket", "container"),
    [
        (MediaKind.image, "images", Image),
        (MediaKind.audio, "audio", Audio),
        (MediaKind.video, "videos", Video),
        (MediaKind.document, "files", File),
    ],
)
def test_each_default_row_names_its_bucket_and_container(kind, bucket, container) -> None:
    (spec,) = [s for s in MediaTaxonomy.default().specs if s.kind is kind]

    assert spec.bucket == bucket
    assert spec.container_cls is container


def test_every_known_extension_maps_to_exactly_its_kinds_row() -> None:
    taxonomy = MediaTaxonomy.default()

    assert taxonomy.all_extensions == {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".svg", ".avif",
        ".heic", ".heif",
        ".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma",
        ".mp4", ".mov", ".avi", ".webm", ".mkv", ".wmv",
        ".pdf",
    }  # fmt: skip
    for ext in taxonomy.all_extensions:
        spec = taxonomy.by_extension[ext]
        assert ext in spec.extensions


def test_a_mixed_case_extension_is_classified_and_a_dotless_one_is_not() -> None:
    taxonomy = MediaTaxonomy.default()

    spec = taxonomy.classify(".PnG")
    assert spec is not None
    assert spec.kind is MediaKind.image
    assert taxonomy.classify("png") is None
    assert taxonomy.classify("") is None


def test_a_row_refuses_both_a_url_and_a_path() -> None:
    spec = MediaTaxonomy.default().by_extension[".png"]

    with pytest.raises(AssertionError):
        spec.build(url="https://x.test/a.png", filepath=Path("/a.png"))


def test_a_bucket_refuses_an_object_of_another_kind() -> None:
    with pytest.raises(ValidationError):
        MediaAttachments(images=[Audio(filepath=Path("/a.mp3"))])
    with pytest.raises(ValidationError):
        MediaAttachments(files=["/a.pdf"])


def test_a_resolved_text_defaults_to_no_paths_and_its_lists_are_its_own() -> None:
    first = ResolvedText(text="a")
    second = ResolvedText(text="b")
    first.paths.append("/x.png")

    assert (second.paths, second.attachable, second.missed) == ([], [], [])
    assert first.attachable == []
