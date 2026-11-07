"""Fetch the pack when the cached one is stale, verify it, store it, materialise it."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from igni_core.core.config.group_policy.answers import (
    PackAnswer,
    PackFetcher,
    SignedAbsence,
    UnchangedPack,
)
from igni_core.core.config.group_policy.cache import GroupPolicyCache
from igni_core.core.config.group_policy.meta import PackMeta
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.group_policy.verifier import PackVerifier

logger = logging.getLogger(__name__)


class RefreshStatus(StrEnum):
    fresh = "fresh"
    no_token = "no_token"  # noqa: S105 — a status name, not a credential
    fetch_failed = "fetch_failed"
    #: igni server confirmed the cached pack is current (a 304).
    unchanged = "unchanged"
    #: The fetcher could not tell; the cache is kept.
    no_answer = "no_answer"
    unverifiable_absence = "unverifiable_absence"
    rejected = "rejected"
    materialize_failed = "materialize_failed"
    #: igni server signed "no pack for this person"; the cache was cleared.
    withdrawn = "withdrawn"
    applied = "applied"


class RefreshResult(BaseModel):
    """What one refresh did."""

    model_config = ConfigDict(frozen=True)

    status: RefreshStatus
    reason: str = ""

    @property
    def ok(self) -> bool:
        """Whether a new pack (or its signed withdrawal) was written."""
        return self.status in {RefreshStatus.applied, RefreshStatus.withdrawn}


class PackRefresher:
    """Refreshes one cache through a :class:`PackVerifier`.

    Production always passes a verifier. Without one — tests of the
    file-shaped kinds, which predate signing — a pack's files are
    materialised but no signed pack is stored, so its ``settings`` never apply.
    """

    def __init__(self, cache: GroupPolicyCache, verifier: PackVerifier | None = None) -> None:
        self._cache = cache
        self._verifier = verifier

    async def refresh_if_stale(
        self, token: str, fetcher: PackFetcher, *, force: bool = False
    ) -> RefreshResult:
        """Refresh the on-disk pack if the cached one is missing or stale.

        ``force`` skips the age check and asks anyway. It does **not** skip
        the ETag: an unchanged pack still comes back as :class:`UnchangedPack`,
        so forcing costs a conditional request rather than a download. Used at
        the start of a session, where a developer should pick up an admin's
        change immediately.
        """
        if not token:
            return RefreshResult(status=RefreshStatus.no_token)
        if not force and not self._cache.meta.is_stale():
            return RefreshResult(status=RefreshStatus.fresh)
        try:
            answer = await fetcher(token, self._etag_to_send())
        except Exception as exc:  # noqa: BLE001 — a fetcher is caller-supplied I/O; failing is its fetch_failed outcome
            # Warning (not debug) so portal outages are visible in the
            # standard log without flipping a flag.
            logger.warning("group-policy fetch failed: %s", exc)
            return RefreshResult(status=RefreshStatus.fetch_failed, reason=str(exc))
        return await self._apply(answer)

    def _etag_to_send(self) -> str | None:
        """The tag of the pack held — unless a 304 would confirm something not whole.

        With no signed pack on disk there is nothing for a 304 to confirm, and
        a 304 would also confirm a pack whose plugin was removed from disk; in
        both cases the full pack is what is needed.
        """
        if self._verifier is not None and (
            not self._cache.signed.signed_text()
            or self._cache.plugins.lacks_any(self._cache.signed.unverified_plugin_sources())
        ):
            return None
        return self._cache.meta.stored_etag()

    async def _apply(self, answer: PackAnswer) -> RefreshResult:
        if isinstance(answer, UnchangedPack):
            # Nothing to write, but the pack on disk is confirmed current — so
            # stamp it, or every poll would find it stale and ask again.
            self._cache.meta.touch()
            return RefreshResult(status=RefreshStatus.unchanged)
        if answer is None:
            return RefreshResult(status=RefreshStatus.no_answer)
        if self._verifier is None:
            if isinstance(answer, SignedAbsence):
                return RefreshResult(status=RefreshStatus.unverifiable_absence)
            return self._materialize(answer)

        verification = await self._verifier(answer)
        if verification.accepted is None:
            logger.warning(
                "group-policy: the pack igni server sent was refused (%s); "
                "nothing from it has been applied",
                verification.reason,
            )
            return RefreshResult(status=RefreshStatus.rejected, reason=verification.reason)
        accepted = verification.accepted
        # Stored first: the settings it carries are what the next start
        # enforces, and they must not wait on a plugin clone below.
        self._cache.signed.store(accepted)
        if accepted.pack is None:
            self._cache.clear_derived()
            self._cache.meta.save(PackMeta.absence(answer.etag, datetime.now(timezone.utc)))
            logger.info("group-policy: igni server says there is no pack; cache cleared")
            return RefreshResult(status=RefreshStatus.withdrawn)
        return self._materialize(accepted.pack)

    def _materialize(self, pack: GroupPolicyPack) -> RefreshResult:
        try:
            self._cache.materialize(pack)
        except (OSError, ValueError) as exc:
            logger.warning("group-policy materialize failed: %s", exc)
            return RefreshResult(status=RefreshStatus.materialize_failed, reason=str(exc))
        return RefreshResult(status=RefreshStatus.applied)
