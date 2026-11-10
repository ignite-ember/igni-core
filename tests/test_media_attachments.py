"""The attachment bundle is typed: one list per kind, and the Agno
containers themselves live in it.

The resolver's job (finding paths, building the bundle) is covered in
``test_images.py``; the URL side in ``test_media_urls.py``. This file is
about the bundle itself — what goes in, what comes out, and what
``team.arun`` is handed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agno.media import Audio, File, Image, Video

from igni_core.core.utils.media_schemas import (
    MediaAttachments,
    MediaKind,
    MediaKindSpec,
    MediaTaxonomy,
)


def _image(name: str = "a.png") -> Image:
    return Image(filepath=Path(f"/project/{name}"))


def _audio() -> Audio:
    return Audio(filepath=Path("/project/a.mp3"))


class TestTheBundle:
    def test_a_new_bundle_is_empty(self) -> None:
        bundle = MediaAttachments()
        assert bundle.is_empty()
        assert bundle.total() == 0
        assert bundle.to_kwargs() == {}

    def test_an_item_lands_in_the_bucket_it_is_named(self) -> None:
        bundle = MediaAttachments()
        bundle.add("images", _image())
        assert len(bundle.images) == 1
        assert bundle.audio == []
        assert bundle.total() == 1

    def test_the_kwargs_carry_the_live_containers(self) -> None:
        """Agno takes the container itself, not a dump of it — a dict
        where an ``Image`` belongs is rejected by the provider."""
        image = _image()
        bundle = MediaAttachments()
        bundle.add("images", image)

        assert bundle.to_kwargs()["images"][0] is image

    def test_the_kwargs_keep_the_order_the_attachments_arrived_in(self) -> None:
        """Two images of the same kind keep their order — the provider
        sees them in the order the message named them."""
        bundle = MediaAttachments()
        bundle.add("images", _image("one.png"))
        bundle.add("images", _image("two.png"))

        assert [i.filepath.name for i in bundle.to_kwargs()["images"]] == ["one.png", "two.png"]

    def test_only_the_buckets_that_hold_something_are_emitted(self) -> None:
        bundle = MediaAttachments()
        bundle.add("audio", _audio())

        assert set(bundle.to_kwargs()) == {"audio"}

    def test_merging_extends_every_bucket(self) -> None:
        first = MediaAttachments()
        first.add("images", _image("one.png"))
        second = MediaAttachments()
        second.add("images", _image("two.png"))
        second.add("audio", _audio())
        second.add("videos", Video(filepath=Path("/project/a.mp4")))

        first.merge(second)

        assert len(first.images) == 2
        assert len(first.audio) == 1
        assert len(first.videos) == 1
        assert first.total() == 4

    def test_merging_copies_rather_than_shares(self) -> None:
        first = MediaAttachments()
        second = MediaAttachments()
        second.add("files", File(filepath=Path("/project/a.pdf")))

        first.merge(second)

        assert len(first.files) == 1
        assert len(second.files) == 1


class TestTheTaxonomy:
    def test_a_row_builds_a_container_from_a_path(self) -> None:
        spec = MediaTaxonomy.default().classify(".png")
        assert spec is not None
        built = spec.build(filepath=Path("/project/a.png"))
        assert isinstance(built, Image)
        assert built.filepath == Path("/project/a.png")

    def test_a_row_builds_a_container_from_a_url(self) -> None:
        spec = MediaTaxonomy.default().classify(".mp3")
        assert spec is not None
        built = spec.build(url="https://example.com/a.mp3")
        assert isinstance(built, Audio)
        assert built.url == "https://example.com/a.mp3"

    def test_a_row_needs_exactly_one_source(self) -> None:
        spec = MediaKindSpec(
            kind=MediaKind.image,
            extensions=frozenset({".png"}),
            container_cls=Image,
            bucket="images",
        )
        with pytest.raises(AssertionError):
            spec.build()
