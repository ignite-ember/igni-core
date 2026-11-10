"""The URL side of ``core/utils/media`` — the taxonomy and the extractor.

The path side (``resolve_text_references`` / ``attach_local_paths``) is
covered in ``test_images.py``. This file fills the URL side: vision-capable
models can take remote URLs as Image/Audio/Video/File parts, and the
taxonomy row that picks them out of the user text is what makes that work.

A URL with a missing trailing slash, a query string, or an uppercase
scheme is still a legitimate media URL; the regex needs to handle each.
"""

from __future__ import annotations

from agno.media import Audio, File, Image, Video

from igni_core.core.utils.media import MediaResolver
from igni_core.core.utils.media_schemas import MediaKind, MediaTaxonomy


class TestClassifyExtension:
    """The taxonomy maps an extension to its Agno container.

    Trivial mapping, but each row maps to a different Agno media class
    downstream — wrong row = wrong API call to the provider.
    """

    def test_image_extensions(self) -> None:
        # Sample the common image formats. The set is wider in
        # the source (heic, heif, avif, …) but pinning these
        # core ones catches the most likely regression.
        taxonomy = MediaTaxonomy.default()
        for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"]:
            spec = taxonomy.classify(ext)
            assert spec is not None
            assert spec.kind is MediaKind.image

    def test_audio_extensions(self) -> None:
        taxonomy = MediaTaxonomy.default()
        for ext in [".mp3", ".wav", ".ogg", ".flac", ".m4a"]:
            spec = taxonomy.classify(ext)
            assert spec is not None
            assert spec.kind is MediaKind.audio

    def test_video_extensions(self) -> None:
        taxonomy = MediaTaxonomy.default()
        for ext in [".mp4", ".mov", ".avi", ".webm", ".mkv"]:
            spec = taxonomy.classify(ext)
            assert spec is not None
            assert spec.kind is MediaKind.video

    def test_pdf_is_document(self) -> None:
        # PDF is the only document type today. Pinning here so
        # a future addition of .docx etc. doesn't silently
        # change the routing of existing PDFs.
        spec = MediaTaxonomy.default().classify(".pdf")
        assert spec is not None
        assert spec.kind is MediaKind.document

    def test_an_unknown_extension_has_no_row(self) -> None:
        # The dispatcher must not crash on an extension that's not
        # in the table; there is simply no row for it, and the
        # caller leaves the attachment out.
        taxonomy = MediaTaxonomy.default()
        assert taxonomy.classify(".xyz") is None
        assert taxonomy.classify("") is None

    def test_the_lookup_is_case_insensitive(self) -> None:
        # macOS Finder surfaces ``.PNG`` and ``.JPEG`` from
        # camera roll. The lookup lowercases before matching.
        taxonomy = MediaTaxonomy.default()
        assert taxonomy.classify(".PNG").kind is MediaKind.image
        assert taxonomy.classify(".MP3").kind is MediaKind.audio


class TestExtractUrlMedia:
    """The URL extractor pulls media URLs from a free-form
    user message. Each URL is wrapped in the appropriate Agno
    media class and bucketed by kind."""

    def test_no_urls_returns_none(self) -> None:
        # The function returns ``None`` when nothing was found
        # — the call site uses that as "don't pass media kwargs
        # at all" (vs ``{}`` which would also work but is
        # syntactically noisier).
        assert MediaResolver().extract_url_media("just plain text") is None

    def test_empty_string_returns_none(self) -> None:
        assert MediaResolver().extract_url_media("") is None

    def test_no_extension_url_ignored(self) -> None:
        # ``https://example.com/`` has no media extension; not
        # a media URL.
        assert MediaResolver().extract_url_media("see https://example.com/page") is None

    def test_image_url_extracted(self) -> None:
        out = MediaResolver().extract_url_media("look at https://example.com/photo.png")
        assert out is not None
        assert len(out.images) == 1
        # The Image object carries the URL — verify it lands
        # on the right field.
        assert isinstance(out.images[0], Image)
        assert out.images[0].url == "https://example.com/photo.png"

    def test_audio_url_extracted(self) -> None:
        out = MediaResolver().extract_url_media("clip: https://example.com/song.mp3")
        assert out is not None
        assert len(out.audio) == 1
        assert isinstance(out.audio[0], Audio)

    def test_video_url_extracted(self) -> None:
        out = MediaResolver().extract_url_media("watch https://example.com/clip.mp4")
        assert out is not None
        assert len(out.videos) == 1
        assert isinstance(out.videos[0], Video)

    def test_pdf_url_extracted(self) -> None:
        out = MediaResolver().extract_url_media("paper: https://example.com/draft.pdf")
        assert out is not None
        assert len(out.files) == 1
        assert isinstance(out.files[0], File)

    def test_http_and_https_both_match(self) -> None:
        # Both schemes are valid. Don't gate on https-only —
        # there are still legitimate http-hosted assets.
        out = MediaResolver().extract_url_media("http://example.com/a.png")
        assert out is not None
        assert len(out.images) == 1

    def test_url_with_query_string(self) -> None:
        # Many CDN URLs append ``?w=600&fmt=auto``. The regex
        # explicitly tolerates a query string after the
        # extension so these still match.
        out = MediaResolver().extract_url_media(
            "see https://cdn.example.com/photo.png?w=600&fmt=auto"
        )
        assert out is not None
        # The Image.url should include the query string — strip
        # only happens for the extension-classification step
        # (path_part = url.split("?")[0]).
        assert out.images[0].url.endswith("?w=600&fmt=auto")

    def test_multiple_urls_in_one_message(self) -> None:
        # Most production calls bundle several attachments.
        # The extractor walks the whole text and buckets by
        # kind.
        text = (
            "compare https://example.com/a.png and "
            "https://example.com/b.jpg with audio "
            "https://example.com/c.mp3"
        )
        out = MediaResolver().extract_url_media(text)
        assert out is not None
        assert len(out.images) == 2
        assert len(out.audio) == 1

    def test_uppercase_extension_in_url_matches(self) -> None:
        # The regex is ``re.IGNORECASE``. Some sites preserve
        # uploaded-file casing (``Photo.PNG``).
        out = MediaResolver().extract_url_media("https://example.com/Photo.PNG")
        assert out is not None
        assert len(out.images) == 1

    def test_only_the_buckets_that_hold_something_are_emitted(self) -> None:
        # If only images are in the text, the kwargs carry
        # ``images`` but NOT ``audio``/``videos``/``files``. The
        # caller forwards each key as a kwarg — empty lists would
        # still be forwarded and could surface as the provider
        # rejecting an ``images=[]`` call.
        out = MediaResolver().extract_url_media("photo https://example.com/x.png")
        assert out is not None
        assert set(out.to_kwargs().keys()) == {"images"}

    def test_unknown_extension_in_url_not_picked_up(self) -> None:
        # ``https://example.com/data.xyz`` is not media. The
        # regex itself is built from the known-extension set,
        # so an unknown extension doesn't even match.
        assert MediaResolver().extract_url_media("see https://example.com/data.xyz") is None

    def test_a_longer_extension_is_not_cut_short(self) -> None:
        # ``.tif`` sorts before ``.tiff``, so the shorter one used to
        # match first and the URL went out naming a file that is not
        # there.
        out = MediaResolver().extract_url_media("look at https://example.com/x.tiff")
        assert out is not None
        assert out.images[0].url == "https://example.com/x.tiff"

    def test_a_url_that_names_a_page_is_not_media(self) -> None:
        # ``photo.png.html`` is a page, not a picture — the URL used to
        # be cut at ``.png`` and the picture's name was sent.
        assert MediaResolver().extract_url_media("see https://example.com/photo.png.html") is None
