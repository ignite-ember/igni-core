"""Update checker — asks PyPI whether a newer release exists.

Calls ``GET https://pypi.org/pypi/{package}/json`` and inspects
the ``info.version`` field of the response. The check runs
asynchronously at session start and never blocks the user.
Results are cached in ``~/.igni/.update-check`` (a JSON blob
matching :class:`UpdateCacheEntry`) to avoid hitting the network
on every session; the TTL is configurable via
``update_check_ttl`` in settings.

**Set ``update_check_ttl: 0`` to disable it entirely**, or
``air_gapped: true``, which disables it along with every other
outbound flow. An air-gapped deployment should set the second: until
the first existed there was no way to stop this request.

Architecture — everything is on :class:`UpdateChecker`:

* :class:`UpdateCache` (composed value object) owns the file-cache
  read/write. Its constructor takes the cache path, so a test points
  it at a tmp file instead of patching a module global.
* :class:`PackageMetadata` (in the sibling schemas module) owns the
  :func:`importlib.metadata.metadata` probe. The checker resolves it
  lazily, on first use, and keeps it on the instance: importing this
  module runs no probe, and two checkers share no state.
* :meth:`UpdateChecker.check` is the entry point and the top
  boundary. It never raises for a failed check — every failure comes
  back as an :class:`UpdateInfo` carrying a typed
  :class:`UpdateError`. Only :exc:`asyncio.CancelledError`
  propagates, because a parent-task cancellation is not a failed
  check.

Every Pydantic model in this module comes from
:mod:`.update_checker_schemas` and is re-exported here so
external callers can keep a single import site.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import httpx
from pydantic import ValidationError

from igni_core import __version__
from igni_core.core.activity.http import busy_call
from igni_core.core.config.settings import Settings
from igni_core.core.config.settings_loader import SettingsLoader
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.utils.update_checker_schemas import (
    PackageMetadata,
    PyPIInfoResponse,
    UpdateCacheEntry,
    UpdateError,
    UpdateInfo,
    Version,
)

# Re-exports so callers keep the single-import-site convention.
__all__ = [
    "PackageMetadata",
    "PyPIInfoResponse",
    "UpdateCache",
    "UpdateCacheEntry",
    "UpdateChecker",
    "UpdateError",
    "UpdateInfo",
    "Version",
    "default_cache_path",
]

logger = logging.getLogger(__name__)


def default_cache_path() -> Path:
    """Where the update check's cache file lives.

    A function rather than a module constant: the path depends on the
    home directory, which is read when it is asked for — not frozen
    into the module when it is imported.
    """
    return Path.home() / CONFIG_DIR / ".update-check"


# ── Cache value object ────────────────────────────────────────────


class UpdateCache:
    """File-backed TTL cache for the update check.

    Composed onto :class:`UpdateChecker` rather than exposed as a
    module-level pair of free functions. Constructor takes the
    path so tests point it at a tmp file directly — no more
    module-global patching for cache IO.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self, ttl: int) -> UpdateCacheEntry | None:
        """Return the cached entry when it exists and is still
        fresh. Any read / parse failure is logged and treated as a
        cache miss (the caller falls back to the network) so a
        corrupt cache file can never break the update check."""
        try:
            if not self._path.exists():
                return None
            raw = self._path.read_text()
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("Failed to read update cache: %s", exc)
            return None
        try:
            entry = UpdateCacheEntry.model_validate_json(raw)
        except (ValueError, ValidationError) as exc:
            logger.debug("Failed to parse update cache: %s", exc)
            return None
        if not entry.is_fresh(ttl):
            return None
        return entry

    def write(self, entry: UpdateCacheEntry) -> None:
        """Persist the entry to disk. Best-effort — a full disk /
        permission-denied is logged and swallowed so the update
        check never breaks the caller."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Rewrite ``checked_at`` at write time so the TTL is
            # measured from persistence, not construction.
            payload = entry.model_copy(update={"checked_at": time.time()})
            self._path.write_text(payload.model_dump_json())
        except OSError as exc:
            logger.debug("Failed to write update cache: %s", exc)


# ── Orchestrator ─────────────────────────────────────────────────


class UpdateChecker:
    """Asks PyPI whether a newer release exists.

    Composition:

    * :class:`UpdateCache` — file IO
    * :class:`PackageMetadata` — installed-package probe (lazy)

    The ``timeout`` + ``settings`` + ``cache_path`` are constructor
    args so a test / caller can override every collaborator
    without patching module state.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        timeout: float = 5.0,
        cache_path: Path | None = None,
        package: PackageMetadata | None = None,
    ) -> None:
        self._settings = settings
        self._timeout = timeout
        self._cache = UpdateCache(cache_path if cache_path is not None else default_cache_path())
        self._package = package

    @property
    def package(self) -> PackageMetadata:
        """The installed package's metadata.

        Resolved on first use and kept on the instance: the probe
        walks the site-packages dist-info, so it is not free, but it
        is not paid at import time either — and two checkers share
        nothing.
        """
        if self._package is None:
            self._package = PackageMetadata.load()
        return self._package

    async def check(self) -> UpdateInfo:
        """Ask PyPI (or the cache) whether a newer version exists.

        The top boundary of the check: it never raises. A failure the
        check knows about comes back with its own
        :class:`UpdateError`; a failure it does not come back as
        :attr:`UpdateError.UNKNOWN`. :exc:`asyncio.CancelledError`
        propagates — a parent-task cancellation is not a failed
        check.
        """
        try:
            return await self._check()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the top boundary; reported, not raised
            logger.warning("Update check failed (unexpected): %s", exc)
            return self._failure(UpdateError.UNKNOWN, str(exc))

    async def _check(self) -> UpdateInfo:
        """The check itself, in three lanes: off, cache, network."""
        current = __version__
        settings = self._settings if self._settings is not None else SettingsLoader.load()
        ttl = settings.update_check_ttl

        # Off switch. ``update_check_ttl`` only ever changed how *often*
        # this ran, so there was no way to stop it — an air-gapped install
        # attempted an outbound request to pypi.org at every session start,
        # failing silently. That request is small but it is unconfigured
        # network traffic leaving the customer's estate, and it reveals
        # that this machine runs igni, at this version, from this address.
        #
        # DP-5 in the compliance tracker is the claim that nothing leaves
        # the customer's network, and the claim carries the GDPR position.
        # ``0`` now means off, matching the retention settings where 0
        # means the feature does nothing.
        #
        # ``air_gapped`` is the same answer for a whole deployment: PyPI is
        # outside its network whatever the TTL says.
        if settings.air_gapped or ttl <= 0:
            return self._current()

        cached = self._cache.read(ttl)
        if cached is not None:
            return self._from_cache(cached, current)
        return await self._from_pypi(current)

    def _from_cache(self, cached: UpdateCacheEntry, current: str) -> UpdateInfo:
        """The answer the cache holds, when it names a newer release."""
        latest = cached.latest_version
        if latest and Version.parse(latest) > Version.parse(current):
            return UpdateInfo(
                available=True,
                latest_version=latest,
                current_version=current,
                release_notes=cached.release_notes,
                download_url=cached.download_url,
            )
        return self._current()

    async def _from_pypi(self, current: str) -> UpdateInfo:
        """The answer PyPI holds — or the reason it could not be asked."""
        pypi_url = f"https://pypi.org/pypi/{self.package.name}/json"
        try:
            async with (
                busy_call("update-check"),
                httpx.AsyncClient(timeout=self._timeout) as client,
            ):
                resp = await client.get(pypi_url)
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPStatusError as exc:
            logger.debug("Update check failed (HTTP status): %s", exc)
            return self._failure(UpdateError.HTTP_STATUS, str(exc))
        except httpx.HTTPError as exc:
            logger.debug("Update check failed (network): %s", exc)
            return self._failure(UpdateError.NETWORK, str(exc))
        except ValueError as exc:  # resp.json() decode failure
            logger.debug("Update check failed (parse): %s", exc)
            return self._failure(UpdateError.PARSE, str(exc))

        try:
            info = PyPIInfoResponse.model_validate(payload)
        except ValidationError as exc:
            logger.debug("Update check failed (schema): %s", exc)
            return self._failure(UpdateError.PARSE, str(exc))

        latest = info.info.version
        release_url = info.info.project_url or self.package.project_url

        # Cache the response for the next session.
        self._cache.write(UpdateCacheEntry(latest_version=latest, download_url=release_url))

        if latest and Version.parse(latest) > Version.parse(current):
            return UpdateInfo(
                available=True,
                latest_version=latest,
                current_version=current,
                download_url=release_url,
            )
        return self._current()

    def _current(self) -> UpdateInfo:
        """The answer when nothing newer is out."""
        return UpdateInfo(available=False, current_version=__version__)

    def _failure(self, error: UpdateError, detail: str) -> UpdateInfo:
        """The answer when the check could not be made."""
        return UpdateInfo(
            available=False,
            current_version=__version__,
            error=error,
            error_detail=detail,
        )
