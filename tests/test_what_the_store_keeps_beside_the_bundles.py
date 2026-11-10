"""What the store keeps beside the bundles.

The store is a directory of bundles. Beside them sit the two things
igni has to remember about them: where a bundle came from, and what
igni server's last listing carried. Both are collaborators of the
store rather than the store itself, and each has a rule of its own
worth a look on its own:

* a name the store will not make, whatever asked for it;
* a distribution record, and what an unreadable one reads as;
* the listing — in the files the desktop keeps, and in the process the
  cloud keeps, where a second store does not see it.
"""

from __future__ import annotations

import pytest

from igni_core.core.layer import Layer, layers
from igni_core.extensions.listing import ListingState
from igni_core.extensions.provenance import DistributionRecord, Provenance
from igni_core.extensions.store import ExtensionStore
from igni_core.extensions.store_errors import StoreError, StoreName


class InProcessLayer(Layer):
    """A layer that keeps what the listing carried in this process only."""

    def keeps_listing_in_files(self) -> bool:
        return False


@pytest.fixture
def in_process_layer():
    """The cloud's choice: the listing is not written to files."""
    layers.install(InProcessLayer())
    try:
        yield
    finally:
        layers.reset()


def test_a_name_that_is_not_a_name_is_refused():
    with pytest.raises(StoreError, match="not a usable directory name"):
        StoreName("../elsewhere")


def test_the_store_refuses_a_bundle_path_it_cannot_make(tmp_path):
    store = ExtensionStore(tmp_path)
    with pytest.raises(StoreError, match="not a usable directory name"):
        store.path_for("../elsewhere")


def test_a_distribution_record_is_written_beside_the_bundle(tmp_path):
    provenance = Provenance(tmp_path)
    record = DistributionRecord(
        extension_id="acme.notes",
        version="1.0.0",
        sha256="a" * 64,
        service_url="https://notes.acme.test",
    )
    provenance.record(record)
    assert provenance.of("acme.notes") == record


def test_an_unreadable_record_reads_as_a_development_install(tmp_path):
    """The safe direction: a bundle with no readable record was
    installed for development, and that needs the dev flag to start."""
    provenance = Provenance(tmp_path)
    provenance.path("acme.notes").write_text("{ not json")
    assert provenance.of("acme.notes") is None


def test_the_listing_is_kept_in_files_on_the_desktop(tmp_path):
    listing = ListingState(tmp_path)
    assert listing.record_priority(["acme.a", "acme.b"]) is True
    assert listing.priority_path.is_file()
    assert listing.priority() == ["acme.a", "acme.b"]
    # The same order again is not a change.
    assert listing.record_priority(["acme.a", "acme.b"]) is False


def test_the_listing_is_kept_in_the_process_where_the_layer_says_so(tmp_path, in_process_layer):
    root = tmp_path / "extensions"
    listing = ListingState(root)
    listing.record_priority(["acme.a"])
    listing.record_group("engineering", name="Engineering")
    assert listing.priority() == ["acme.a"]
    group = listing.group()
    assert group is not None and group.group == "engineering"
    assert not root.exists(), "nothing written in the VM"


def test_two_stores_do_not_share_the_listing_they_keep_in_process(tmp_path, in_process_layer):
    """What one store was told is its own: the listing is not a
    process-wide thing smuggled through a class attribute."""
    ExtensionStore(tmp_path).listing.record_group("engineering")
    assert ExtensionStore(tmp_path).listing.group() is None
