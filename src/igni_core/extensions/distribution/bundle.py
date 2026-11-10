"""The archive: its digest, its safe unpacking, and every check.

Between a download and the store there is a chain of checks, and each
one is a refusal that leaves the machine as it was: the bytes are the
ones the listing describes, the signature is the publisher's and is
over those bytes, the archive unpacks without reaching outside itself.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from pathlib import Path

from igni_core.extensions.bundle_signature import BundleSignatureError, PublisherKeys
from igni_core.extensions.distribution.constants import MAX_UNPACKED_BYTES, SIGNATURE_HEADER
from igni_core.extensions.distribution.errors import DistributionError
from igni_core.extensions.distribution.schemas import DistributedExtension, DownloadedBundle


class BundleArchive:
    """A downloaded archive: its digest, and unpacking it safely."""

    def __init__(self, archive: bytes) -> None:
        self.archive = archive
        self.sha256 = hashlib.sha256(archive).hexdigest()

    def extract(self, target: Path) -> None:
        """Unpack into ``target``, refusing anything that escapes it.

        The ``data`` filter refuses absolute paths, ``..``, links that
        point outside and device files; the size check before it
        bounds what a small archive can expand to.
        """
        target.mkdir(parents=True, exist_ok=True)
        try:
            with tarfile.open(fileobj=io.BytesIO(self.archive), mode="r:gz") as tar:
                members = tar.getmembers()
                if sum(m.size for m in members if m.isfile()) > MAX_UNPACKED_BYTES:
                    raise DistributionError("the bundle unpacks to more than igni allows")
                tar.extractall(target, members=members, filter="data")
        except (tarfile.TarError, gzip.BadGzipFile, EOFError, OSError) as exc:
            raise DistributionError(f"the bundle could not be unpacked: {exc}") from exc


class BundleVerifier:
    """Every check between a download and the store."""

    @staticmethod
    def verify(entry: DistributedExtension, downloaded: DownloadedBundle) -> BundleArchive:
        """The archive, if it is the one the listing describes and signed."""
        archive = BundleArchive(downloaded.archive)
        if downloaded.sha256 and downloaded.sha256.lower() != archive.sha256:
            raise DistributionError("the archive does not match the digest igni sent with it")
        if entry.sha256.lower() != archive.sha256:
            raise DistributionError("the archive is not the one the listing describes")
        if not downloaded.signature:
            raise DistributionError(f"the bundle came without {SIGNATURE_HEADER}")
        try:
            claim = PublisherKeys(entry.publisher_jwks.as_jwks()).verify(downloaded.signature)
        except BundleSignatureError as exc:
            raise DistributionError(str(exc)) from exc
        if claim.sha256.lower() != archive.sha256:
            raise DistributionError("the signature is for different bytes than were downloaded")
        if (claim.extension, claim.version) != (entry.extension_id, entry.version):
            raise DistributionError(
                f"the signature is for {claim.extension} {claim.version}, and igni "
                f"listed {entry.extension_id} {entry.version}"
            )
        return archive


__all__ = ["BundleArchive", "BundleVerifier"]
