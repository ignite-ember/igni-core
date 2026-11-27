"""A text-only model is told what a media file is, never asked to read what it shows.

A PNG attached while the model was MiniMax-M2.7 (no vision) came to the
model as "[Referenced files: … — read before responding]". It looped for
minutes: ``cat`` on the image, Swift OCR scripts, ASCII renderings. Now an
image, recording or video named to a text-only model comes with its
metadata — name, path, type, size, and an image's pixel size — and the
plain statement that vision is disabled. A vision model is unchanged, and
so is every other file.

The pixel sizes are read from real files written by real encoders (``sips``
and ``cwebp``), each 7x3 pixels, in ``tests/fixtures/media``.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from igni_core.core.utils.attached_media import AttachedMediaNotice
from igni_core.core.utils.image_dimensions import ImageDimensions
from igni_core.core.utils.media_metadata import MediaMetadataReader
from igni_core.core.utils.media_schemas import (
    MediaFileMetadata,
    MediaKind,
    MediaKindSpec,
    MediaTaxonomy,
    PixelSize,
)
from igni_core.core.utils.mentions import process_file_mentions
from igni_core.core.utils.text_only_media import TextOnlyMediaNotice
from igni_core.core.utils.turn_media import TurnMediaNotices

FIXTURES = Path(__file__).parent / "fixtures" / "media"
PNG = FIXTURES / "seven-by-three.png"
SEVEN_BY_THREE = PixelSize(width=7, height=3)

HEADER = (
    "[Vision is disabled for this model. Do not open, read, decode, OCR or otherwise "
    "extract the content of these media files; only their metadata is available. "
    "Tell the user you cannot see them and suggest switching to a vision-enabled model.]"
)


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


# ── the pixel size, from the header ───────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "seven-by-three.png",
        "seven-by-three.jpg",
        "seven-by-three.gif",
        "seven-by-three.webp",
        "seven-by-three-lossless.webp",
        "seven-by-three-extended.webp",
    ],
)
def test_each_header_format_gives_the_real_pixel_size(name: str):
    assert ImageDimensions.read(FIXTURES / name) == SEVEN_BY_THREE


def test_the_fixture_png_is_the_size_its_name_says():
    head = PNG.read_bytes()
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", head[16:24]) == (7, 3)


def test_a_gif89a_is_read_as_a_gif87a_is(tmp_path: Path):
    gif = _write(tmp_path / "a.gif", b"GIF89a" + struct.pack("<HH", 640, 480) + b"\x00" * 22)

    assert ImageDimensions.read(gif) == PixelSize(width=640, height=480)


def test_a_lossy_webps_scale_bits_are_not_part_of_its_size(tmp_path: Path):
    head = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 7 + b"\x9d\x01\x2a"
    webp = _write(tmp_path / "a.webp", head + struct.pack("<HH", 0x4007, 0xC003) + b"\x00" * 4)

    assert ImageDimensions.read(webp) == PixelSize(width=7, height=3)


def test_a_jpeg_is_walked_past_its_fill_bytes_and_restart_markers(tmp_path: Path):
    app0 = b"\xff\xe0" + struct.pack(">H", 4) + b"\x00\x00"
    sof = b"\xff\xc2" + struct.pack(">HBHH", 11, 8, 30, 40) + b"\x00" * 6
    jpeg = _write(tmp_path / "a.jpg", b"\xff\xd8" + app0 + b"\xff\xff\xd0" + sof + b"\xff\xd9")

    assert ImageDimensions.read(jpeg) == PixelSize(width=40, height=30)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"\xff\xd8\xff\xd9", id="jpeg-ends-before-a-frame"),
        pytest.param(b"\xff\xd8\xff\xda\x00\x02", id="jpeg-scan-before-a-frame"),
        pytest.param(b"\xff\xd8\xff\xe0\x00\x01", id="jpeg-length-below-two"),
        pytest.param(b"\xff\xd8\x00\x00", id="jpeg-no-marker"),
        pytest.param(b"\xff\xd8\xff", id="jpeg-cut-at-the-marker"),
        pytest.param(b"\xff\xd8\xff\xc0\x00", id="jpeg-cut-in-the-frame"),
        pytest.param(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR", id="png-cut-in-the-header"),
        pytest.param(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIDAT" + struct.pack(">II", 7, 3), id="png-no-ihdr"
        ),
        pytest.param(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + b"\x00" * 8 + b"\x00" * 8, id="png-zero"
        ),
        pytest.param(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + struct.pack(">II", 5, 0), id="png-no-height"
        ),
        pytest.param(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + struct.pack(">II", 0, 5), id="png-no-width"
        ),
        pytest.param(b"RIFF\x00\x00\x00\x00WEBPXXXX" + b"\x00" * 16, id="webp-unknown-chunk"),
        pytest.param(
            b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 10 + struct.pack("<HH", 7, 3),
            id="webp-lossy-no-start",
        ),
        pytest.param(b"RIFF\x00\x00\x00\x00WEBPVP8L" + b"\x00" * 18, id="webp-lossless-no-sig"),
        pytest.param(
            b"RIFF\x00\x00\x00\x00WAVEVP8X" + b"\x00" * 8 + b"\x06\x00\x00\x02\x00\x00",
            id="riff-not-webp",
        ),
        pytest.param(b"BM" + b"\x00" * 40, id="bmp-is-not-read"),
        pytest.param(b"", id="empty"),
    ],
)
def test_a_header_that_says_no_size_gives_none(tmp_path: Path, data: bytes):
    assert ImageDimensions.read(_write(tmp_path / "x.img", data)) is None


def _sof(code: int, height: int, width: int) -> bytes:
    return bytes([0xFF, code]) + struct.pack(">HBHH", 13, 8, height, width) + b"\x00" * 6


@pytest.mark.parametrize("code", [0xC4, 0xC8, 0xCC])
def test_the_markers_in_the_frame_range_that_are_not_frames_are_skipped(tmp_path: Path, code: int):
    jpeg = _write(tmp_path / "a.jpg", b"\xff\xd8" + _sof(code, 1, 1) + _sof(0xC0, 30, 40))

    assert ImageDimensions.read(jpeg) == PixelSize(width=40, height=30)


@pytest.mark.parametrize("code", [0xC0, 0xC1, 0xC3, 0xC5, 0xCB, 0xCF])
def test_every_start_of_frame_marker_carries_the_size(tmp_path: Path, code: int):
    jpeg = _write(tmp_path / "a.jpg", b"\xff\xd8" + _sof(code, 30, 40))

    assert ImageDimensions.read(jpeg) == PixelSize(width=40, height=30)


def test_a_tem_marker_has_no_length_and_is_stepped_over(tmp_path: Path):
    jpeg = _write(tmp_path / "a.jpg", b"\xff\xd8\xff\x01" + _sof(0xC0, 30, 40))

    assert ImageDimensions.read(jpeg) == PixelSize(width=40, height=30)


def test_frame_bytes_without_the_jpeg_start_marker_are_not_a_jpeg(tmp_path: Path):
    data = _write(tmp_path / "a.bin", b"\x00\x00" + _sof(0xC0, 30, 40))

    assert ImageDimensions.read(data) is None


def test_any_number_of_fill_bytes_may_precede_a_marker(tmp_path: Path):
    jpeg = _write(tmp_path / "a.jpg", b"\xff\xd8\xff\xff\xff" + _sof(0xC0, 30, 40))

    assert ImageDimensions.read(jpeg) == PixelSize(width=40, height=30)


@pytest.mark.parametrize("stop", [b"\xff\xda\x00\x02", b"\xff\xd9\x00\x02"], ids=["scan", "end"])
def test_nothing_after_the_scan_or_the_end_is_read_as_a_frame(tmp_path: Path, stop: bytes):
    jpeg = _write(tmp_path / "a.jpg", b"\xff\xd8" + stop + _sof(0xC0, 30, 40))

    assert ImageDimensions.read(jpeg) is None


def test_a_jpeg_that_loses_its_marker_alignment_is_given_up_on(tmp_path: Path):
    jpeg = _write(tmp_path / "a.jpg", b"\xff\xd8\x00" + _sof(0xC0, 30, 40))

    assert ImageDimensions.read(jpeg) is None


def test_a_jpeg_of_endless_segments_is_given_up_on(tmp_path: Path):
    segment = b"\xff\xe1" + struct.pack(">H", 2)
    many = _write(tmp_path / "many.jpg", b"\xff\xd8" + segment * 600 + _sof(0xC0, 30, 40))
    few = _write(tmp_path / "few.jpg", b"\xff\xd8" + segment * 500 + _sof(0xC0, 30, 40))

    assert ImageDimensions.read(many) is None
    assert ImageDimensions.read(few) == PixelSize(width=40, height=30)


def test_a_missing_file_gives_no_size(tmp_path: Path):
    assert ImageDimensions.read(tmp_path / "gone.png") is None


# ── what is a media file ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("ext", "expected"),
    [(".png", True), (".JPG", True), (".mp3", True), (".mp4", True), (".pdf", False)]
    + [(".md", False), ("", False)],
)
def test_images_audio_and_video_need_vision_documents_do_not(ext: str, expected: bool):
    assert MediaTaxonomy.default().needs_vision(ext) is expected


# ── the metadata ──────────────────────────────────────────────────


def test_an_image_is_described_by_name_path_type_size_and_pixels(tmp_path: Path):
    shot = _write(tmp_path / "shot.png", PNG.read_bytes())

    metadata = MediaMetadataReader(tmp_path).read("shot.png")

    assert metadata == MediaFileMetadata(
        name="shot.png",
        path=str(shot.resolve()),
        kind=MediaKind.image,
        media_type="image/png",
        size_bytes=74,
        pixels=SEVEN_BY_THREE,
    )
    assert metadata.describe() == (
        f"- shot.png (path: {shot.resolve()}, type: image/png, size: 74 bytes, dimensions: 7x3 px)"
    )


def test_a_video_and_a_recording_have_a_size_and_no_pixels(tmp_path: Path):
    _write(tmp_path / "clip.mp4", b"\x00" * 12)
    _write(tmp_path / "talk.mp3", b"\x00" * 5)
    reader = MediaMetadataReader(tmp_path)

    clip, talk = reader.read("clip.mp4"), reader.read("talk.mp3")

    assert clip is not None and talk is not None
    assert (clip.kind, clip.media_type, clip.size_bytes, clip.pixels) == (
        MediaKind.video,
        "video/mp4",
        12,
        None,
    )
    assert (talk.kind, talk.media_type, talk.size_bytes) == (MediaKind.audio, "audio/mpeg", 5)
    assert clip.describe() == f"- clip.mp4 (path: {tmp_path.resolve() / 'clip.mp4'}, " + (
        "type: video/mp4, size: 12 bytes)"
    )


def test_a_file_that_is_not_there_is_still_named_and_typed(tmp_path: Path):
    metadata = MediaMetadataReader(tmp_path).read("gone.webp")

    assert metadata is not None
    assert (metadata.size_bytes, metadata.pixels, metadata.media_type) == (None, None, "image/webp")
    assert metadata.describe() == f"- gone.webp (path: {tmp_path.resolve() / 'gone.webp'}, " + (
        "type: image/webp)"
    )


def test_a_directory_named_like_an_image_has_no_size(tmp_path: Path):
    (tmp_path / "album.png").mkdir()

    metadata = MediaMetadataReader(tmp_path).read("album.png")

    assert metadata is not None and metadata.size_bytes is None and metadata.pixels is None


def test_an_image_whose_header_is_not_readable_has_a_size_and_no_pixels(tmp_path: Path):
    _write(tmp_path / "odd.heic", b"\x00" * 9)

    metadata = MediaMetadataReader(tmp_path).read("odd.heic")

    assert metadata is not None
    assert (metadata.media_type, metadata.size_bytes, metadata.pixels) == ("image/heic", 9, None)


def test_an_absolute_path_ignores_the_base_and_a_home_path_is_expanded(tmp_path: Path, monkeypatch):
    shot = _write(tmp_path / "shot.png", PNG.read_bytes())
    monkeypatch.setenv("HOME", str(tmp_path))
    reader = MediaMetadataReader(tmp_path / "elsewhere")

    absolute, home = reader.read(str(shot)), reader.read("~/shot.png")

    assert absolute is not None and home is not None
    assert absolute.path == home.path == str(shot.resolve())


def test_a_relative_path_without_a_base_is_taken_from_the_working_directory(
    tmp_path: Path, monkeypatch
):
    _write(tmp_path / "shot.png", PNG.read_bytes())
    monkeypatch.chdir(tmp_path)

    metadata = MediaMetadataReader().read("shot.png")

    assert metadata is not None and metadata.size_bytes == 74


def test_an_upper_case_extension_is_typed_as_the_lower_case_one(tmp_path: Path):
    _write(tmp_path / "SHOT.PNG", PNG.read_bytes())

    metadata = MediaMetadataReader(tmp_path).read("SHOT.PNG")

    assert metadata is not None and metadata.media_type == "image/png"
    assert metadata.pixels == SEVEN_BY_THREE


@pytest.mark.parametrize("name", ["notes.md", "report.pdf", "Makefile", "data.csv"])
def test_a_file_that_is_not_image_audio_or_video_has_no_media_metadata(tmp_path: Path, name: str):
    assert MediaMetadataReader(tmp_path).read(name) is None


def test_a_kind_with_no_written_type_is_typed_by_its_kind(tmp_path: Path):
    taxonomy = MediaTaxonomy(
        specs=(
            *MediaTaxonomy.default().specs,
            MediaKindSpec(
                kind=MediaKind.video,
                extensions=frozenset({".flv"}),
                container_cls=MediaTaxonomy.default().by_extension[".mp4"].container_cls,
                bucket="videos",
            ),
        )
    )

    metadata = MediaMetadataReader(tmp_path, taxonomy).read("old.flv")

    assert metadata is not None and metadata.media_type == "video"


def test_every_media_extension_has_a_written_type():
    taxonomy = MediaTaxonomy.default()
    media = {ext for ext in taxonomy.all_extensions if taxonomy.needs_vision(ext)}

    assert media == set(MediaMetadataReader.MEDIA_TYPES)


def test_each_file_is_described_once_in_the_order_first_named(tmp_path: Path):
    shot = _write(tmp_path / "shot.png", PNG.read_bytes())
    _write(tmp_path / "clip.mp4", b"\x00")

    files = MediaMetadataReader(tmp_path).read_all(
        ["clip.mp4", "notes.md", "shot.png", str(shot), "clip.mp4"]
    )

    assert [f.name for f in files] == ["clip.mp4", "shot.png"]


# ── the mention line ──────────────────────────────────────────────


def test_only_the_files_that_are_not_media_are_listed_to_read():
    text, paths = process_file_mentions("compare @notes.md @shot.png @clip.mp4")

    assert paths == ["notes.md", "shot.png", "clip.mp4"], "every mention is still announced"
    assert text == (
        "<attached-files>\n"
        "[Referenced files: notes.md — read before responding]\n"
        "</attached-files>\ncompare @notes.md @shot.png @clip.mp4"
    )


def test_a_message_of_media_alone_is_not_told_to_read_anything():
    text, paths = process_file_mentions("what is @shot.png")

    assert paths == ["shot.png"]
    assert text == "what is @shot.png"


def test_files_that_are_not_media_are_listed_as_before():
    text, _ = process_file_mentions("read @notes.md and @report.pdf")

    assert text == (
        "<attached-files>\n"
        "[Referenced files: notes.md, report.pdf — read before responding]\n"
        "</attached-files>\nread @notes.md and @report.pdf"
    )


# ── the notice ────────────────────────────────────────────────────


def _notice(tmp_path: Path) -> tuple[TextOnlyMediaNotice, str, str]:
    _write(tmp_path / "shot.png", PNG.read_bytes())
    _write(tmp_path / "clip.mp4", b"\x00" * 3)
    notice = TextOnlyMediaNotice.collect(["shot.png", "notes.md", "clip.mp4"], tmp_path)
    shot = (
        f"- shot.png (path: {tmp_path.resolve() / 'shot.png'}, type: image/png, "
        "size: 74 bytes, dimensions: 7x3 px)"
    )
    clip = f"- clip.mp4 (path: {tmp_path.resolve() / 'clip.mp4'}, type: video/mp4, size: 3 bytes)"
    return notice, shot, clip


def test_the_instruction_is_worded_exactly_as_approved():
    assert TextOnlyMediaNotice.HEADER == HEADER


def test_the_notice_closes_the_existing_attached_files_block(tmp_path: Path):
    notice, shot, clip = _notice(tmp_path)
    message = (
        "<attached-files>\n[Referenced files: notes.md — read before responding]\n"
        "</attached-files>\ncompare"
    )

    assert notice.wrap(message) == (
        "<attached-files>\n[Referenced files: notes.md — read before responding]\n"
        f"{HEADER}\n{shot}\n{clip}\n</attached-files>\ncompare"
    )


def test_the_notice_opens_the_block_when_there_is_none(tmp_path: Path):
    notice, shot, clip = _notice(tmp_path)

    assert notice.wrap("what is this") == (
        f"<attached-files>\n{HEADER}\n{shot}\n{clip}\n</attached-files>\nwhat is this"
    )
    assert notice.wrap("") == f"<attached-files>\n{HEADER}\n{shot}\n{clip}\n</attached-files>"


def test_a_block_that_is_never_closed_is_not_written_into(tmp_path: Path):
    notice, shot, clip = _notice(tmp_path)

    wrapped = notice.wrap("<attached-files>\nunclosed")

    assert wrapped == (
        f"<attached-files>\n{HEADER}\n{shot}\n{clip}\n</attached-files>\n<attached-files>\nunclosed"
    )


def test_a_closing_tag_later_in_the_text_is_not_a_block(tmp_path: Path):
    notice, shot, clip = _notice(tmp_path)

    wrapped = notice.wrap("quote\n</attached-files> here")

    assert wrapped.startswith(f"<attached-files>\n{HEADER}\n")
    assert wrapped.endswith("</attached-files>\nquote\n</attached-files> here")


def test_only_the_first_block_is_closed_with_the_notice(tmp_path: Path):
    notice, shot, clip = _notice(tmp_path)
    message = "<attached-files>\nA\n</attached-files>\nbody\n</attached-files>"

    assert notice.wrap(message) == (
        f"<attached-files>\nA\n{HEADER}\n{shot}\n{clip}\n</attached-files>\nbody\n</attached-files>"
    )


def test_a_notice_without_media_changes_nothing(tmp_path: Path):
    notice = TextOnlyMediaNotice.collect(["notes.md"], tmp_path)

    assert notice.files == []
    assert notice.wrap("text") == "text"


def test_the_person_is_told_which_files_the_model_cannot_see(tmp_path: Path):
    notice, _, _ = _notice(tmp_path)

    assert notice.user_notice("MiniMax-M2.7") == (
        "Vision is disabled for MiniMax-M2.7: only the metadata of shot.png, clip.mp4 was sent. "
        "Switch to a vision-enabled model to let it see the content."
    )


def test_the_block_is_the_instruction_then_one_line_per_file(tmp_path: Path):
    notice, shot, clip = _notice(tmp_path)

    assert notice.block() == f"{HEADER}\n{shot}\n{clip}"


def test_an_empty_file_is_described_as_zero_bytes(tmp_path: Path):
    _write(tmp_path / "empty.mp4", b"")

    metadata = MediaMetadataReader(tmp_path).read("empty.mp4")

    assert metadata is not None
    assert metadata.describe().endswith("type: video/mp4, size: 0 bytes)")


def test_the_path_is_given_resolved(tmp_path: Path):
    shot = _write(tmp_path / "shot.png", PNG.read_bytes())
    (tmp_path / "sub").mkdir()
    (tmp_path / "link.png").symlink_to(shot)
    reader = MediaMetadataReader(tmp_path)

    dotted, linked = reader.read("sub/../shot.png"), reader.read("link.png")

    assert dotted is not None and dotted.path == str(shot.resolve())
    assert linked is not None and linked.path == str(shot.resolve())


def test_only_an_image_is_given_pixels_whatever_its_bytes(tmp_path: Path):
    _write(tmp_path / "odd.mp4", PNG.read_bytes())

    metadata = MediaMetadataReader(tmp_path).read("odd.mp4")

    assert metadata is not None and metadata.pixels is None


# ── a vision turn: attached, or described when not ────────────────


ATTACHED = (
    "[These media files are attached to this message and already visible to you. "
    "Do not open, read, decode, OCR or otherwise extract their content with tools.]"
)


def test_a_vision_turn_lists_the_attached_png_and_describes_the_mp4_it_was_not_sent(
    tmp_path: Path,
):
    _write(tmp_path / "shot.png", PNG.read_bytes())
    _write(tmp_path / "clip.mp4", b"\x00" * 6)
    shot, clip = str(tmp_path / "shot.png"), str(tmp_path / "clip.mp4")

    notices = TurnMediaNotices.collect(["shot.png", "notes.md", "clip.mp4"], [shot], tmp_path)

    assert AttachedMediaNotice.HEADER == ATTACHED
    assert notices.wrap(
        "<attached-files>\n[Referenced files: notes.md — read before responding]\n</attached-files>\nhi"
    ) == (
        "<attached-files>\n[Referenced files: notes.md — read before responding]\n"
        f"{ATTACHED}\n"
        f"- shot.png (path: {shot}, type: image/png, size: 74 bytes, dimensions: 7x3 px)\n"
        f"{HEADER}\n"
        f"- clip.mp4 (path: {clip}, type: video/mp4, size: 6 bytes)\n"
        "</attached-files>\nhi"
    )
    assert [f.path for f in notices.unseen.files] == [clip]


def test_a_turn_with_no_media_leaves_the_message_alone(tmp_path: Path):
    assert TurnMediaNotices.collect(["notes.md"], [], tmp_path).wrap("hi") == "hi"
