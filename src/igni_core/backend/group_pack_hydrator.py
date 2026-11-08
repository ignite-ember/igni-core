"""Fetches, verifies and stores the group-policy pack for a token.

:class:`GroupPackHydrator` is the part of keeping the group pack fresh
that needs no session: given a bearer token it asks igni server for the
pack, checks the signature and writes the on-disk cache. A running
session wraps it in :class:`~igni_core.backend.group_policy_sync.GroupPolicySync`,
which adds the timer and reloads the session's group agents when a pack
lands; ``igni login`` in a terminal, which has no session, uses it on
its own.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from igni_core.core.auth.portal_client import PortalClient
from igni_core.core.auth.schemas import JwtClaims, SignedInSession
from igni_core.core.config.group_policy.cache import GroupPolicyCache
from igni_core.core.config.group_policy.refresher import PackRefresher
from igni_core.core.config.group_policy.trust import PackTrust
from igni_core.core.config.group_policy.verifier import PackVerifier
from igni_core.core.plugins.installer import PluginInstaller
from igni_core.core.plugins.source_policy import PluginSourcePolicy

if TYPE_CHECKING:
    from igni_core.core.config.settings import Settings

logger = logging.getLogger(__name__)


class GroupPackHydrator:
    """Refreshes the cached group pack from ``portal``, one call at a time.

    Holds the hydration lock — the only state — and reads the cache
    location, issuer and pinned keys off ``settings``. When the user picks
    another server, :meth:`use_portal` points this at the new one.
    """

    def __init__(self, settings: Settings, portal: PortalClient) -> None:
        self._settings = settings
        self._portal = portal
        # Serializes concurrent :meth:`hydrate` calls so a cold-start
        # fetch and a fresh login can't race on the same
        # ``pack_meta.json`` write. Lazy because ``asyncio.Lock`` binds
        # to the current event loop; constructed on first async use.
        self._hydration_lock: asyncio.Lock | None = None

    def use_portal(self, portal: PortalClient) -> None:
        """Ask ``portal`` from now on — the user chose another server."""
        self._portal = portal

    async def hydrate(self, token: str, *, force: bool = False) -> bool:
        """Refresh the cached group policy pack if stale; ``True`` when one landed.

        Wraps :class:`PackRefresher` so this class doesn't need to know
        which on-disk path the cache writes to — that decision lives in
        :class:`GroupPolicyLayout`.

        ``force`` skips the age check — a login or a new dialogue should
        pick up an admin's change now, and the ETag makes an unchanged
        pack a 304 rather than a download.

        Builds a fresh :class:`PluginInstaller` per call (cheap, no IO at
        construction) so plugin overrides with ``source_url`` get
        git-installed instead of being logged and skipped.

        A fetch that fails is the refresher's ``fetch_failed``; what is
        caught here is the disk (``OSError``) and a cache file that no
        longer parses (``ValueError``, which pydantic's errors are). The
        cached pack, if any, is left untouched so the next call retries.
        """
        if self._hydration_lock is None:
            self._hydration_lock = asyncio.Lock()
        async with self._hydration_lock:
            if not token:
                logger.info("group-policy: skipped hydration (no bearer token)")
                return False
            data_dir = Path(self._settings.storage.data_dir).expanduser()
            try:
                cache = GroupPolicyCache.for_data_dir(data_dir, installer=self._installer(data_dir))
                if not force and not cache.meta.is_stale():
                    return False
                result = await PackRefresher(cache, self._pack_verifier(token)).refresh_if_stale(
                    token, self._portal.fetch_group_pack, force=force
                )
            except (OSError, ValueError) as exc:
                logger.warning("group-policy hydration failed: %s", exc)
                return False
            # ``unchanged`` is a 304: nothing new was written.
            if not result.ok:
                return False
            logger.info("group-policy: group policy pack refreshed")
            return True

    def _installer(self, data_dir: Path) -> PluginInstaller:
        """A group's plugin sources are the admin's, but an air-gapped
        install still fetches only from inside its network."""
        return PluginInstaller(
            data_dir=data_dir, source_policy=PluginSourcePolicy.from_settings(self._settings)
        )

    def _pack_verifier(self, token: str) -> PackVerifier:
        """What checks a fetched pack before any of it is written.

        Bound to the person this token is for (``sub``) and its organisation
        (``org``, when it names one), the configured
        ``api_url`` (``iss``) and the pinned keys, which only the managed
        file or the environment can set. The keys themselves come from igni
        server's JWKS, fetched now. See ``core/config/group_policy/signature.py``.
        """
        claims = JwtClaims.decode(token)
        trust = PackTrust(
            api_url=self._settings.api_url or None,
            session=SignedInSession(
                subject=claims.sub if claims else None,
                org=claims.org if claims else None,
                token=token,
            ),
            pins=tuple(self._settings.group_policy.trusted_keys or ()),
        )
        return PackVerifier(trust=trust, jwks_fetch=self._portal.fetch_jwks)


__all__ = ["GroupPackHydrator"]
