"""Where an installed bundle came from.

A bundle igni server distributed — its signature checked against the
approved service's key — is recorded in ``<id>.distribution.json``
beside it. A bundle with no record was installed for development. The
installer decides trust from that, and the distribution sync decides
what to update and what to remove from it, so a development install is
never overwritten or removed by a sync.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from igni_core.extensions.store_errors import StoreName

logger = logging.getLogger(__name__)

#: Beside a bundle: where it came from, when igni distributed it.
DISTRIBUTION_SUFFIX = ".distribution.json"


class DistributionRecord(BaseModel):
    """A bundle igni server distributed, as it was verified.

    Written only after the signature checked out against the
    publisher's key and the archive's digest matched it, so its
    presence is what "distributed by igni" means on this machine.
    """

    extension_id: str
    version: str
    sha256: str
    #: The service the extension belongs to — and the audience of the
    #: token the host fetches for it.
    service_url: str
    published_at: datetime | None = None


class Provenance:
    """The distribution records of the bundles in one store."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def path(self, extension_id: str) -> Path:
        """Where this extension's record is written."""
        return self._root / StoreName(extension_id).with_suffix(DISTRIBUTION_SUFFIX)

    def record(self, record: DistributionRecord) -> None:
        """Write down that igni distributed this bundle."""
        self._root.mkdir(parents=True, exist_ok=True)
        self.path(record.extension_id).write_text(
            record.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )

    def of(self, extension_id: str) -> DistributionRecord | None:
        """How igni distributed this extension, or ``None`` when it did not.

        An unreadable record reads as none, which makes the bundle a
        development install: the safe direction, because that needs
        the dev flag to start.
        """
        path = self.path(extension_id)
        if not path.is_file():
            return None
        try:
            return DistributionRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValidationError) as exc:
            logger.warning("ignoring %s: %s", path, exc)
            return None

    def records(self) -> list[DistributionRecord]:
        """Every extension igni distributed to this machine."""
        if not self._root.is_dir():
            return []
        records: list[DistributionRecord] = []
        for path in sorted(self._root.glob(f"*{DISTRIBUTION_SUFFIX}")):
            record = self.of(path.name.removesuffix(DISTRIBUTION_SUFFIX))
            if record is not None:
                records.append(record)
        return records


__all__ = ["DISTRIBUTION_SUFFIX", "DistributionRecord", "Provenance"]
