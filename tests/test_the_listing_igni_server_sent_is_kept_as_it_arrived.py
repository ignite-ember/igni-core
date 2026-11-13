"""The priority order and the group a listing carried, kept beside the bundles.

On the desktop they are two small files read back at the next start;
what cannot be read back is ignored with a warning and reads as if no
listing had arrived. In the cloud they are kept in memory and no file
is written.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from igni_core.core.layer import Layer, layers
from igni_core.extensions.listing import GROUP_FILE, PRIORITY_FILE, ListingState, StoredGroup


class InProcessLayer(Layer):
    def keeps_listing_in_files(self) -> bool:
        return False


@pytest.fixture
def in_process_layer():
    layers.install(InProcessLayer())
    try:
        yield
    finally:
        layers.reset()


def test_nothing_received_reads_as_no_priority_and_no_group(tmp_path: Path) -> None:
    listing = ListingState(tmp_path)

    assert listing.priority() == []
    assert listing.group() is None


def test_a_priority_is_written_to_its_file_and_read_back_in_order(tmp_path: Path) -> None:
    ListingState(tmp_path).record_priority(["acme.b", "acme.a", "acme.é"])

    assert (tmp_path / PRIORITY_FILE).is_file()
    assert ListingState(tmp_path).priority() == ["acme.b", "acme.a", "acme.é"]


def test_recording_a_priority_says_whether_it_changed(tmp_path: Path) -> None:
    listing = ListingState(tmp_path)

    assert listing.record_priority([]) is False
    assert listing.record_priority(["acme.a"]) is True
    assert listing.record_priority(["acme.a"]) is False
    assert listing.record_priority(["acme.a", "acme.a"]) is True
    assert listing.priority() == ["acme.a", "acme.a"]
    assert listing.record_priority([]) is True
    assert listing.priority() == []


def test_the_directory_is_made_on_the_first_write(tmp_path: Path) -> None:
    root = tmp_path / "not" / "yet"

    ListingState(root).record_group("eng", name="Engineering")

    assert (root / GROUP_FILE).is_file()


def test_a_group_is_written_and_read_back_with_its_name(tmp_path: Path) -> None:
    ListingState(tmp_path).record_group("eng", name="Engineering")

    assert ListingState(tmp_path).group() == StoredGroup(group="eng", name="Engineering")


def test_no_group_drops_the_name_that_came_with_it(tmp_path: Path) -> None:
    listing = ListingState(tmp_path)

    listing.record_group(None, name="Engineering")

    assert listing.group() == StoredGroup(group=None, name=None)


@pytest.mark.parametrize(
    ("group", "name", "label"),
    [
        ("eng", "Engineering", "Engineering"),
        ("eng", None, "eng"),
        ("eng", "", "eng"),
        (None, None, None),
    ],
)
def test_a_group_is_called_by_its_name_else_its_id(group, name, label) -> None:
    assert StoredGroup(group=group, name=name).label == label


@pytest.mark.parametrize("content", ["{not json", '{"priority": "acme.a"}', '{"priority": [1]}'])
def test_a_priority_file_that_cannot_be_read_is_ignored_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, content: str
) -> None:
    (tmp_path / PRIORITY_FILE).write_text(content)

    with caplog.at_level(logging.WARNING):
        assert ListingState(tmp_path).priority() == []

    assert f"ignoring {tmp_path / PRIORITY_FILE}" in caplog.text


@pytest.mark.parametrize("content", ["{not json", '{"group": ["x"]}'])
def test_a_group_file_that_cannot_be_read_is_ignored_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, content: str
) -> None:
    (tmp_path / GROUP_FILE).write_text(content)

    with caplog.at_level(logging.WARNING):
        assert ListingState(tmp_path).group() is None

    assert f"ignoring {tmp_path / GROUP_FILE}" in caplog.text


def test_a_write_that_fails_is_not_swallowed(tmp_path: Path) -> None:
    (tmp_path / PRIORITY_FILE).mkdir()
    listing = ListingState(tmp_path)

    with pytest.raises(OSError):
        listing.record_priority(["acme.a"])


def test_in_the_cloud_the_listing_is_kept_in_memory_and_no_file_is_written(
    tmp_path: Path, in_process_layer: None
) -> None:
    root = tmp_path / "root"
    listing = ListingState(root)

    assert listing.record_priority(["acme.a"]) is True
    assert listing.record_priority(["acme.a"]) is False
    listing.record_group("eng", name="Engineering")
    assert listing.group() == StoredGroup(group="eng", name="Engineering")
    listing.record_group(None, name="dropped")

    assert listing.priority() == ["acme.a"]
    assert listing.group() == StoredGroup(group=None, name=None)
    assert not root.exists()


def test_two_listings_over_one_root_in_the_cloud_do_not_share_it(
    tmp_path: Path, in_process_layer: None
) -> None:
    ListingState(tmp_path).record_priority(["acme.a"])

    assert ListingState(tmp_path).priority() == []
