"""The wire's names and limits.

Shared by the client that fetches a bundle and the verifier that
checks it, so both name the same header the same way.
"""

from __future__ import annotations

#: The compact JWS a bundle is signed with.
SIGNATURE_HEADER = "X-Igni-Bundle-Signature"

#: The digest of the bundle, as the server says it.
SHA256_HEADER = "X-Igni-Bundle-Sha256"

#: igni server's limit on an upload, so the most a download can be.
MAX_ARCHIVE_BYTES = 400 * 1024 * 1024

#: The most an archive may unpack to. Wheels are already compressed;
#: this bounds a bomb.
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024

#: How long one asset may take to arrive: a JVM or a database is
#: hundreds of megabytes.
ASSET_DOWNLOAD_TIMEOUT = 1800.0
