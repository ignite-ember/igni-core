"""The three distribution calls, with the person's igni session.

The listing, the bundle and the assets — each one asked for with the
session's bearer token, each one bounded, each one refusing to hand
back more than the server promised.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from pydantic import ValidationError

from igni_core.core.activity.http import busy_call
from igni_core.extensions.assets import AssetCache, AssetError, ExtensionAsset
from igni_core.extensions.distribution.constants import (
    ASSET_DOWNLOAD_TIMEOUT,
    MAX_ARCHIVE_BYTES,
    SHA256_HEADER,
    SIGNATURE_HEADER,
)
from igni_core.extensions.distribution.errors import DistributionError
from igni_core.extensions.distribution.schemas import DistributionListing, DownloadedBundle
from igni_core.extensions.service_token import IgniSession


class DistributionClient:
    """The distribution calls, with the person's igni session."""

    LISTING_PATH = "/v1/extensions"

    def __init__(
        self,
        api_url: str,
        session: IgniSession,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
        download_timeout: float = 300.0,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {session.access_token}"}
        self._transport = transport
        self._timeout = timeout
        self._download_timeout = download_timeout

    async def listing(self) -> DistributionListing:
        """What igni server lists for this person."""
        try:
            async with (
                busy_call("extension-distribution"),
                httpx.AsyncClient(
                    timeout=self._timeout,
                    transport=self._transport,
                ) as client,
            ):
                response = await client.get(
                    f"{self._api_url}{self.LISTING_PATH}", headers=self._headers
                )
        except httpx.HTTPError as exc:
            raise DistributionError(f"igni could not be reached: {exc}") from exc
        if response.status_code != 200:
            raise DistributionError(f"igni refused the extension listing ({response.status_code})")
        try:
            return DistributionListing.model_validate_json(response.content)
        except ValidationError as exc:
            raise DistributionError(f"the extension listing is malformed: {exc}") from exc

    async def bundle(self, extension_id: str) -> DownloadedBundle:
        """One extension's bundle, with the signature and digest headers."""
        url = f"{self._api_url}{self.LISTING_PATH}/{extension_id}/bundle"
        try:
            async with (
                busy_call("extension-distribution"),
                httpx.AsyncClient(
                    timeout=self._download_timeout,
                    transport=self._transport,
                ) as client,
                client.stream("GET", url, headers=self._headers) as response,
            ):
                if response.status_code != 200:
                    raise DistributionError(
                        f"igni refused the bundle download ({response.status_code})"
                    )
                received = bytearray()
                async for chunk in response.aiter_bytes():
                    received.extend(chunk)
                    if len(received) > MAX_ARCHIVE_BYTES:
                        raise DistributionError("the bundle is larger than any igni accepts")
                return DownloadedBundle(
                    archive=bytes(received),
                    signature=response.headers.get(SIGNATURE_HEADER, ""),
                    sha256=response.headers.get(SHA256_HEADER, ""),
                )
        except httpx.HTTPError as exc:
            raise DistributionError(f"the bundle download failed: {exc}") from exc

    async def asset(self, extension_id: str, asset: ExtensionAsset, cache: AssetCache) -> Path:
        """Download one asset into ``cache``, held to its size and digest on the way."""
        url = f"{self._api_url}{self.LISTING_PATH}/{extension_id}/assets/{asset.sha256}"
        partial = cache.partial()
        try:
            async with (
                busy_call("extension-distribution"),
                httpx.AsyncClient(
                    timeout=ASSET_DOWNLOAD_TIMEOUT,
                    transport=self._transport,
                ) as client,
                client.stream("GET", url, headers=self._headers) as response,
            ):
                if response.status_code != 200:
                    raise DistributionError(
                        f"igni refused asset {asset.name} ({asset.platform}): "
                        f"{response.status_code}"
                    )
                received = 0
                with partial.open("wb") as out:
                    async for chunk in response.aiter_bytes():
                        received += len(chunk)
                        if received > asset.size:
                            raise DistributionError(
                                f"asset {asset.name} ({asset.platform}) is larger than "
                                "its manifest pins"
                            )
                        out.write(chunk)
        except httpx.HTTPError as exc:
            partial.unlink(missing_ok=True)
            raise DistributionError(f"asset {asset.name} could not be downloaded: {exc}") from exc
        except DistributionError:
            partial.unlink(missing_ok=True)
            raise
        try:
            return cache.adopt(partial, asset)
        except AssetError as exc:
            raise DistributionError(str(exc)) from exc


__all__ = ["DistributionClient"]
