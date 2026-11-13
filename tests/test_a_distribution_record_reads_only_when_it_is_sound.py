from __future__ import annotations

import json

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from igni_core.extensions.provenance import DISTRIBUTION_SUFFIX, DistributionRecord, Provenance
from igni_core.extensions.store_errors import StoreError


if TYPE_CHECKING:
    from igni_core.extensions.provenance import DistributionRecord


def record_of(extension_id: str) -> DistributionRecord:
    return DistributionRecord(extension_id, "1.0.0", "sha256:abc123")


def test_recording_makes_the_missing_directory_and_writes_indented_json(tmp_path: Path) -> None:
    root = tmp_path / "distributions"
    provenance = Provenance(root)

    assert not root.exists()

    provenance.record(record_of("acme.notes"))

    written = (root / f"acme.notes{DISTRIBUTION_SUFFIX}").read_text(encoding="utf-8")
    assert written.startswith('{\n  "extension_id": "acme.notes"')
    assert written.endswith("}\n")


def test_the_records_are_listed_in_file_name_order_without_the_broken_ones(tmp_path: Path) -> None:
    root = tmp_path / "distributions"
    provenance = Provenance(root)

    # Create distribution records with various file names and one broken one.
    provenance.record(record_of("acme.mid"))
    provenance.record(record_of("acme.alpha"))
    provenance.record(record_of("acme.zulu"))

    # Write a broken file that is not valid UTF-8.
    broken = root / f"acme.broken{DISTRIBUTION_SUFFIX}"
    broken.write_bytes(b"\x80\x81\x82\x83")

    listed = provenance.records()
    ids = [r.extension_id for r in listed]
    assert ids == ["acme.alpha", "acme.mid", "acme.zulu"]


def test_a_record_that_is_not_text_reads_as_none_and_says_so(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    root = tmp_path / "distributions"
    provenance = Provenance(root)

    # Write a broken file that is not valid UTF-8.
    broken = root / f"acme.broken{DISTRIBUTION_SUFFIX}"
    root.mkdir()
    broken.write_bytes(b"\x80\x81\x82\x83")

    found = provenance.of("acme.broken")
    assert found is None
    assert "acme.broken" in caplog.text
    assert "read" in caplog.text.lower()
