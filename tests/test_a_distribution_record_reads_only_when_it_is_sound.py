"""Provenance: the records of the bundles igni distributed, and what a damaged one reads as.

A record is the only proof a bundle was distributed by igni. One that
cannot be read — not JSON, not the right shape, not even text — reads
as none, which makes the bundle a development install: the safe
direction, because that needs the dev flag to start. Reading it must
never raise: the installer and the sync ask for it on every pass.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from igni_core.extensions.provenance import DistributionRecord, Provenance
from igni_core.extensions.store_errors import StoreError


def record_of(extension_id: str, **changes: object) -> DistributionRecord:
    fields: dict[str, object] = {
        "extension_id": extension_id,
        "version": "1.0.0",
        "sha256": "a" * 64,
        "service_url": "https://notes.acme.test",
    }
    fields.update(changes)
    return DistributionRecord.model_validate(fields)


def test_recording_makes_the_missing_directory_and_writes_indented_json(tmp_path: Path) -> None:
    root = tmp_path / "not" / "yet"
    provenance = Provenance(root)

    provenance.record(record_of("acme.notes"))

    written = (root / "acme.notes.distribution.json").read_text(encoding="utf-8")
    assert written.startswith('{\n  "extension_id": "acme.notes"')
    assert written.endswith("}\n")


def test_recording_again_replaces_the_record(tmp_path: Path) -> None:
    provenance = Provenance(tmp_path)
    provenance.record(record_of("acme.notes", version="1.0.0"))

    provenance.record(record_of("acme.notes", version="2.0.0"))

    found = provenance.of("acme.notes")
    assert found is not None and found.version == "2.0.0"


def test_a_record_with_no_publication_date_round_trips(tmp_path: Path) -> None:
    provenance = Provenance(tmp_path)
    provenance.record(record_of("acme.notes"))

    found = provenance.of("acme.notes")
    assert found is not None and found.published_at is None


def test_an_extension_with_no_record_reads_as_none(tmp_path: Path) -> None:
    assert Provenance(tmp_path).of("acme.notes") is None
    assert Provenance(tmp_path / "absent").of("acme.notes") is None


def test_a_record_that_is_a_directory_reads_as_none(tmp_path: Path) -> None:
    provenance = Provenance(tmp_path)
    provenance.path("acme.notes").mkdir()

    assert provenance.of("acme.notes") is None


def test_a_record_of_the_wrong_shape_reads_as_none_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    provenance = Provenance(tmp_path)
    provenance.path("acme.notes").write_text('{"extension_id": "acme.notes"}')

    with caplog.at_level(logging.WARNING):
        assert provenance.of("acme.notes") is None

    assert "ignoring" in caplog.text and "acme.notes" in caplog.text


def test_a_record_that_is_not_text_reads_as_none_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A half-written or overwritten file is bytes, not UTF-8: still none, never a crash."""
    provenance = Provenance(tmp_path)
    provenance.path("acme.notes").write_bytes(b"\xff\xfe\x00 not text")

    with caplog.at_level(logging.WARNING):
        assert provenance.of("acme.notes") is None

    assert "ignoring" in caplog.text


def test_a_name_that_escapes_the_store_is_refused(tmp_path: Path) -> None:
    provenance = Provenance(tmp_path)

    with pytest.raises(StoreError):
        provenance.path("../elsewhere")
    with pytest.raises(StoreError):
        provenance.record(record_of("../elsewhere"))


def test_the_records_are_listed_in_file_name_order_without_the_broken_ones(
    tmp_path: Path,
) -> None:
    provenance = Provenance(tmp_path)
    for name in ("acme.zeta", "acme.alpha", "acme.mid"):
        provenance.record(record_of(name))
    provenance.path("acme.broken").write_text("{ not json")
    provenance.path("acme.binary").write_bytes(b"\xff\xfe")
    (tmp_path / "acme.alpha.json").write_text("{}")  # not a record's file name

    assert [found.extension_id for found in provenance.records()] == [
        "acme.alpha",
        "acme.mid",
        "acme.zeta",
    ]


def test_no_directory_means_no_records(tmp_path: Path) -> None:
    assert Provenance(tmp_path / "absent").records() == []
